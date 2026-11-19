"""Gemini 原生协议适配器：``/models/{model}:generateContent``。

鉴权走 ``x-goog-api-key`` 头（而非 URL query，避免密钥进访问日志）；system 走
``systemInstruction``；json_mode 用 ``generationConfig.responseMimeType``，
这是 Gemini 协议里真实存在的结构化输出开关。
"""

from __future__ import annotations

import httpx

from app.providers.base import BaseProvider, ChatResponse, ProviderError, ProviderTimeout


class GeminiProvider(BaseProvider):
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
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
                headers={"x-goog-api-key": self.api_key},
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
        generation: dict = {"temperature": 0}
        if json_mode:
            generation["responseMimeType"] = "application/json"
        payload: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation,
        }
        try:
            response = await self.client.post(f"/models/{self.model}:generateContent", json=payload, timeout=timeout_s)
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
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("模型响应结构不符合 Gemini generateContent 协议") from exc
        usage = data.get("usageMetadata") or {}
        return ChatResponse(
            text=text or "",
            prompt_tokens=int(usage.get("promptTokenCount") or 0),
            completion_tokens=int(usage.get("candidatesTokenCount") or 0),
        )

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None
