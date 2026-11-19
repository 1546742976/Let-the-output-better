"""Anthropic 原生协议适配器：``/messages``（Messages API）。

与 OpenAI 兼容面的三处差异：鉴权走 ``x-api-key``＋``anthropic-version`` 头；
system 是顶层字段而非 message；没有 ``response_format``，json_mode 只能把
"只输出 JSON" 并进 system 提示——语义对齐调用方期望，不假装协议支持。
"""

from __future__ import annotations

import httpx

from app.providers.base import BaseProvider, ChatResponse, ProviderError, ProviderTimeout

JSON_ONLY = "只输出一个 JSON 对象本身，不带围栏、注释或客套说明。"


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com/v1",
        max_tokens: int = 4096,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
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
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
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
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "system": f"{system}\n{JSON_ONLY}" if json_mode else system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            response = await self.client.post("/messages", json=payload, timeout=timeout_s)
        except httpx.InvalidURL as exc:
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
            text = data["content"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("模型响应结构不符合 Anthropic Messages 协议") from exc
        usage = data.get("usage") or {}
        return ChatResponse(
            text=text or "",
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
        )

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None
