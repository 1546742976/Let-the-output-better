"""v0.7 三个适配器：Anthropic Messages、Gemini generateContent、OpenRouter（OpenAI 兼容面）。

断言口径与 test_providers.py 一致：协议字段、鉴权头、usage 映射、限流/超时/坏结构
三类失败语义，以及装配层"缺密钥在启动期炸"。全部走 respx，不触网。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.optimizer import optimize
from app.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenRouterProvider,
    build_provider,
)
from app.providers.base import ProviderError, ProviderTimeout
from app.settings import PipelineConfig, Settings

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def anthropic_reply(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 11, "output_tokens": 7}},
    )


def gemini_reply(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7},
        },
    )


class TestAnthropicProvider:
    @respx.mock
    async def test_success_maps_messages_protocol_and_usage(self):
        route = respx.post(ANTHROPIC_URL).mock(return_value=anthropic_reply('{"title": "T"}'))
        provider = AnthropicProvider(api_key="sk-ant", model="claude-sonnet-4-5")
        try:
            response = await provider.chat(system="s", user="u", timeout_s=5)
        finally:
            await provider.aclose()
        assert response.text == '{"title": "T"}'
        assert (response.prompt_tokens, response.completion_tokens) == (11, 7)
        request = route.calls.last.request
        assert request.headers["x-api-key"] == "sk-ant"
        assert request.headers["anthropic-version"] == "2023-06-01"
        payload = json.loads(request.content)
        assert payload["system"] == "s"
        assert payload["messages"] == [{"role": "user", "content": "u"}]
        assert payload["max_tokens"] > 0
        assert payload["temperature"] == 0

    @respx.mock
    async def test_json_mode_folds_into_system_not_response_format(self):
        route = respx.post(ANTHROPIC_URL).mock(return_value=anthropic_reply("{}"))
        provider = AnthropicProvider(api_key="k", model="m")
        try:
            await provider.chat(system="s", user="u", json_mode=True)
        finally:
            await provider.aclose()
        payload = json.loads(route.calls.last.request.content)
        assert "JSON" in payload["system"]
        assert "response_format" not in payload

    @respx.mock
    async def test_rate_limit_and_server_error_and_timeout(self):
        route = respx.post(ANTHROPIC_URL)
        provider = AnthropicProvider(api_key="k", model="m")
        try:
            route.mock(return_value=httpx.Response(429, json={}))
            with pytest.raises(ProviderError, match="限流"):
                await provider.chat(system="s", user="u")
            route.mock(return_value=httpx.Response(500, text="boom"))
            with pytest.raises(ProviderError, match="500"):
                await provider.chat(system="s", user="u")
            route.mock(side_effect=httpx.ReadTimeout("slow"))
            with pytest.raises(ProviderTimeout):
                await provider.chat(system="s", user="u", timeout_s=0.5)
        finally:
            await provider.aclose()

    @pytest.mark.parametrize("body", [{"content": []}, {"unexpected": True}, "not json"])
    @respx.mock
    async def test_malformed_response_raises_provider_error(self, body):
        response = httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)
        respx.post(ANTHROPIC_URL).mock(return_value=response)
        provider = AnthropicProvider(api_key="k", model="m")
        try:
            with pytest.raises(ProviderError, match="Anthropic Messages"):
                await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()


class TestGeminiProvider:
    @respx.mock
    async def test_success_maps_generate_content_and_usage(self):
        route = respx.post(GEMINI_URL).mock(return_value=gemini_reply("ok"))
        provider = GeminiProvider(api_key="g-key", model="gemini-2.5-flash")
        try:
            response = await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()
        assert response.text == "ok"
        assert (response.prompt_tokens, response.completion_tokens) == (11, 7)
        request = route.calls.last.request
        assert request.headers["x-goog-api-key"] == "g-key"
        assert "key=" not in str(request.url)
        payload = json.loads(request.content)
        assert payload["systemInstruction"]["parts"][0]["text"] == "s"
        assert payload["contents"][0]["parts"][0]["text"] == "u"

    @respx.mock
    async def test_json_mode_uses_response_mime_type(self):
        route = respx.post(GEMINI_URL).mock(return_value=gemini_reply("{}"))
        provider = GeminiProvider(api_key="k", model="gemini-2.5-flash")
        try:
            await provider.chat(system="s", user="u", json_mode=True)
        finally:
            await provider.aclose()
        payload = json.loads(route.calls.last.request.content)
        assert payload["generationConfig"]["responseMimeType"] == "application/json"

    @respx.mock
    async def test_timeout_and_malformed(self):
        route = respx.post(GEMINI_URL)
        provider = GeminiProvider(api_key="k", model="gemini-2.5-flash")
        try:
            route.mock(side_effect=httpx.ReadTimeout("slow"))
            with pytest.raises(ProviderTimeout):
                await provider.chat(system="s", user="u", timeout_s=0.5)
            route.mock(return_value=httpx.Response(200, json={"candidates": []}))
            with pytest.raises(ProviderError, match="Gemini generateContent"):
                await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()


class TestOpenRouterProvider:
    @respx.mock
    async def test_routes_to_openrouter_with_bearer_auth(self):
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )
        )
        provider = OpenRouterProvider(api_key="sk-or", model="openai/gpt-4o-mini")
        try:
            response = await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()
        assert provider.name == "openrouter"
        assert response.text == "ok"
        assert route.calls.last.request.headers["authorization"] == "Bearer sk-or"


class TestBuildProviderV07:
    @pytest.mark.parametrize(
        ("provider", "key_field", "cls"),
        [
            ("anthropic", "anthropic_api_key", AnthropicProvider),
            ("gemini", "gemini_api_key", GeminiProvider),
            ("openrouter", "openrouter_api_key", OpenRouterProvider),
        ],
    )
    def test_built_from_settings_with_key(self, provider, key_field, cls):
        settings = Settings(provider=provider, **{key_field: "k"}, _env_file=None)
        built = build_provider(settings)
        assert isinstance(built, cls)
        assert built.name == provider

    @pytest.mark.parametrize("provider", ["anthropic", "gemini", "openrouter"])
    def test_missing_key_fails_at_startup(self, provider):
        with pytest.raises(ValidationError, match="API_KEY"):
            Settings(provider=provider, _env_file=None)

    @pytest.mark.parametrize("cls", [AnthropicProvider, GeminiProvider, OpenRouterProvider])
    async def test_aclose_closes_owned_client(self, cls):
        provider = cls(api_key="k", model="m")
        client = provider.client
        await provider.aclose()
        assert client.is_closed


class TestPipelineIntegration:
    @respx.mock
    async def test_repair_through_anthropic_yields_parseable_json(self, tmp_path):
        """json_mode 折进 system 后，修复链路拿到的仍必须是裸 JSON。

        关掉评分（score=False）让采纳闸门的基线为 0：否则原文散文分可能高于
        候选结构分，候选被合法拒绝，就测不到"采纳后结构可用"这一段。
        """
        route = respx.post(ANTHROPIC_URL).mock(return_value=anthropic_reply('{"title": "T"}'))
        provider = AnthropicProvider(api_key="k", model="m")
        try:
            result = await optimize(
                '好的\n```json\n{"title": "T",}\n```',
                schema="paper",
                provider=provider,
                score=False,
                settings=Settings(_env_file=None, provider="mock", audit_path=str(tmp_path / "a.jsonl")),
                config=PipelineConfig(audit=False),
            )
        finally:
            await provider.aclose()
        assert route.called
        assert result.repaired is True
        assert result.valid is True
        assert result.final_output["title"] == "T"

    @respx.mock
    async def test_simplify_through_gemini_runs_without_degrading(self, tmp_path):
        route = respx.post(GEMINI_URL).mock(return_value=gemini_reply("实验表明，本文方法性能优于对比方法。"))
        provider = GeminiProvider(api_key="k", model="gemini-2.5-flash")
        try:
            result = await optimize(
                "综上所述，本研究可能在一定程度上具有参考价值，但仍需进一步验证。",
                provider=provider,
                settings=Settings(_env_file=None, provider="mock", audit_path=str(tmp_path / "a.jsonl")),
                config=PipelineConfig(audit=False),
            )
        finally:
            await provider.aclose()
        assert route.called
        assert result.degraded is False
        assert any(step["name"] == "simplify" for step in result.steps)
