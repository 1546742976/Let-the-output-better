"""配置加载：代码默认值 < `.env` < `config/pipeline.yaml`。

密钥只从环境变量读取，`pipeline.yaml` 仅覆盖步骤编排与阈值。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.harness.base import HarnessUnavailable
from app.harness.command import resolve_allowed_command


def collect_config_warnings(settings: Settings) -> list[str]:
    """把"配了但静默无效"的配置组合变成可读告警。

    这些都是不报错的错：写进 ``.env`` 就以为自己生效了，实际整项被忽略。做成告警而不是
    启动失败——`HARNESS_PROVIDERS` 拼错只该关掉 `/v1/run`，不该让粘贴路径起不来。
    """
    from app.harness.profiles import PROFILES  # 局部导入：避免模块级触发 harness 包的装配链

    warnings: list[str] = []
    if not settings.harness_enabled:
        return warnings
    known = {profile.name for profile in PROFILES.values()} | {"command"}
    allowed = {name.strip() for name in settings.harness_providers.split(",") if name.strip()}
    unknown = sorted(allowed - known)
    if unknown:
        warnings.append(f"HARNESS_PROVIDERS 含未知 profile：{'、'.join(unknown)}；可选：{'、'.join(sorted(known))}")
    if settings.harness_profile and settings.harness_profile not in known:
        warnings.append(f"HARNESS_PROFILE={settings.harness_profile!r} 不是内置 profile，将退回第一个可用成员")
    if not settings.harness_command and (settings.harness_args or settings.harness_allowed_commands):
        warnings.append("HARNESS_COMMAND 为空，HARNESS_ARGS 与 HARNESS_ALLOWED_COMMANDS 整体不生效")
    if any(token in name.upper() for name in settings.harness_env_list for token in ("KEY", "SECRET", "TOKEN")):
        warnings.append("HARNESS_ENV_ALLOW 含疑似凭据项，密钥不应进入子进程环境")
    return warnings


DEFAULT_PIPELINE_PATH = Path("config") / "pipeline.yaml"
SUPPORTED_PROVIDERS = ("mock", "openai", "anthropic", "gemini", "openrouter")
#: 散文输出的评分口径：去防御化是主目标，结构完整性不适用。
PROSE_WEIGHTS = {"plainness": 0.35, "confidence": 0.3, "concision": 0.2, "preservation": 0.15}
#: 结构化输出的评分口径：保留原有三维，另加一维通俗度。
STRUCTURED_WEIGHTS = {"completeness": 0.3, "format": 0.25, "grounding": 0.25, "plainness": 0.2}
# 开发者本地可能存在 .env；测试通过 OPTIMIZE_ENV_FILE=dev/null 让配置只来自默认值与环境变量。
ENV_FILE = os.environ.get("OPTIMIZE_ENV_FILE", ".env")


class Settings(BaseSettings):
    # 每个字段的 alias 就是 README §8.1 中的环境变量名；populate_by_name 使代码内仍可按字段名构造。
    provider: str = Field(default="mock", alias="PROVIDER")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    anthropic_base_url: str = Field(default="https://api.anthropic.com/v1", alias="ANTHROPIC_BASE_URL")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-5", alias="ANTHROPIC_MODEL")
    gemini_base_url: str = Field(default="https://generativelanguage.googleapis.com/v1beta", alias="GEMINI_BASE_URL")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(default="openai/gpt-4o-mini", alias="OPENROUTER_MODEL")
    score_threshold: float = Field(default=0.8, alias="OPTIMIZE_SCORE_THRESHOLD")
    max_repair_retries: int = Field(default=1, alias="OPTIMIZE_MAX_REPAIR_RETRIES")
    timeout_ms: int = Field(default=8000, alias="OPTIMIZE_TIMEOUT_MS")
    fail_open: bool = Field(default=True, alias="OPTIMIZE_FAIL_OPEN")
    audit_path: str = Field(default="data/audit.jsonl", alias="AUDIT_PATH")

    # harness 驱动默认开启：任务的主路径是"先让 harness 生成，再优化其输出"。
    # 开启不等于可执行——命令/参数仍只能来自这里，未配置任何 harness 成员时
    # build_harness 返回 None，/v1/run 直接 503 并指回 /v1/optimize。
    harness_enabled: bool = Field(default=True, alias="HARNESS_ENABLED")
    harness_command: str = Field(default="", alias="HARNESS_COMMAND")
    harness_args: list[str] = Field(default_factory=list, alias="HARNESS_ARGS")
    harness_allowed_commands: list[str] = Field(default_factory=list, alias="HARNESS_ALLOWED_COMMANDS")
    harness_cwd: str = Field(default="", alias="HARNESS_CWD")
    harness_timeout_ms: int = Field(default=120000, alias="HARNESS_TIMEOUT_MS")
    harness_max_output_bytes: int = Field(default=262144, alias="HARNESS_MAX_OUTPUT_BYTES")
    harness_max_concurrency: int = Field(default=1, alias="HARNESS_MAX_CONCURRENCY")
    harness_env_allow: str = Field(default="", alias="HARNESS_ENV_ALLOW")
    #: 逗号分隔的 profile 白名单（如 "codex,openai-api"）；空表示全部可用。
    harness_providers: str = Field(default="", alias="HARNESS_PROVIDERS")
    #: 默认 profile；未设时取第一个可用的。
    harness_profile: str = Field(default="", alias="HARNESS_PROFILE")
    #: 关闭后完全不做文件系统扫描，只认 .env 里的显式命令。
    harness_detect: bool = Field(default=True, alias="HARNESS_DETECT")
    harness_probe_timeout_ms: int = Field(default=5000, alias="HARNESS_PROBE_TIMEOUT_MS")

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    @field_validator("harness_args", "harness_allowed_commands", mode="before")
    @classmethod
    def _parse_json_list(cls, value: Any) -> list[str]:
        """两个列表用 JSON 数组表达：路径里可能出现空格与反斜杠，按分隔符切会切坏。"""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError as exc:
                raise ValueError(f"应为 JSON 字符串数组，解析失败：{exc}") from exc
            value = parsed
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("应为字符串数组")
        return [item for item in value if item.strip()]

    @property
    def harness_env_list(self) -> list[str]:
        return [name.strip() for name in self.harness_env_allow.split(",") if name.strip()]

    @model_validator(mode="after")
    def _check_provider(self) -> Settings:
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"PROVIDER 仅支持 {SUPPORTED_PROVIDERS}，当前为 {self.provider!r}")
        keys = {
            "openai": ("OPENAI_API_KEY", self.openai_api_key),
            "anthropic": ("ANTHROPIC_API_KEY", self.anthropic_api_key),
            "gemini": ("GEMINI_API_KEY", self.gemini_api_key),
            "openrouter": ("OPENROUTER_API_KEY", self.openrouter_api_key),
        }
        if self.provider in keys and not keys[self.provider][1]:
            raise ValueError(f"PROVIDER={self.provider} 必须设置 {keys[self.provider][0]}")
        return self

    @model_validator(mode="after")
    def _check_harness(self) -> Settings:
        """启用即校验：让"命令不在白名单"这类问题在启动期暴露，而不是第一次调用时。

        ``HARNESS_COMMAND`` 不再是必需项——发现式 profile 与 API 形态同样能提供 harness，
        但一旦显式写了命令，它就必须通过白名单校验。
        """
        if not self.harness_enabled:
            return self
        if self.harness_command:
            try:
                resolve_allowed_command(self.harness_command, self.harness_allowed_commands)
            except HarnessUnavailable as exc:
                # 统一成 ValidationError，与其它配置错误的暴露方式一致。
                raise ValueError(str(exc)) from exc
        if self.harness_cwd and not Path(self.harness_cwd).is_dir():
            raise ValueError(f"HARNESS_CWD 不是已存在的目录：{self.harness_cwd}")
        if self.harness_timeout_ms < 100:
            raise ValueError("HARNESS_TIMEOUT_MS 不得小于 100")
        if self.harness_probe_timeout_ms < 100:
            raise ValueError("HARNESS_PROBE_TIMEOUT_MS 不得小于 100")
        return self


class StepConfig(BaseModel):
    """单个步骤的配置；未列出的键透传给自定义步骤，供其自行取用。"""

    name: str
    enabled: bool = True
    strict: bool = False
    prompt: str | None = None
    weights: dict[str, dict[str, float]] | None = None
    strip_markdown_fences: bool = True
    trim_whitespace: bool = True
    reference_style: Literal["gb-t-7714", "apa"] = "gb-t-7714"
    #: 通俗化只在散文场景生效；结构化字段要改写需在 pipeline.yaml 显式打开。
    prose_only: bool = True
    module: str | None = None

    model_config = ConfigDict(extra="allow")


def default_steps() -> list[StepConfig]:
    return [
        StepConfig(name="schema"),
        StepConfig(name="rules"),
        StepConfig(name="repair", prompt="prompts/repair.txt"),
        StepConfig(name="format", reference_style="gb-t-7714"),
        StepConfig(name="simplify", prompt="prompts/simplify.txt", prose_only=True),
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
