from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.harness import CommandHarness, HarnessBusy, HarnessError, HarnessTimeout, HarnessUnavailable
from app.harness.command import build_child_env, resolve_allowed_command
from app.settings import Settings

PY = sys.executable
# 桩 harness：把 stdin 原样大写回 stdout，以此证明任务确实经 stdin 传入。
ECHO = "import sys; sys.stdout.write(sys.stdin.read().upper())"
FAIL = "import sys; sys.stderr.write('boom'); raise SystemExit(3)"
SLEEP = "import sys, time; time.sleep(float(sys.argv[1] if len(sys.argv) > 1 else 5)); print('late')"
BIG = "import sys; sys.stdout.write('x' * 200000)"
ENV_DUMP = "import os,sys; sys.stdout.write(repr(sorted(os.environ)))"
WRITE_OUT = "import sys,pathlib; pathlib.Path(sys.argv[1]).write_text('来自 output 文件', encoding='utf-8')"


def harness(*, args: list[str], allowed: list[str] | None = None, **kwargs) -> CommandHarness:
    kwargs.setdefault("command", PY)
    kwargs.setdefault("allowed_commands", allowed if allowed is not None else [PY])
    return CommandHarness(args=args, **kwargs)


class TestAllowlist:
    def test_relative_path_is_rejected(self):
        with pytest.raises(HarnessUnavailable, match="绝对路径"):
            resolve_allowed_command("python", ["python"])

    def test_path_outside_allowlist_is_rejected(self):
        with pytest.raises(HarnessUnavailable, match="白名单"):
            resolve_allowed_command(PY, [str(Path(PY).parent / "other.exe")])

    def test_missing_file_is_rejected(self, tmp_path: Path):
        with pytest.raises(HarnessUnavailable, match="不存在"):
            resolve_allowed_command(str(tmp_path / "nope.exe"), [str(tmp_path / "nope.exe")])

    def test_empty_command_is_rejected(self):
        with pytest.raises(HarnessUnavailable, match="为空"):
            resolve_allowed_command("", [PY])

    def test_basename_allowlist_does_not_bypass_absolute_check(self):
        """白名单写 basename 也不能放行相对命令——否则等于允许任意 python -c。"""
        with pytest.raises(HarnessUnavailable, match="绝对路径"):
            resolve_allowed_command("python", ["python", "node"])


class TestChildEnv:
    def test_secrets_are_not_inherited(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
        env = build_child_env(())
        assert "OPENAI_API_KEY" not in env
        assert "PATH" in env

    def test_explicit_allow_entry_is_passed_through(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEX_HOME", "C:/somewhere")
        assert build_child_env(("CODEX_HOME",))["CODEX_HOME"] == "C:/somewhere"


class TestRun:
    async def test_task_is_passed_via_stdin_not_argv(self):
        result = await harness(args=["-c", ECHO]).run(task="抽取论文元数据", timeout_s=20)
        assert result.text == "抽取论文元数据".upper()
        assert result.source == "stdout"
        assert result.exit_code == 0
        assert result.ms >= 0

    async def test_nonzero_exit_is_an_error_with_stderr(self):
        with pytest.raises(HarnessError, match="退出码 3"):
            await harness(args=["-c", FAIL]).run(task="x", timeout_s=20)

    async def test_timeout_kills_and_reaps_child(self):
        runner = harness(args=["-c", SLEEP, "5"])
        with pytest.raises(HarnessTimeout):
            await runner.run(task="x", timeout_s=0.4)
        # 被拒/超时后计数必须归零，否则一次超时会永久占死并发额度。
        assert runner._in_flight == 0

    async def test_oversized_output_is_truncated(self):
        result = await harness(args=["-c", BIG], max_output_bytes=1024).run(task="x", timeout_s=30)
        assert result.truncated is True
        assert len(result.text) <= 1024

    async def test_output_file_wins_over_stdout(self, tmp_path: Path):
        target = tmp_path / "collector.py"
        target.write_text(
            "import sys, pathlib\n"
            "data = sys.stdin.read()\n"
            "sys.stdout.write('stdout 噪声 ' + data)\n"
            "pathlib.Path(sys.argv[1]).write_text('干净回答：' + data, encoding='utf-8')\n",
            encoding="utf-8",
        )
        runner = harness(args=[str(target), "{output_file}"])
        result = await runner.run(task="总结这篇论文", timeout_s=30)
        assert result.source == "output_file"
        assert result.text == "干净回答：总结这篇论文"

    async def test_empty_output_is_rejected(self):
        with pytest.raises(HarnessError, match="未产出可用文本"):
            await harness(args=["-c", "import sys; sys.stdin.read()"]).run(task="x", timeout_s=20)

    async def test_child_environment_excludes_keys(self):
        result = await harness(args=["-c", ENV_DUMP]).run(task="x", timeout_s=20)
        assert "OPENAI_API_KEY" not in result.text


class TestConcurrency:
    async def test_second_call_is_rejected_while_one_runs(self):
        runner = harness(args=["-c", SLEEP, "1.2"], max_concurrency=1)
        first = asyncio.create_task(runner.run(task="a", timeout_s=20))
        await asyncio.sleep(0.2)
        with pytest.raises(HarnessBusy, match="并发已达上限"):
            await runner.run(task="b", timeout_s=20)
        assert (await first).exit_code == 0

    async def test_rejected_command_does_not_consume_the_slot(self):
        runner = CommandHarness(command="python", args=["-c", ECHO], allowed_commands=[PY])
        for _ in range(3):
            with pytest.raises(HarnessUnavailable):
                await runner.run(task="x", timeout_s=5)
        assert runner._in_flight == 0


class TestSettingsValidation:
    def test_enabled_without_command_is_allowed(self):
        """命令不再是必需项：发现式 profile 与 API 形态同样能提供 harness。"""
        settings = Settings(_env_file=None, harness_enabled=True, harness_command="", harness_allowed_commands=[])
        assert settings.harness_command == ""

    def test_enabled_with_unlisted_command_fails_at_startup(self):
        with pytest.raises(ValueError, match="白名单"):
            Settings(
                _env_file=None, harness_enabled=True, harness_command=PY, harness_allowed_commands=["C:/nope/x.exe"]
            )

    def test_disabled_settings_need_no_command(self):
        assert Settings(_env_file=None, harness_enabled=False).harness_command == ""

    def test_bad_cwd_is_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="HARNESS_CWD"):
            Settings(
                _env_file=None,
                harness_enabled=True,
                harness_command=PY,
                harness_allowed_commands=[PY],
                harness_cwd=str(tmp_path / "missing"),
            )
