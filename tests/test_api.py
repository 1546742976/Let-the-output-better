from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.settings import PipelineConfig, Settings

README_BODY = {
    "raw_output": '{"title":"基于深度学习的图像分类方法研究","year":"2023"}',
    "schema": "paper",
    "input_text": "《基于深度学习的图像分类方法研究》，张三、李四，发表于 CVPR 2023。",
    "options": {"repair": False},
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(provider="mock", audit_path=str(tmp_path / "api-audit.jsonl"))
    # lifespan 在 app.main 命名空间里查找这两个函数，因此必须在此打桩
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "get_pipeline_config", lambda: PipelineConfig(audit=False))
    with TestClient(api.app) as instance:
        yield instance


class TestInputLimits:
    """入口封顶：审计会整段落盘，不设上限等于让一次请求写进几百 MB。"""

    def test_oversized_raw_output_is_rejected(self, client):
        response = client.post("/v1/optimize", json={"raw_output": "值得" * (api.MAX_OUTPUT_CHARS // 2 + 1)})
        assert response.status_code == 422
        assert "max_len" in json.dumps(response.json(), ensure_ascii=False) or "longer" in json.dumps(
            response.json(), ensure_ascii=False
        )

    def test_oversized_input_text_is_rejected(self, client):
        response = client.post(
            "/v1/optimize",
            json={"raw_output": "hi", "input_text": "x" * (api.MAX_INPUT_CHARS + 1)},
        )
        assert response.status_code == 422

    def test_boundary_sizes_are_accepted(self, client):
        """上限本身可用，否则封顶就变成了新的隐性拒绝。

        这里关掉全部步骤：本用例要验的是入口是否放行，不是 200k 字符的评分吞吐
        ——留着 rules 跑满整段会让整套测试从秒级涨到近两分钟。
        """
        response = client.post(
            "/v1/optimize",
            json={
                "raw_output": "x" * api.MAX_OUTPUT_CHARS,
                "options": {"score": False, "repair": False, "format": False, "simplify": False},
            },
        )
        assert response.status_code == 200


class TestHealthz:
    def test_reports_active_provider(self, client):
        assert client.get("/healthz").json() == {"status": "ok", "provider": "mock"}


class TestOptimizeEndpoint:
    def test_readme_curl_payload(self, client):
        response = client.post("/v1/optimize", json=README_BODY)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "request_id",
            "final_output",
            "valid",
            "score",
            "repaired",
            "degraded",
            "steps",
            "usage",
        }
        assert body["valid"] is True
        assert body["score"] is not None
        assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
        assert [step["name"] for step in body["steps"]] == ["schema", "rules", "format"]

    def test_headline_example_is_consumable_json(self, client):
        fenced = (
            "好的，以下是这篇论文的相关信息，希望对您有帮助！\n"
            '```json\n{"title": "T", "authors": "张三、李四", "year": "2023", '
            '"reference": "Zhang San, Li Si. Research on image classification. CVPR 2023.", '
            '"note": "请随时告诉我"}\n```'
        )
        body = client.post(
            "/v1/optimize",
            json={"raw_output": fenced, "schema": "paper", "instruction": "抽取论文元数据"},
        ).json()
        assert body["final_output"]["authors"] == ["张三", "李四"]
        assert body["final_output"]["year"] == 2023
        assert body["final_output"]["references"][0].endswith("[C]//CVPR. 2023.")
        assert "note" not in body["final_output"]

    @pytest.mark.parametrize("schema_name", ["paper", "reference", "experiment"])
    def test_builtin_schemas_are_reachable(self, client, schema_name):
        payload = {
            "paper": '{"title":"T"}',
            "reference": "He K. Deep residual learning. CVPR 2016.",
            "experiment": '{"method":"M","metric":"Acc","value":"91.2 %"}',
        }[schema_name]
        response = client.post("/v1/optimize", json={"raw_output": payload, "schema": schema_name})
        assert response.status_code == 200
        assert response.json()["valid"] is True

    def test_unknown_schema_returns_422_with_options(self, client):
        response = client.post("/v1/optimize", json={"raw_output": "{}", "schema": "thesis"})
        assert response.status_code == 422
        assert "paper, reference, experiment" in response.json()["detail"]

    def test_missing_raw_output_is_rejected(self, client):
        assert client.post("/v1/optimize", json={"schema": "paper"}).status_code == 422

    def test_empty_raw_output_is_rejected(self, client):
        assert client.post("/v1/optimize", json={"raw_output": "", "schema": "paper"}).status_code == 422

    def test_invalid_structured_output_falls_back_to_raw_string(self, client):
        raw = "抱歉，我无法完成该请求。"
        body = client.post(
            "/v1/optimize", json={"raw_output": raw, "schema": "paper", "options": {"repair": False}}
        ).json()
        assert body["valid"] is False
        assert body["final_output"] == raw
        assert body["degraded"] is False

    def test_options_can_enable_simplify_per_request(self, client):
        text = "值得注意的是，本文方法充分表明具有显著的性能优势和广阔的应用前景。"
        body = client.post(
            "/v1/optimize",
            json={"raw_output": text, "options": {"simplify": True, "score": True}},
        ).json()
        assert "simplify" in [step["name"] for step in body["steps"]]
        assert body["final_output"] != text

    def test_options_can_disable_format_step(self, client):
        fenced = '前言\n```json\n{"title": "T"}\n```\n后记'
        body = client.post(
            "/v1/optimize",
            json={"raw_output": fenced, "schema": "paper", "options": {"format": False}},
        ).json()
        assert "format" not in [step["name"] for step in body["steps"]]

    def test_timeout_option_bounds_the_pipeline(self, client):
        body = client.post(
            "/v1/optimize",
            json={
                "raw_output": '{"authors":"张三"}',
                "schema": "paper",
                "options": {"timeout_ms": 8000},
            },
        ).json()
        assert body["degraded"] is False

    def test_response_is_json_serialisable(self, client):
        response = client.post("/v1/optimize", json=README_BODY)
        assert json.loads(response.text)["request_id"].startswith("req_")
