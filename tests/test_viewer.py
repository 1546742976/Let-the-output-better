from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app import viewer
from app.settings import PipelineConfig, Settings


@contextmanager
def running_client(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    pipeline: PipelineConfig | None = None,
) -> Iterator[TestClient]:
    """lifespan 在 app.main 命名空间查找配置函数，因此在此打桩。"""
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "get_pipeline_config", lambda: pipeline or PipelineConfig())
    with TestClient(api.app) as client:
        yield client


@pytest.fixture
def audit_file(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def client(audit_file: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with running_client(monkeypatch, Settings(provider="mock", audit_path=str(audit_file))) as instance:
        yield instance


def write_records(path: Path, records: list[Any]) -> None:
    lines = []
    for record in records:
        lines.append(record if isinstance(record, str) else json.dumps(record, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestPageAssets:
    def test_web_dir_is_absolute_and_inside_package(self):
        """静态目录不能依赖进程 CWD，否则换目录起服就 404。"""
        assert viewer.WEB_DIR.is_absolute()
        assert viewer.WEB_DIR.parent == Path(viewer.__file__).parent

    def test_index_is_served_as_html(self, client: TestClient):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert 'id="optimize-form"' in response.text

    def test_page_wires_every_static_asset(self, client: TestClient):
        for asset in ("/static/app.js", "/static/styles.css"):
            assert client.get(asset).status_code == 200, asset

    def test_page_never_injects_markup_into_the_dom(self):
        """模型与审计输出属不可信文本，只能经 textContent 写入；这里拦截真实的注入写法。"""
        script = (viewer.WEB_DIR / "app.js").read_text(encoding="utf-8")
        for pattern in (r"\.innerHTML\s*=", r"\.outerHTML\s*=", r"insertAdjacentHTML\s*\(", r"document\.write\s*\("):
            assert re.search(pattern, script) is None, f"页面出现 HTML 注入写法：{pattern}"

    def test_page_has_no_external_resource_references(self):
        html = (viewer.WEB_DIR / "index.html").read_text(encoding="utf-8")
        for url in html.lower().split('"'):
            assert not url.startswith(("http://", "https://")), f"页面不应引用站外资源：{url}"


class TestRuntimeConfig:
    def test_exposes_builtin_schemas_with_required_fields(self, client: TestClient):
        config = client.get("/v1/config").json()
        assert set(config["schemas"]) == {"paper", "reference", "experiment"}
        assert config["schemas"]["paper"]["required"] == ["title"]
        assert config["schemas"]["experiment"]["required"] == ["method", "metric", "value"]

    def test_step_flags_match_pipeline_defaults(self, client: TestClient):
        config = client.get("/v1/config").json()
        assert config["steps"]["humanize"]["enabled"] is False
        assert config["steps"]["schema"]["enabled"] is True
        assert config["steps"]["schema"]["strict"] is False
        assert config["reference_style"] == "gb-t-7714"

    def test_pipeline_overrides_are_visible_to_the_page(self, audit_file: Path, monkeypatch: pytest.MonkeyPatch):
        """页面显示的分值必须与实际执行口径一致（含 pipeline.yaml 覆盖 .env）。"""
        pipeline = PipelineConfig(score_threshold=0.61, timeout_ms=4321)
        with running_client(monkeypatch, Settings(audit_path=str(audit_file)), pipeline) as client:
            config = client.get("/v1/config").json()
        assert config["score_threshold"] == 0.61
        assert config["timeout_ms"] == 4321


class TestAuditHistory:
    def test_missing_file_returns_empty_list(self, client: TestClient, audit_file: Path):
        payload = client.get("/v1/audits").json()
        assert payload["items"] == []
        assert payload["total_lines"] == 0
        assert Path(payload["path"]) == audit_file

    def test_newest_record_first_and_limit_applied(self, client: TestClient, audit_file: Path):
        write_records(audit_file, [{"request_id": "req_1"}, {"request_id": "req_2"}, {"request_id": "req_3"}])
        payload = client.get("/v1/audits?limit=2").json()
        assert [item["request_id"] for item in payload["items"]] == ["req_3", "req_2"]
        assert payload["total_lines"] == 3

    def test_duplicate_request_ids_stay_distinguishable_by_line_no(self, client: TestClient, audit_file: Path):
        """request_id 由进程内计数器生成，多进程会重号；页面选中态依赖 line_no 唯一。"""
        write_records(audit_file, [{"request_id": "req_dup"}, {"request_id": "other"}, {"request_id": "req_dup"}])
        items = client.get("/v1/audits").json()["items"]
        assert [item["request_id"] for item in items] == ["req_dup", "other", "req_dup"]
        assert [item["line_no"] for item in items] == [3, 2, 1]

    def test_corrupt_and_non_object_lines_are_skipped(self, client: TestClient, audit_file: Path):
        write_records(audit_file, [{"request_id": "ok_1"}, "这不是 JSON", [1, 2], {"request_id": "ok_2"}])
        payload = client.get("/v1/audits").json()
        assert [item["request_id"] for item in payload["items"]] == ["ok_2", "ok_1"]
        assert payload["skipped"] == 2
        assert payload["total_lines"] == 4

    def test_blank_lines_are_not_counted(self, client: TestClient, audit_file: Path):
        audit_file.write_text('{"request_id":"a"}\n\n   \n', encoding="utf-8")
        assert client.get("/v1/audits").json()["total_lines"] == 1

    def test_history_does_not_accept_a_path_parameter(self, client: TestClient, audit_file: Path):
        """审计路径只来自配置，查询参数里的 path 必须被忽略。"""
        write_records(audit_file, [{"request_id": "mine"}])
        payload = client.get("/v1/audits?path=C:/Windows/win.ini").json()
        assert Path(payload["path"]) == audit_file

    @pytest.mark.parametrize("limit", ["0", "201"])
    def test_limit_is_bounded(self, client: TestClient, limit: str):
        assert client.get(f"/v1/audits?limit={limit}").status_code == 422


class TestReadAuditTail:
    def test_limit_is_clamped_to_the_maximum(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        write_records(path, [{"request_id": f"req_{index}"} for index in range(viewer.MAX_AUDIT_ITEMS + 30)])
        assert len(viewer.read_audit_tail(path, limit=10_000)["items"]) == viewer.MAX_AUDIT_ITEMS

    def test_limit_below_one_still_returns_one(self, tmp_path: Path):
        path = tmp_path / "audit.jsonl"
        write_records(path, [{"request_id": "a"}, {"request_id": "b"}])
        assert len(viewer.read_audit_tail(path, limit=0)["items"]) == 1

    def test_small_file_line_count_is_exact(self, tmp_path: Path):
        """读到文件头时 total_exact 为真，total_lines 是全文件非空行数。"""
        path = tmp_path / "audit.jsonl"
        write_records(path, [{"request_id": "1"}, {"request_id": "2"}, "   ", {"request_id": "3"}])
        payload = viewer.read_audit_tail(path, limit=10)
        assert payload["total_exact"] is True
        assert payload["total_lines"] == 3

    def test_window_truncated_but_bof_reached_is_still_exact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """窗口被 scan_cap 截断，但整文件在一块内读完并触到文件头：total_lines 仍是全量而非窗口大小。"""
        path = tmp_path / "audit.jsonl"
        write_records(path, [{"request_id": f"req_{index}"} for index in range(50)])
        monkeypatch.setattr(viewer, "MAX_SCANNED_LINES", 10)  # wanted=2 → scan_cap=max(20,10)=20 < 50
        payload = viewer.read_audit_tail(path, limit=2)
        assert payload["total_exact"] is True
        assert payload["total_lines"] == 50  # 全量，不受窗口=20 影响
        assert payload["scanned"] == 20  # 窗口只留最后 20 行
        assert [item["line_no"] for item in payload["items"]] == [50, 49]

    def test_large_file_marks_total_as_window_lower_bound(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """小读块下窗口在触及文件头前就被 scan_cap 填满，未读到文件头：total_exact 转假、total_lines 是下界。"""
        path = tmp_path / "audit.jsonl"
        write_records(path, [{"request_id": f"req_{index}"} for index in range(50)])
        monkeypatch.setattr(viewer, "READ_BLOCK", 40)  # 每块约 1.8 行，凑满窗口时仍距文件头很远
        monkeypatch.setattr(viewer, "MAX_SCANNED_LINES", 10)
        payload = viewer.read_audit_tail(path, limit=2)  # wanted=2 → scan_cap=max(20,10)=20
        assert payload["total_exact"] is False
        # 未读到文件头，total_lines 只是已扫到的下界，必然小于全文件 50 行
        assert 20 <= payload["total_lines"] < 50
        assert len(payload["items"]) == 2

    def test_line_longer_than_read_block_is_reassembled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """超长单行跨多个读取块时，靠 carry 拼接，仍是一条完整记录。"""
        monkeypatch.setattr(viewer, "READ_BLOCK", 16)
        path = tmp_path / "audit.jsonl"
        big = {"request_id": "big", "blob": "x" * 400}
        path.write_text(json.dumps(big, ensure_ascii=False) + "\n", encoding="utf-8")
        payload = viewer.read_audit_tail(path, limit=5)
        assert len(payload["items"]) == 1
        assert payload["items"][0]["request_id"] == "big"
        assert payload["items"][0]["blob"] == "x" * 400
        assert payload["total_exact"] is True


class TestAuditCarriesInputText:
    def test_record_written_by_a_request_includes_input_text(self, client: TestClient, audit_file: Path):
        source = "《测试论文》，张三，2023 年。"
        response = client.post(
            "/v1/optimize",
            json={
                "raw_output": '{"title":"测试论文","year":"2023"}',
                "schema": "paper",
                "input_text": source,
                "instruction": "抽取论文元数据",
            },
        )
        assert response.status_code == 200
        record = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[-1])
        assert record["input_text"] == source
        assert record["instruction"] == "抽取论文元数据"
        assert record["request_id"] == response.json()["request_id"]

    def test_history_backfills_a_selected_record(self, client: TestClient, audit_file: Path):
        client.post("/v1/optimize", json={"raw_output": '{"title":"T"}', "schema": "paper", "input_text": "原文"})
        items = client.get("/v1/audits").json()["items"]
        assert items[0]["input_text"] == "原文"
        assert items[0]["final_output"]["title"] == "T"
