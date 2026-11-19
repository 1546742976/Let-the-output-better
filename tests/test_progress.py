"""运行进度登记与 /v1/progress：长任务期间页面必须能说出"卡在哪一段"，而不是只有转圈。

登记只供本地查看页解释现状，不参与优化语义；这里钉住三件事：步骤 start/end 成对
（隔离异常也不例外）、在飞阶段可被并发轮询看到、结束（含 429）后 active 清空且
last 的 run_id 与响应同源。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import app.main as api
from app import progress
from app.harness.base import HarnessBusy, HarnessRun
from app.optimizer import optimize
from app.optimizer.steps import REGISTRY
from app.providers.mock import MockProvider
from app.settings import PipelineConfig, Settings

PROSE = "综上所述，本研究可能在一定程度上具有参考价值，但仍需进一步验证。"


@pytest.fixture(autouse=True)
def _isolated_registry():
    progress.reset()
    yield
    progress.reset()


async def _noop() -> None:
    return None


@asynccontextmanager
async def async_client(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    *,
    harness: Any = None,
    provider: Any = None,
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "get_pipeline_config", lambda: PipelineConfig(audit=False))
    if harness is not None:
        monkeypatch.setattr(api, "build_harness", lambda _settings: harness)
    if provider is not None:
        monkeypatch.setattr(api, "build_provider", lambda _settings: provider)
    async with api.lifespan(api.app):
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _settings(tmp_path: Path, **extra: Any) -> Settings:
    return Settings(_env_file=None, provider="mock", audit_path=str(tmp_path / "audit.jsonl"), **extra)


class TestRegistry:
    def test_lifecycle_from_begin_to_last(self):
        progress.begin("r1", "run", "harness")
        progress.phase("r1", "pipeline")
        progress.step_done("r1", "schema")
        progress.step_done("r1", "schema")
        snap = progress.snapshot()
        assert snap["active"][0]["phase"] == "pipeline"
        assert snap["active"][0]["done_steps"] == ["schema"]
        assert snap["last"] is None
        progress.finish("r1")
        snap = progress.snapshot()
        assert snap["active"] == []
        assert snap["last"]["phase"] == "done"
        assert snap["last"]["run_id"] == "r1"

    def test_failure_is_recorded_not_dropped(self):
        progress.begin("r2", "optimize", "pipeline")
        progress.finish("r2", ok=False, detail="boom")
        last = progress.snapshot()["last"]
        assert last["phase"] == "failed"
        assert last["detail"] == "boom"

    def test_finish_unknown_id_is_a_noop(self):
        progress.finish("nope")
        assert progress.snapshot() == {"active": [], "last": None}

    def test_elapsed_ms_is_non_negative(self):
        state = progress.begin("r3", "run", "harness")
        assert state.as_dict()["elapsed_ms"] >= 0


class TestStepEvents:
    async def test_on_step_reports_start_and_end_in_pipeline_order(self, tmp_path: Path):
        events: list[tuple[str, str]] = []
        await optimize(
            PROSE,
            settings=_settings(tmp_path),
            config=PipelineConfig(audit=False),
            on_step=lambda event, step: events.append((event, step)),
        )
        starts = [step for event, step in events if event == "start"]
        ends = [step for event, step in events if event == "end"]
        assert starts == ends
        assert starts[0] == "schema"
        assert "simplify" in starts

    async def test_isolated_step_failure_still_ends(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        class Boom:
            def __init__(self, config: Any, settings: Any) -> None:
                pass

            def should_run(self, ctx: Any) -> bool:
                return True

            async def run(self, ctx: Any) -> Any:
                raise RuntimeError("boom")

        monkeypatch.setitem(REGISTRY, "rules", Boom)
        events: list[tuple[str, str]] = []
        result = await optimize(
            PROSE,
            settings=_settings(tmp_path),
            config=PipelineConfig(audit=False),
            on_step=lambda event, step: events.append((event, step)),
        )
        assert ("start", "rules") in events
        assert ("end", "rules") in events
        assert any("step_error(rules)" in error for error in result.errors)


class TestProgressEndpoint:
    async def test_idle_shape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        async with async_client(monkeypatch, _settings(tmp_path)) as client:
            assert (await client.get("/v1/progress")).json() == {"active": [], "last": None}

    async def test_run_shows_harness_phase_then_done(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        entered, release = asyncio.Event(), asyncio.Event()

        async def run(*, task: str, timeout_s: float) -> HarnessRun:
            entered.set()
            await release.wait()
            return HarnessRun(text='{"title":"T"}', ms=5)

        harness = SimpleNamespace(name="stub", run=run, aclose=_noop)
        async with async_client(monkeypatch, _settings(tmp_path, harness_enabled=True), harness=harness) as client:
            posting = asyncio.create_task(client.post("/v1/run", json={"task": "x", "schema": "paper"}))
            await asyncio.wait_for(entered.wait(), 5)
            snap = (await client.get("/v1/progress")).json()
            assert snap["active"][0]["kind"] == "run"
            assert snap["active"][0]["phase"] == "harness"
            release.set()
            response = await posting
            assert response.status_code == 200
            snap = (await client.get("/v1/progress")).json()
            assert snap["active"] == []
            assert snap["last"]["run_id"] == response.json()["request_id"]
            assert snap["last"]["phase"] == "done"

    async def test_busy_run_is_recorded_as_failed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        async def run(*, task: str, timeout_s: float) -> HarnessRun:
            raise HarnessBusy("harness 并发已达上限（1），请稍后再试")

        harness = SimpleNamespace(name="stub", run=run, aclose=_noop)
        async with async_client(monkeypatch, _settings(tmp_path, harness_enabled=True), harness=harness) as client:
            response = await client.post("/v1/run", json={"task": "x"})
            assert response.status_code == 429
            snap = (await client.get("/v1/progress")).json()
            assert snap["active"] == []
            assert snap["last"]["phase"] == "failed"
            assert "并发已达上限" in snap["last"]["detail"]

    async def test_optimize_shows_live_step_phase(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        class GateProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def chat(self, **kwargs: Any) -> Any:
                self.entered.set()
                await self.release.wait()
                return await super().chat(**kwargs)

        gate = GateProvider()
        async with async_client(monkeypatch, _settings(tmp_path), provider=gate) as client:
            posting = asyncio.create_task(client.post("/v1/optimize", json={"raw_output": PROSE}))
            await asyncio.wait_for(gate.entered.wait(), 5)
            snap = (await client.get("/v1/progress")).json()
            assert snap["active"][0]["kind"] == "optimize"
            assert snap["active"][0]["phase"] == "simplify"
            assert "schema" in snap["active"][0]["done_steps"]
            gate.release.set()
            response = await posting
            assert response.status_code == 200
            snap = (await client.get("/v1/progress")).json()
            assert snap["active"] == []
            assert snap["last"]["run_id"] == response.json()["request_id"]
