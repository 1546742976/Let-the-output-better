from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.harness import HarnessBusy, HarnessError, HarnessTimeout, HarnessUnavailable
from app.settings import PipelineConfig, Settings

PY = sys.executable
ECHO = 'import sys; sys.stdin.read(); sys.stdout.write(\'{"title":"来自 harness 的回答","year":"2023"}\')'
FAIL = "import sys; sys.stdin.read(); sys.stderr.write('上游失败'); raise SystemExit(4)"
SLEEP = "import sys,time; sys.stdin.read(); time.sleep(5); print('{}')"


@contextmanager
def client_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    args: list[str],
    enabled: bool = True,
    timeout_ms: int = 15000,
    concurrency: int = 1,
) -> Iterator[TestClient]:
    harness_command = PY if enabled else ""
    settings = Settings(
        _env_file=None,
        provider="mock",
        audit_path=str(tmp_path / "audit.jsonl"),
        harness_enabled=enabled,
        harness_command=harness_command,
        harness_allowed_commands=[PY],
        harness_args=args,
        harness_timeout_ms=timeout_ms,
        harness_max_concurrency=concurrency,
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "get_pipeline_config", lambda: PipelineConfig(audit=True))
    with TestClient(api.app) as client:
        yield client


def test_run_is_503_when_harness_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with client_with(monkeypatch, tmp_path, args=["-c", ECHO], enabled=False) as client:
        response = client.post("/v1/run", json={"task": "抽取元数据"})
    assert response.status_code == 503
    assert "/v1/optimize" in response.json()["detail"]


def test_run_optimizes_harness_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with client_with(monkeypatch, tmp_path, args=["-c", ECHO]) as client:
        response = client.post(
            "/v1/run",
            json={"task": "抽取论文元数据", "schema": "paper", "input_text": "《来自 harness 的回答》，2023 年。"},
        )
    assert response.status_code == 200
    body = response.json()
    # 稳定契约的 8 个键必须原样存在，harness 块是额外附加。
    assert {
        "request_id",
        "final_output",
        "valid",
        "score",
        "repaired",
        "degraded",
        "steps",
        "usage",
    } <= set(body)
    assert body["valid"] is True
    assert body["final_output"]["year"] == 2023
    assert body["harness"]["name"] == "command"
    assert body["harness"]["exit_code"] == 0
    assert body["harness"]["source"] == "stdout"
    assert body["harness"]["chars"] > 0


def test_run_records_task_and_harness_into_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    audit = tmp_path / "audit.jsonl"
    with client_with(monkeypatch, tmp_path, args=["-c", ECHO]) as client:
        client.post("/v1/run", json={"task": "抽取论文元数据", "schema": "paper"})
    record = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
    assert record["task"] == "抽取论文元数据"
    assert record["harness"]["name"] == "command"
    # 核心键不可被 audit_extra 覆盖。
    assert record["raw_output"] == '{"title":"来自 harness 的回答","year":"2023"}'


def test_harness_failure_never_enters_the_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    audit = tmp_path / "audit.jsonl"
    with client_with(monkeypatch, tmp_path, args=["-c", FAIL]) as client:
        response = client.post("/v1/run", json={"task": "x"})
    assert response.status_code == 502
    assert "退出码 4" in response.json()["detail"]
    assert not audit.exists() or audit.read_text(encoding="utf-8").strip() == ""


def test_harness_timeout_maps_to_504(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with client_with(monkeypatch, tmp_path, args=["-c", SLEEP], timeout_ms=400) as client:
        response = client.post("/v1/run", json={"task": "x"})
    assert response.status_code == 504
    assert "超时" in response.json()["detail"]


def test_unknown_schema_is_rejected_before_spawning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """schema 校验在 harness 之前：不合法请求不该白跑一次子进程。"""
    with client_with(monkeypatch, tmp_path, args=["-c", "raise SystemExit(9)"]) as client:
        response = client.post("/v1/run", json={"task": "x", "schema": "thesis"})
    assert response.status_code == 422
    assert "paper, reference, experiment" in response.json()["detail"]


def test_empty_task_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with client_with(monkeypatch, tmp_path, args=["-c", ECHO]) as client:
        assert client.post("/v1/run", json={"task": ""}).status_code == 422


def test_command_body_cannot_specify_the_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """请求体里带 command 必须被忽略——否则无鉴权端口等于远程命令执行。"""
    with client_with(monkeypatch, tmp_path, args=["-c", ECHO]) as client:
        response = client.post(
            "/v1/run",
            json={"task": "x", "command": "cmd.exe", "args": ["/c", "del /q *"], "cwd": "C:/Windows"},
        )
    assert response.status_code == 200
    assert response.json()["harness"]["name"] == "command"


class RaisingHarness:
    """把 HarnessError 族逐个映射到 HTTP 语义，不必真的等待子进程调度。"""

    name = "command"

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def run(self, *, task: str, timeout_s: float):
        raise self.error

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HarnessBusy("并发已达上限（1）"), 429),
        (HarnessUnavailable("不在白名单内"), 503),
        (HarnessTimeout("超时（0.4s）"), 504),
        (HarnessError("退出码 1"), 502),
    ],
)
def test_harness_errors_map_to_distinct_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: int,
):
    with client_with(monkeypatch, tmp_path, args=["-c", ECHO]) as client:
        api.app.state.harness = RaisingHarness(error)
        response = client.post("/v1/run", json={"task": "x"})
    assert response.status_code == expected


def test_error_detail_never_leaks_the_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with client_with(monkeypatch, tmp_path, args=["-c", FAIL]) as client:
        detail = client.post("/v1/run", json={"task": "x"}).json()["detail"]
    assert PY not in detail
    assert "上游失败" in detail


def test_config_endpoint_reports_harness_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with client_with(monkeypatch, tmp_path, args=["-c", ECHO], enabled=False) as client:
        harness = client.get("/v1/config").json()["harness"]
    assert harness == {
        "enabled": False,
        "name": None,
        "timeout_ms": 15000,
        "max_concurrency": 1,
        "detect": True,
        "api_ready": False,
        "options": [],
    }


def test_config_lists_assembled_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """options 必须来自实际装配结果，页面下拉才有依据。"""
    with client_with(monkeypatch, tmp_path, args=["-c", ECHO]) as client:
        harness = client.get("/v1/config").json()["harness"]
    assert "command" in harness["options"]
    assert harness["name"] in harness["options"]


def test_run_can_select_profile_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with client_with(monkeypatch, tmp_path, args=["-c", ECHO]) as client:
        ok = client.post("/v1/run", json={"task": "x", "harness": "command"})
        unknown = client.post("/v1/run", json={"task": "x", "harness": "not-a-profile"})
    assert ok.status_code == 200
    assert unknown.status_code == 422
    assert "not-a-profile" in unknown.json()["detail"]
