"""Provider 工厂：按配置构造，切换模型只改环境变量。"""

from __future__ import annotations

from app.providers.base import BaseProvider, ChatResponse, ProviderError, ProviderTimeout
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.settings import Settings

__all__ = [
    "BaseProvider",
    "ChatResponse",
    "ProviderError",
    "ProviderTimeout",
    "MockProvider",
    "OpenAICompatibleProvider",
    "build_provider",
]


def build_provider(settings: Settings) -> BaseProvider:
    if settings.provider == "openai":
        return OpenAICompatibleProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
        )
    return MockProvider()
