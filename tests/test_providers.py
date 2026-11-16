from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.providers import build_provider
from app.providers.base import ProviderError, ProviderTimeout
from app.providers.mock import MockProvider, condense_text, repair_payload
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.settings import Settings

CHAT_URL = "https://api.openai.com/v1/chat/completions"


def completion(content: str, *, prompt_tokens: int = 11, completion_tokens: int = 7) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


class TestOpenAICompatibleProvider:
    @respx.mock
    async def test_success_returns_text_and_usage(self):
        route = respx.post(CHAT_URL).mock(return_value=completion('{"title": "T"}'))
        provider = OpenAICompatibleProvider(api_key="sk-test", model="gpt-4o-mini")
        try:
            response = await provider.chat(system="s", user="u", json_mode=True, timeout_s=5)
        finally:
            await provider.aclose()
        assert response.text == '{"title": "T"}'
        assert (response.prompt_tokens, response.completion_tokens) == (11, 7)
        payload = json.loads(route.calls.last.request.content)
        assert payload["model"] == "gpt-4o-mini"
        assert payload["temperature"] == 0
        assert payload["response_format"] == {"type": "json_object"}
        assert [message["role"] for message in payload["messages"]] == ["system", "user"]

    @respx.mock
    async def test_base_url_switching_needs_no_code_change(self):
        """接入 DeepSeek/Qwen/本地 vLLM 只改 base_url：路径按各自的 host 拼接。"""
        route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(return_value=completion("ok"))
        provider = OpenAICompatibleProvider(api_key="sk-secret", model="m", base_url="https://api.deepseek.com/v1")
        try:
            await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()
        assert route.calls.last.request.headers["authorization"] == "Bearer sk-secret"
        assert route.calls.last.request.url.path == "/v1/chat/completions"

    @respx.mock
    async def test_json_mode_is_omitted_by_default(self):
        route = respx.post(CHAT_URL).mock(return_value=completion("ok"))
        provider = OpenAICompatibleProvider(api_key="sk-test", model="m")
        try:
            await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()
        assert "response_format" not in json.loads(route.calls.last.request.content)

    @respx.mock
    async def test_rate_limit_raises_provider_error(self):
        respx.post(CHAT_URL).mock(return_value=httpx.Response(429, json={"error": "rate limited"}))
        provider = OpenAICompatibleProvider(api_key="sk-test", model="m")
        try:
            with pytest.raises(ProviderError, match="限流"):
                await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()

    @respx.mock
    async def test_server_error_raises_provider_error(self):
        respx.post(CHAT_URL).mock(return_value=httpx.Response(500, text="boom"))
        provider = OpenAICompatibleProvider(api_key="sk-test", model="m")
        try:
            with pytest.raises(ProviderError, match="500"):
                await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()

    @respx.mock
    async def test_timeout_raises_provider_timeout(self):
        respx.post(CHAT_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        provider = OpenAICompatibleProvider(api_key="sk-test", model="m")
        try:
            with pytest.raises(ProviderTimeout):
                await provider.chat(system="s", user="u", timeout_s=0.5)
        finally:
            await provider.aclose()

    @respx.mock
    async def test_connection_error_raises_provider_error(self):
        respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("dns"))
        provider = OpenAICompatibleProvider(api_key="sk-test", model="m")
        try:
            with pytest.raises(ProviderError):
                await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()

    @pytest.mark.parametrize(
        "body",
        [
            {"choices": []},
            {"choices": [{"message": {}}]},
            {"unexpected": True},
            "not json at all",
        ],
    )
    @respx.mock
    async def test_malformed_response_raises_provider_error(self, body):
        response = httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)
        respx.post(CHAT_URL).mock(return_value=response)
        provider = OpenAICompatibleProvider(api_key="sk-test", model="m")
        try:
            with pytest.raises(ProviderError, match="OpenAI 兼容协议"):
                await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()

    @respx.mock
    async def test_missing_usage_defaults_to_zero(self):
        provider = OpenAICompatibleProvider(api_key="sk-test", model="m")
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}))
        try:
            response = await provider.chat(system="s", user="u")
        finally:
            await provider.aclose()
        assert (response.prompt_tokens, response.completion_tokens) == (0, 0)


class TestMockProvider:
    async def test_repair_task_returns_bare_json(self):
        prompt = '任务说明：\n抽取元数据\n\n待修复输出：\n好的\n```json\n{"title": "T"}\n```'
        assert json.loads(repair_payload(prompt)) == {"title": "T"}

    async def test_repair_without_json_yields_empty_object(self):
        assert repair_payload("待修复输出：\n一段没有结构的文字") == "{}"

    async def test_humanize_task_condenses_boilerplate(self):
        prompt = "待改写文本：\n值得注意的是，实验结果充分表明，本文方法具有显著的性能优势和广阔的应用前景。"
        assert condense_text(prompt) == "实验表明，本文方法性能优于对比方法。"

    async def test_humanize_keeps_numbers(self):
        prompt = "待改写文本：\n该方法在 3 个数据集上提升 12.5%。"
        assert condense_text(prompt) == "该方法在 3 个数据集上提升 12.5%。"

    async def test_reply_override_and_call_log(self):
        provider = MockProvider(reply="固定回复")
        response = await provider.chat(system="s", user="u", task="humanize")
        assert response.text == "固定回复"
        assert provider.calls == ["humanize"]

    async def test_generic_task_echoes_prompt(self):
        provider = MockProvider()
        response = await provider.chat(system="s", user="原始材料", task="generate")
        assert response.text == "原始材料"

    async def test_delay_is_awaitable(self):
        provider = MockProvider(delay_s=0.01)
        response = await provider.chat(system="s", user="u")
        assert response.text == "u"


class TestBuildProvider:
    def test_mock_is_default(self):
        assert build_provider(Settings(provider="mock")).name == "mock"

    def test_openai_requires_key(self):
        with pytest.raises(ValidationError):
            Settings(provider="openai", openai_api_key="")

    def test_openai_provider_is_built_from_settings(self):
        provider = build_provider(Settings(provider="openai", openai_api_key="sk-x", openai_model="m2"))
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.model == "m2"
