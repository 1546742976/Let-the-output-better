"""每请求 reference_style：引用风格此前只有 pipeline.yaml 一个全局开关。

同一份服务要同时接 GB/T 7714 与 APA 的调用方时，全局开关意味着互相改配置。
这里钉住：逐请求覆盖双向生效、缺省跟随配置、坏值在入口炸（model_copy 不校验
Literal，静默落进 else 分支就是"选了 APA 却按 GB/T 排"）、HTTP 层 422 同口径。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.optimizer import optimize
from app.settings import PipelineConfig, Settings

PLAIN_CONF = "Zhang San, Li Si. Research on image classification based on deep learning. CVPR 2023."
RAW = json.dumps({"title": "基于深度学习的图像分类方法研究", "references": [PLAIN_CONF]}, ensure_ascii=False)


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, provider="mock", audit_path=str(tmp_path / "audit.jsonl"))


async def _references(tmp_path: Path, config: PipelineConfig, **kwargs: object) -> list[str]:
    result = await optimize(RAW, schema="paper", settings=_settings(tmp_path), config=config, **kwargs)
    return result.final_output["references"]


class TestPerRequestOverride:
    async def test_apa_override_differs_from_config_default(self, tmp_path: Path):
        config = PipelineConfig(audit=False)
        gbt = await _references(tmp_path, config)
        apa = await _references(tmp_path, config, reference_style="apa")
        assert gbt[0].endswith("[C]//CVPR. 2023.")
        assert apa[0] != gbt[0]
        assert "(2023)" in apa[0] and "&" in apa[0]

    async def test_override_wins_over_yaml_style_in_both_directions(self, tmp_path: Path):
        config = PipelineConfig(audit=False).with_step("format", reference_style="apa")
        forced_gbt = await _references(tmp_path, config, reference_style="gb-t-7714")
        followed = await _references(tmp_path, config)
        assert forced_gbt[0].endswith("[C]//CVPR. 2023.")
        assert "(2023)" in followed[0]

    async def test_unknown_style_raises_at_entry(self, tmp_path: Path):
        with pytest.raises(ValueError, match="reference_style"):
            await optimize(
                RAW,
                schema="paper",
                settings=_settings(tmp_path),
                config=PipelineConfig(audit=False),
                reference_style="mla",
            )


@contextmanager
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, audit: bool = False) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None, provider="mock", audit_path=str(tmp_path / "audit.jsonl"), harness_enabled=False
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "get_pipeline_config", lambda: PipelineConfig(audit=audit))
    with TestClient(api.app) as client:
        yield client


class TestHttpSurface:
    def test_optimize_accepts_apa_and_reports_it_in_step_reason(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        with _client(tmp_path, monkeypatch) as client:
            response = client.post(
                "/v1/optimize", json={"raw_output": RAW, "schema": "paper", "reference_style": "apa"}
            )
        assert response.status_code == 200
        payload = response.json()
        assert "(2023)" in payload["final_output"]["references"][0]
        format_step = next(step for step in payload["steps"] if step["name"] == "format")
        assert "->apa" in format_step["reason"]

    def test_optimize_rejects_unknown_style(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        with _client(tmp_path, monkeypatch) as client:
            response = client.post("/v1/optimize", json={"raw_output": RAW, "reference_style": "mla"})
        assert response.status_code == 422

    def test_run_accepts_the_same_field(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        with _client(tmp_path, monkeypatch) as client:
            ok = client.post("/v1/run", json={"task": "x", "reference_style": "apa"})
            bad = client.post("/v1/run", json={"task": "x", "reference_style": "mla"})
        # 无 harness 时 503 说明请求体已通过校验；坏值必须在校验层就 422
        assert ok.status_code == 503
        assert bad.status_code == 422

    def test_omitted_field_keeps_config_style(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        with _client(tmp_path, monkeypatch) as client:
            response = client.post("/v1/optimize", json={"raw_output": RAW, "schema": "paper"})
        assert response.status_code == 200
        assert response.json()["final_output"]["references"][0].endswith("[C]//CVPR. 2023.")

    def test_audit_records_the_effective_style(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """审计要能回答"这条输出按哪种格式排的"，翻记录的人不必再猜配置。"""
        with _client(tmp_path, monkeypatch, audit=True) as client:
            client.post("/v1/optimize", json={"raw_output": RAW, "schema": "paper", "reference_style": "apa"})
            client.post("/v1/optimize", json={"raw_output": RAW, "schema": "paper"})
        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["reference_style"] for line in lines] == ["apa", "gb-t-7714"]
