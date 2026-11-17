"""配置加载：代码默认值 < `.env` < `config/pipeline.yaml`。

密钥只从环境变量读取，`pipeline.yaml` 仅覆盖步骤编排与阈值。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PIPELINE_PATH = Path("config") / "pipeline.yaml"
SUPPORTED_PROVIDERS = ("mock", "openai")
DEFAULT_WEIGHTS = {"completeness": 0.4, "format": 0.3, "grounding": 0.3}
# 开发者本地可能存在 .env；测试通过 OPTIMIZE_ENV_FILE=dev/null 让配置只来自默认值与环境变量。
ENV_FILE = os.environ.get("OPTIMIZE_ENV_FILE", ".env")


class Settings(BaseSettings):
    # 每个字段的 alias 就是 README §8.1 中的环境变量名；populate_by_name 使代码内仍可按字段名构造。
    provider: str = Field(default="mock", alias="PROVIDER")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    score_threshold: float = Field(default=0.8, alias="OPTIMIZE_SCORE_THRESHOLD")
    max_repair_retries: int = Field(default=1, alias="OPTIMIZE_MAX_REPAIR_RETRIES")
    timeout_ms: int = Field(default=8000, alias="OPTIMIZE_TIMEOUT_MS")
    fail_open: bool = Field(default=True, alias="OPTIMIZE_FAIL_OPEN")
    audit_path: str = Field(default="data/audit.jsonl", alias="AUDIT_PATH")

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def _check_provider(self) -> Settings:
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"PROVIDER 仅支持 {SUPPORTED_PROVIDERS}，当前为 {self.provider!r}")
        if self.provider == "openai" and not self.openai_api_key:
            raise ValueError("PROVIDER=openai 必须设置 OPENAI_API_KEY")
        return self


class StepConfig(BaseModel):
    """单个步骤的配置；未列出的键透传给自定义步骤，供其自行取用。"""

    name: str
    enabled: bool = True
    strict: bool = False
    prompt: str | None = None
    weights: dict[str, float] | None = None
    strip_markdown_fences: bool = True
    trim_whitespace: bool = True
    reference_style: Literal["gb-t-7714", "apa"] = "gb-t-7714"
    module: str | None = None

    model_config = ConfigDict(extra="allow")


def default_steps() -> list[StepConfig]:
    return [
        StepConfig(name="schema"),
        StepConfig(name="rules", weights=dict(DEFAULT_WEIGHTS)),
        StepConfig(name="repair", prompt="prompts/repair.txt"),
        StepConfig(name="format", reference_style="gb-t-7714"),
        StepConfig(name="humanize", enabled=False, prompt="prompts/humanize.txt"),
    ]


class PipelineConfig(BaseModel):
    steps: list[StepConfig] = Field(default_factory=default_steps)
    fail_open: bool | None = None
    audit: bool = True
    score_threshold: float | None = None
    max_repair_retries: int | None = None
    timeout_ms: int | None = None

    def step(self, name: str) -> StepConfig | None:
        return next((s for s in self.steps if s.name == name), None)

    def is_enabled(self, name: str) -> bool:
        found = self.step(name)
        return found is not None and found.enabled

    def with_step(self, name: str, **updates: Any) -> PipelineConfig:
        """返回覆盖指定步骤后的新配置，步骤不存在时追加到末尾。"""
        steps = [s.model_copy(update=updates) if s.name == name else s for s in self.steps]
        if all(s.name != name for s in steps):
            steps.append(StepConfig(name=name, **updates))
        return self.model_copy(update={"steps": steps})


def load_pipeline_config(path: str | Path | None = None) -> PipelineConfig:
    config_path = DEFAULT_PIPELINE_PATH if path is None else Path(path)
    if not config_path.exists():
        return PipelineConfig()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return PipelineConfig.model_validate(raw)


def apply_overrides(settings: Settings, config: PipelineConfig) -> Settings:
    """把 `pipeline.yaml` 中的阈值覆盖到环境变量之上。"""
    updates = {
        key: value
        for key, value in {
            "score_threshold": config.score_threshold,
            "max_repair_retries": config.max_repair_retries,
            "timeout_ms": config.timeout_ms,
            "fail_open": config.fail_open,
        }.items()
        if value is not None
    }
    return settings.model_copy(update=updates) if updates else settings


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_pipeline_config() -> PipelineConfig:
    return load_pipeline_config()
