from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app.optimizer.context import StepContext  # noqa: E402
from app.providers.mock import MockProvider  # noqa: E402
from app.settings import PipelineConfig, Settings  # noqa: E402

PROXY_VARS = (
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定测试环境：清空代理变量（本机 NO_PROXY 含 IPv6 字面量会让 httpx 建连失败），
    并屏蔽开发者本地 `.env` 对默认配置的干扰。"""
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPTIMIZE_ENV_FILE", "dev/null")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(provider="mock", audit_path=str(tmp_path / "audit.jsonl"))


@pytest.fixture
def config() -> PipelineConfig:
    return PipelineConfig()


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def make_ctx(settings: Settings, config: PipelineConfig):
    def _make(**kwargs):
        kwargs.setdefault("settings", settings)
        kwargs.setdefault("config", config)
        return StepContext(**kwargs)

    return _make
