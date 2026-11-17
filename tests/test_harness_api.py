from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from app.harness import ApiHarness, build_harness
from app.harness.api import DEFAULT_SYSTEM_PROMPT
from app.harness.base import HarnessBusy, HarnessError, HarnessTimeout
from app.providers.base import BaseProvider, ChatResponse
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.settings import Settings

URL = "https://api.openai.com/v1/chat/completions"


def completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        },
    )


def make(**kwargs) -> ApiHarness:
    provider = OpenAICompatibleProvider(api_key="sk-test", model="gpt-4o-mini")
    return ApiHarness(provider, **kwargs)


class GateProvider(BaseProvider):
    """把调用挂起，用来确定性触发并发闸门。"""

    name = "gate"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, **kwargs) -> ChatResponse:
        self.entered.set()
        await self.release.wait()
        return ChatResponse(text="ok")


class TestApiHarness:
    @respx.mock
    async def test_success_shape_never_fakes_exit_code(self):
        respx.post(URL).mock(return_value=completion("最终回答"))
        harness = make()
        try:
            run = await harness.run(task="写一段", timeout_s=10)
        finally:
            await harness.aclose()
        assert run.text == "最终回答"
        assert run.kind == "api"
        assert run.exit_code is None, "无进程形态不得伪造成退出码 0"
        assert run.source == "chat_completions"
        assert run.target == "gpt-4o-mini"
        assert run.bytes == len("最终回答".encode())
        audit = run.as_audit(harness.name)
        assert audit["kind"] == "api" and audit["exit_code"] is None and audit["name"] == "openai-api"

    @respx.mock
    async def test_task_is_user_message_and_system_prompt_is_constant(self):
        route = respx.post(URL).mock(return_value=completion("ok"))
        harness = make()
        try:
            await harness.run(task="任务正文", timeout_s=10)
        finally:
            await harness.aclose()
        body = json.loads(route.calls.last.request.content)
        assert body["messages"] == [
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": "任务正文"},
        ]

    @respx.mock
    async def test_rate_limit_becomes_harness_error(self):
        respx.post(URL).mock(return_value=httpx.Response(429, json={"error": "slow down"}))
        harness = make()
        try:
            with pytest.raises(HarnessError, match="限流"):
                await harness.run(task="x", timeout_s=10)
        finally:
            await harness.aclose()

    @respx.mock
    async def test_timeout_becomes_harness_timeout(self):
        respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        harness = make()
        try:
            with pytest.raises(HarnessTimeout):
                await harness.run(task="x", timeout_s=1)
        finally:
            await harness.aclose()

    @respx.mock
    async def test_empty_content_is_rejected(self):
        respx.post(URL).mock(return_value=completion("   "))
        harness = make()
        try:
            with pytest.raises(HarnessError, match="空内容"):
                await harness.run(task="x", timeout_s=10)
        finally:
            await harness.aclose()

    @respx.mock
    async def test_oversized_answer_is_truncated(self):
        respx.post(URL).mock(return_value=completion("结" * 500))
        harness = make(max_output_bytes=1024)
        try:
            run = await harness.run(task="x", timeout_s=10)
        finally:
            await harness.aclose()
        assert run.truncated is True
        assert run.bytes == len("结".encode()) * 500
        assert len(run.text.encode("utf-8")) <= 1024

    async def test_concurrency_gate(self):
        provider = GateProvider()
        harness = ApiHarness(provider, max_concurrency=1)
        first = asyncio.create_task(harness.run(task="a", timeout_s=10))
        await provider.entered.wait()
        with pytest.raises(HarnessBusy):
            await harness.run(task="b", timeout_s=10)
        provider.release.set()
        assert (await first).text == "ok"


class TestRegistration:
    def test_api_profile_absent_without_key(self):
        settings = Settings(_env_file=None, harness_enabled=True, harness_detect=False, openai_api_key="")
        assert build_harness(settings) is None

    def test_api_profile_present_with_key(self):
        settings = Settings(
            _env_file=None,
            harness_enabled=True,
            harness_detect=False,
            openai_api_key="sk-x",
            openai_model="m1",
        )
        harness = build_harness(settings)
        assert isinstance(harness, ApiHarness)
        assert harness.target == "m1"

    def test_providers_filter_can_exclude_api(self):
        settings = Settings(
            _env_file=None,
            harness_enabled=True,
            harness_detect=False,
            openai_api_key="sk-x",
            harness_providers="codex",
        )
        assert build_harness(settings) is None

    def test_disabled_master_switch_wins(self):
        settings = Settings(_env_file=None, harness_enabled=False, harness_detect=True, openai_api_key="sk-x")
        assert build_harness(settings) is None
