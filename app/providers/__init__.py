"""Provider 工厂：按配置构造，切换模型只改环境变量。"""

from __future__ import annotations

from app.providers.anthropic import AnthropicProvider
from app.providers.base import BaseProvider, ChatResponse, ProviderError, ProviderTimeout
from app.providers.gemini import GeminiProvider
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.openrouter import OpenRouterProvider
from app.settings import Settings

__all__ = [
    "BaseProvider",
    "ChatResponse",
    "ProviderError",
    "ProviderTimeout",
    "MockProvider",
    "OpenAICompatibleProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "OpenRouterProvider",
    "build_provider",
]


def build_provider(settings: Settings) -> BaseProvider:
    if settings.provider == "openai":
        return OpenAICompatibleProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
        )
    if settings.provider == "anthropic":
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            base_url=settings.anthropic_base_url,
        )
    if settings.provider == "gemini":
        return GeminiProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            base_url=settings.gemini_base_url,
        )
    if settings.provider == "openrouter":
        return OpenRouterProvider(
            api_key=settings.openrouter_api_key,
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
        )
    return MockProvider()
