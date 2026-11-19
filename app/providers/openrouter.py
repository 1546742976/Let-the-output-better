"""OpenRouter 适配器：协议即 OpenAI 兼容面，差异只在默认地址与计费身份。

不复用 ``provider=openai``＋改 base_url 的写法而单列一个 provider 名：审计与
页面健康灯要能区分"直连 OpenAI"与"经 OpenRouter 路由"，两者的限流与账单口径不同。
"""

from __future__ import annotations

import httpx

from app.providers.openai_compatible import OpenAICompatibleProvider


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(api_key=api_key, model=model, base_url=base_url, client=client)
