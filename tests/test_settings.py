from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.settings import (
    PipelineConfig,
    Settings,
    StepConfig,
    apply_overrides,
    load_pipeline_config,
)
from app.textio import extract_json, strip_markdown_fences, trim_whitespace

FENCED = '好的，以下是结果！\n```json\n{"title": "A"}\n```'


class TestSettings:
    def test_defaults_match_readme(self, monkeypatch):
        for name in (
            "PROVIDER",
            "OPENAI_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
            "OPTIMIZE_SCORE_THRESHOLD",
            "OPTIMIZE_MAX_REPAIR_RETRIES",
            "OPTIMIZE_TIMEOUT_MS",
            "OPTIMIZE_FAIL_OPEN",
            "AUDIT_PATH",
        ):
            monkeypatch.delenv(name, raising=False)
        settings = Settings(_env_file=None)
        assert settings.provider == "mock"
        assert settings.openai_base_url == "https://api.openai.com/v1"
        assert settings.openai_model == "gpt-4o-mini"
        assert settings.score_threshold == 0.8
        assert settings.max_repair_retries == 1
        assert settings.timeout_ms == 8000
        assert settings.fail_open is True
        assert settings.audit_path == "data/audit.jsonl"

    def test_env_overrides_code_defaults(self, monkeypatch):
        monkeypatch.setenv("OPTIMIZE_SCORE_THRESHOLD", "0.55")
        monkeypatch.setenv("PROVIDER", "mock")
        assert Settings().score_threshold == 0.55

    def test_openai_provider_requires_key(self, monkeypatch):
        monkeypatch.setenv("PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "")
        with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
            Settings(_env_file=None)

    def test_unknown_provider_rejected(self):
        with pytest.raises(ValidationError, match="PROVIDER"):
            Settings(provider="anthropic")

    def test_pipeline_yaml_overrides_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("OPTIMIZE_SCORE_THRESHOLD", "0.7")
        config_path = tmp_path / "pipeline.yaml"
        config_path.write_text("steps:\n  - name: schema\nscore_threshold: 0.95\n", encoding="utf-8")
        effective = apply_overrides(Settings(), load_pipeline_config(config_path))
        assert effective.score_threshold == 0.95

    def test_missing_yaml_falls_back_to_defaults(self, tmp_path: Path):
        config = load_pipeline_config(tmp_path / "absent.yaml")
        assert [step.name for step in config.steps] == ["schema", "rules", "repair", "format", "simplify"]
        assert config.is_enabled("simplify") is True
        assert config.is_enabled("schema") is True


class TestPipelineConfig:
    def test_with_step_toggles_enabled_without_mutating_original(self):
        config = PipelineConfig()
        updated = config.with_step("repair", enabled=False)
        assert config.is_enabled("repair") is True
        assert updated.is_enabled("repair") is False

    def test_with_step_appends_unknown_step(self):
        config = PipelineConfig().with_step("ccf_venue", enabled=True)
        assert config.step("ccf_venue") == StepConfig(name="ccf_venue")

    def test_step_config_passes_extra_keys_through(self):
        step = StepConfig.model_validate({"name": "ccf_venue", "list": ["CVPR"]})
        assert step.model_dump()["list"] == ["CVPR"]


class TestTextio:
    def test_strip_fences_wrapping_whole_output(self):
        assert strip_markdown_fences("```json\n{}\n```") == "{}"

    def test_extract_json_from_prose_and_fences(self):
        assert extract_json(FENCED) == {"title": "A"}
        assert extract_json('前缀 {"a": [1, 2]} 后缀') == {"a": [1, 2]}
        assert extract_json("完全没有 JSON") is None

    def test_extract_json_keeps_braces_inside_strings(self):
        assert extract_json('噪声 {"title": "a{b}c"} 噪声') == {"title": "a{b}c"}

    def test_trim_whitespace_collapses_blank_lines(self):
        assert trim_whitespace("  a  \n\n  b \t c \n") == "a\nb c"

    def test_json_roundtrip_of_fenced_sample(self):
        assert json.loads(strip_markdown_fences(FENCED).splitlines()[-1]) == {"title": "A"}
