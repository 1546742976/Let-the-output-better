"""OpenAI 兼容协议适配器：`/chat/completions`。"""

from __future__ import annotations

import httpx

from app.providers.base import BaseProvider, ChatResponse, ProviderError, ProviderTimeout


class OpenAICompatibleProvider(BaseProvider):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owned_client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(30.0),
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._owned_client

    async def chat(
        self,
        *,
        system: str,
        user: str,
        task: str = "generic",
        json_mode: bool = False,
        timeout_s: float = 6.0,
    ) -> ChatResponse:
        payload: dict = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = await self.client.post("/chat/completions", json=payload, timeout=timeout_s)
        except httpx.InvalidURL as exc:
            # 本机代理变量（如含 IPv6 字面量的 NO_PROXY）可能导致 httpx 无法建客户端
            raise ProviderError(f"无法建立 HTTP 客户端，请检查 *_PROXY/NO_PROXY 配置：{exc}") from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"模型调用超时（{timeout_s:.1f}s）") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"模型调用失败：{exc}") from exc
        if response.status_code == 429:
            raise ProviderError("模型调用被限流（429）")
        if response.status_code >= 400:
            raise ProviderError(f"模型服务返回 {response.status_code}")
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("模型响应结构不符合 OpenAI 兼容协议") from exc
        usage = data.get("usage") or {}
        return ChatResponse(
            text=text or "",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None
