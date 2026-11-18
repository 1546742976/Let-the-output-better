"""harness 装配层：发现式过滤、HarnessRegistry 选择、build_harness 多源合并。

此前 ``app/harness/__init__.py`` 覆盖率仅 65%——测试一律置 ``harness_detect=false``，
发现式装配 ``_detected()`` 与多 profile 的 ``HarnessRegistry`` 分支从未被执行。
这里把这些分支补上，且全程不 exec 任何程序：可执行目标只到 ``CommandHarness``
构造为止，真正的白名单校验发生在 ``run()``。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app import harness as harness_pkg
from app.harness import (
    ApiHarness,
    CommandHarness,
    HarnessRegistry,
    HarnessRun,
    HarnessUnavailable,
    build_harness,
    default_workdir,
)
from app.harness.base import BaseHarness
from app.harness.detect import HarnessCandidate

PY = sys.executable


def candidate(
    profile: str,
    *,
    status: str = "可执行",
    prefix: tuple[str, ...] = (PY, "entry.js"),
) -> HarnessCandidate:
    return HarnessCandidate(
        profile=profile,
        label=profile,
        drivable=True,
        status=status,
        prefix=prefix,
    )


class FakeHarness(BaseHarness):
    """可观察 run/aclose 的假成员，用来验证注册表的委派与级联关闭。"""

    def __init__(self, name: str, text: str = "ok") -> None:
        self.name = name
        self.text = text
        self.closed = False
        self.calls: list[tuple[str, float]] = []

    async def run(self, *, task: str, timeout_s: float) -> HarnessRun:
        self.calls.append((task, timeout_s))
        return HarnessRun(text=self.text, ms=1, kind="api", source="stub")

    async def aclose(self) -> None:
        self.closed = True


class TestHarnessRegistry:
    def test_default_must_be_a_member(self):
        with pytest.raises(HarnessUnavailable, match="默认 harness profile"):
            HarnessRegistry({"codex": FakeHarness("codex")}, default="ghost")

    def test_profiles_returns_a_defensive_copy(self):
        members = {"a": FakeHarness("a"), "b": FakeHarness("b")}
        registry = HarnessRegistry(members, default="a")
        snapshot = registry.profiles()
        snapshot["c"] = FakeHarness("c")
        assert set(registry.profiles()) == {"a", "b"}, "profiles() 不得暴露可变内部字典"

    def test_select_covers_default_known_and_unknown(self):
        a = FakeHarness("a")
        registry = HarnessRegistry({"a": a, "b": FakeHarness("b")}, default="a")
        assert registry.select(None) is a
        assert registry.select("b").name == "b"
        with pytest.raises(HarnessUnavailable, match="未知 harness profile"):
            registry.select("ghost")

    async def test_run_delegates_to_default_member(self):
        a = FakeHarness("a", text="来自默认")
        registry = HarnessRegistry({"a": a, "b": FakeHarness("b")}, default="a")
        run = await registry.run(task="跑一次", timeout_s=7.5)
        assert run.text == "来自默认"
        assert a.calls == [("跑一次", 7.5)]

    async def test_aclose_cascades_to_every_member(self):
        members = {"a": FakeHarness("a"), "b": FakeHarness("b"), "c": FakeHarness("c")}
        registry = HarnessRegistry(members, default="a")
        await registry.aclose()
        assert all(member.closed for member in members.values())


def settings(**kwargs):
    from app.settings import Settings

    kwargs.setdefault("_env_file", None)
    kwargs.setdefault("harness_enabled", True)
    return Settings(**kwargs)


class TestDetectedAssembly:
    def _build(self, monkeypatch: pytest.MonkeyPatch, candidates: list[HarnessCandidate], **kw):
        monkeypatch.setattr("app.harness.scan", lambda: list(candidates))
        return build_harness(settings(harness_detect=True, **kw))

    def test_drivable_candidate_becomes_named_command_harness(self, monkeypatch: pytest.MonkeyPatch):
        harness = self._build(monkeypatch, [candidate("codex")])
        assert isinstance(harness, CommandHarness)
        assert harness.name == "codex"
        # argv = launch 前缀（宿主+脚本）+ profile 常量模板；模板参数被拼在脚本之后
        assert harness.args[0] == "entry.js"
        assert "exec" in harness.args
        assert "{output_file}" in harness.args

    def test_undocumented_profile_is_skipped(self, monkeypatch: pytest.MonkeyPatch):
        assert self._build(monkeypatch, [candidate("mystery-cli")]) is None

    def test_non_drivable_profile_is_skipped(self, monkeypatch: pytest.MonkeyPatch):
        # opencode 已在 PROFILES 里登记但 profile.drivable=False：只检测、不驱动
        assert self._build(monkeypatch, [candidate("opencode")]) is None

    def test_unverified_status_is_skipped(self, monkeypatch: pytest.MonkeyPatch):
        assert self._build(monkeypatch, [candidate("codex", status="已检测未验证")]) is None

    def test_empty_launch_is_skipped(self, monkeypatch: pytest.MonkeyPatch):
        assert self._build(monkeypatch, [candidate("codex", prefix=())]) is None

    def test_command_plus_detected_yields_a_registry(self, monkeypatch: pytest.MonkeyPatch):
        """显式命令源与发现式 codex 同时存在时收成 registry（发现 profile 只有 codex 可驱动）。"""
        monkeypatch.setattr("app.harness.scan", lambda: [candidate("codex")])
        built = build_harness(settings(harness_detect=True, harness_command=PY, harness_allowed_commands=[PY]))
        assert isinstance(built, HarnessRegistry)
        assert set(built.profiles()) == {"command", "codex"}


class TestBuildHarnessCombination:
    def test_detected_plus_api_merges_into_registry(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("app.harness.scan", lambda: [candidate("codex")])
        harness = build_harness(settings(harness_detect=True, openai_api_key="sk-x", openai_model="m1"))
        assert isinstance(harness, HarnessRegistry)
        assert set(harness.profiles()) == {"codex", "openai-api"}
        # 插入顺序：发现式在前、API 在后，未指定 profile 时默认取第一个
        assert harness.default == "codex"
        assert isinstance(harness.profiles()["openai-api"], ApiHarness)

    def test_explicit_profile_selects_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("app.harness.scan", lambda: [candidate("codex")])
        harness = build_harness(settings(harness_detect=True, openai_api_key="sk-x", harness_profile="openai-api"))
        assert isinstance(harness, HarnessRegistry)
        assert harness.default == "openai-api"

    def test_unknown_profile_falls_back_to_first(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("app.harness.scan", lambda: [candidate("codex")])
        # 只有单成员时 build_harness 直接返回该成员、不套 registry，故补一个 API 成员凑成多成员
        harness = build_harness(settings(harness_detect=True, openai_api_key="sk-x", harness_profile="not-real"))
        assert isinstance(harness, HarnessRegistry)
        assert harness.default == "codex"

    def test_providers_allowlist_filters_detected(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("app.harness.scan", lambda: [candidate("codex")])
        harness = build_harness(settings(harness_detect=True, harness_providers="openai-api"))
        assert harness is None, "白名单里没有 codex，发现式成员应被剔除"


class TestDefaultWorkdir:
    def test_is_a_temp_dir_and_cached(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(harness_pkg, "_WORKDIR", None)
        first = default_workdir()
        assert first.is_dir()
        assert first != Path.cwd(), "不得让外部 agent 继承服务进程 cwd（本仓库）"
        assert default_workdir() == first, "第二次调用应复用同一临时目录"
        monkeypatch.setattr(harness_pkg, "_WORKDIR", None)
