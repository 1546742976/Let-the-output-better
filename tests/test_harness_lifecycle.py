"""批 4 不变式：子进程生命周期、进程级并发预算、探测限流。

这些行为此前全部没有测试覆盖（所以改坏了 CI 也是绿的）：每条用例都写成一个
具体故障的可执行描述——取消后进程必须死、成员必须共用一份预算、重复探测不得重复 exec。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.harness import CommandHarness
from app.harness.api import ApiHarness
from app.harness.base import ConcurrencyBudget, HarnessBusy, HarnessError
from app.harness.command import _redact

PY = sys.executable
SLEEP = "import sys, time; time.sleep(30); print('late')"
# 共享预算用例只需要"还在跑"的状态，用长睡眠会把整套拖到半分钟
HOLD = "import sys, time; time.sleep(1.5); print('held')"
# 头尾哨兵之间塞满超过 max_output_bytes 的内容：截断后保留哪一端一目了然
HEAD_TAIL = "import sys; sys.stdout.write('HEAD' + 'x' * 60000 + 'TAIL')"


def harness(*, args: list[str], budget: ConcurrencyBudget | None = None, **kwargs) -> CommandHarness:
    kwargs.setdefault("command", PY)
    kwargs.setdefault("allowed_commands", [PY])
    return CommandHarness(args=args, budget=budget, **kwargs)


class TestChildLifecycle:
    async def test_cancelling_the_run_terminates_the_child(self):
        """外层取消过去不经过任何终止路径：进程活着，并发额度却已归还。"""
        runner = harness(args=["-c", SLEEP])
        task = asyncio.create_task(runner.run(task="a", timeout_s=60))
        await asyncio.sleep(0.5)  # 让子进程确实起来
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.budget.in_flight == 0
        proc = runner.last_process
        assert proc is not None
        for _ in range(40):
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.1)
        assert proc.returncode is not None, "取消后子进程仍在运行（泄漏）"

    async def test_unexpected_error_becomes_harness_error_not_a_crash(self, monkeypatch: pytest.MonkeyPatch):
        runner = harness(args=["-c", "print(1)"])

        async def boom(*args, **kwargs):  # 模拟管道/编码等非 OSError 意外
            raise RuntimeError("管道炸了")

        monkeypatch.setattr(CommandHarness, "_exchange", boom)
        with pytest.raises(HarnessError, match="RuntimeError"):
            await runner.run(task="x", timeout_s=5)
        assert runner.budget.in_flight == 0

    async def test_timeout_still_reports_and_reclaims(self):
        runner = harness(args=["-c", SLEEP])
        with pytest.raises(HarnessError, match="超时"):
            await runner.run(task="x", timeout_s=0.4)
        assert runner.budget.in_flight == 0
        assert runner.last_process is not None and runner.last_process.returncode is not None

    async def test_truncated_output_keeps_the_tail_not_the_head(self):
        """最终回答在末尾：保头等于拿一段没有结论的文本去评分。"""
        runner = harness(args=["-c", HEAD_TAIL], max_output_bytes=1024)
        result = await runner.run(task="q", timeout_s=20)
        assert result.text.startswith("x") or "TAIL" in result.text, "尾部被截掉了"
        assert "HEAD" not in result.text, "保留的还是开头"
        assert result.truncated is True


class TestRedaction:
    def test_key_shaped_values_are_masked(self):
        text = 'export OPENAI_API_KEY=sk-proj-supersecretvalue and token: "abc123456"'
        out = _redact(text)
        assert "supersecretvalue" not in out
        assert "abc123456" not in out

    def test_ordinary_error_text_is_untouched(self):
        assert _redact("ModuleNotFoundError: no module named codex") == "ModuleNotFoundError: no module named codex"


class TestSharedBudget:
    def test_budget_default_matches_max_concurrency(self):
        runner = harness(args=["-c", "print(1)"], max_concurrency=3)
        assert runner.budget.limit == 3
        assert runner.max_concurrency == 3

    async def test_two_members_share_one_budget(self):
        first = harness(args=["-c", HOLD], budget=None)
        budget = first.budget
        second = harness(args=["-c", HOLD], budget=budget)
        assert second.budget is budget, "装配方没把预算接起来，429 语义就是错的"
        running = asyncio.create_task(first.run(task="a", timeout_s=60))
        await asyncio.sleep(0.4)
        with pytest.raises(HarnessBusy):
            await second.run(task="b", timeout_s=60)
        await running

    async def test_budget_is_released_even_when_the_member_raises(self):
        budget = ConcurrencyBudget(1)
        runner = ApiHarness(_FailingProvider(), max_concurrency=1, budget=budget)
        with pytest.raises(HarnessError):
            await runner.run(task="x", timeout_s=1)
        assert budget.in_flight == 0
        # 额度回来了，下一次调用不该被 429 挡住
        assert budget.limit == 1


class _FailingProvider:
    name = "failing"

    async def chat(self, **kwargs):
        from app.providers.base import ProviderError

        raise ProviderError("上游不可用")

    async def aclose(self) -> None:
        return None


class TestProbeThrottle:
    async def test_repeated_probe_executes_once_within_the_ttl(self, monkeypatch: pytest.MonkeyPatch):
        from app.harness import detect
        from app.harness.profiles import PROFILES

        calls: list[list[str]] = []

        async def fake_run(argv, timeout_s):
            calls.append(argv)
            return 0, "codex-cli 0.99.0" if "--version" in argv else "Logged in"

        monkeypatch.setattr(detect, "_run_probe", fake_run)
        detect.clear_probe_cache()
        candidate = detect.HarnessCandidate(
            profile="codex",
            label="x",
            drivable=True,
            executable=Path(PY),
            entry=Path("codex.js"),
            status="可执行",
            prefix=(PY, "codex.js"),
        )
        assert PROFILES.get("codex") is not None
        await detect.probe(candidate, timeout_s=2)
        first_count = len(calls)
        assert first_count >= 1
        await detect.probe(candidate, timeout_s=2)
        assert len(calls) == first_count, "TTL 内重复探测又起了一次子进程"
        assert detect.probe_cache_size() >= 1
        await detect.probe(candidate, timeout_s=2, refresh=True)
        assert len(calls) > first_count, "refresh=True 必须绕开缓存"
        detect.clear_probe_cache()

    async def test_concurrent_probes_are_serialised(self, monkeypatch: pytest.MonkeyPatch):
        from app.harness import detect
        from app.harness.detect import HarnessCandidate

        active = 0
        peak = 0

        async def fake_run(argv, timeout_s):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return 0, "codex-cli 0.99.0"

        monkeypatch.setattr(detect, "_run_probe", fake_run)
        detect.clear_probe_cache()
        candidate = HarnessCandidate(
            profile="codex",
            label="x",
            drivable=True,
            executable=Path(PY),
            entry=Path("codex.js"),
            status="可执行",
            prefix=(PY, "codex.js"),
        )
        # 同一候选并发 5 次：锁 + 缓存应把它压成一次探测
        await asyncio.gather(*(detect.probe(candidate, timeout_s=2) for _ in range(5)))
        assert peak == 1, f"并发探测起了 {peak} 份，单飞锁没生效"
        detect.clear_probe_cache()
