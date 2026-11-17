from __future__ import annotations

import time

from app.optimizer.steps.repair import RepairStep
from app.providers.base import BaseProvider, ChatResponse, ProviderError
from app.providers.mock import MockProvider
from app.schemas import Paper
from app.settings import Settings, StepConfig


def step(settings: Settings) -> RepairStep:
    return RepairStep(StepConfig(name="repair", prompt="prompts/repair.txt"), settings)


class ExplodingProvider(BaseProvider):
    name = "exploding"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs) -> ChatResponse:
        self.calls += 1
        raise ProviderError("连接被拒绝")


def _ctx(settings: Settings, **kwargs):
    from app.optimizer.context import StepContext

    kwargs.setdefault("raw_output", "")
    kwargs.setdefault("timeout_s", 8.0)
    kwargs.setdefault("deadline", time.monotonic() + 8.0)
    return StepContext(settings=settings, **kwargs)


class TestTriggerCondition:
    def test_skipped_when_valid_and_above_threshold(self, config, settings):
        ctx = _ctx(settings, config=config, schema=Paper, provider=MockProvider(), valid=True, score=0.95)
        assert RepairStep().should_run(ctx) is False

    def test_runs_on_low_score(self, config, settings):
        ctx = _ctx(settings, config=config, schema=Paper, provider=MockProvider(), valid=True, score=0.4)
        assert RepairStep().should_run(ctx) is True

    def test_runs_on_invalid_output(self, config, settings):
        ctx = _ctx(settings, config=config, schema=Paper, provider=MockProvider(), valid=False, score=None)
        assert RepairStep().should_run(ctx) is True

    def test_never_runs_without_provider(self, config, settings):
        ctx = _ctx(settings, config=config, schema=Paper, provider=None, valid=False, score=None)
        assert RepairStep().should_run(ctx) is False

    def test_never_runs_when_time_budget_is_exhausted(self, config, settings):
        ctx = _ctx(
            settings,
            config=config,
            schema=Paper,
            provider=MockProvider(),
            valid=False,
            deadline=time.monotonic(),
        )
        assert RepairStep().should_run(ctx) is False


async def test_prompt_carries_schema_errors_and_raw_output(config, settings):
    provider = MockProvider(reply='{"title": "T", "year": 2023}')
    ctx = _ctx(
        settings,
        config=config,
        schema=Paper,
        provider=provider,
        valid=False,
        errors=["title: missing"],
        input_text="《T》，2023 年。",
        raw_output="抱歉，我不知道。",
        instruction="抽取论文元数据",
    )
    await step(settings).run(ctx)
    assert provider.calls == ["repair"]


async def test_candidate_beyond_the_gate_is_adopted(config, settings):
    provider = MockProvider(reply='{"title": "T", "year": 2023}')
    ctx = _ctx(
        settings,
        config=config,
        schema=Paper,
        provider=provider,
        valid=False,
        score=None,
        input_text="《T》，2023 年。",
        raw_output="抱歉，我不知道。",
    )
    result = await step(settings).run(ctx)
    assert result.passed is True
    assert ctx.valid is True and ctx.parsed["title"] == "T"
    assert ctx.score is not None and ctx.score >= 0.8
    assert ctx.repaired is True and ctx.markers == []
    assert any("采纳修复结果" in change for change in ctx.changes)


async def test_candidate_with_lower_score_is_rejected(config, settings):
    provider = MockProvider(reply='{"title": "T"}')
    parsed = {
        "title": "T",
        "authors": ["张三"],
        "year": 2023,
        "venue": "CVPR",
        "keywords": ["k"],
        "abstract": "a",
        "references": [],
    }
    ctx = _ctx(
        settings,
        config=config,
        schema=Paper,
        provider=provider,
        parsed=dict(parsed),
        valid=True,
        score=0.95,
        input_text="《T》，张三，CVPR 2023，关键词 k。",
    )
    result = await step(settings).run(ctx)
    assert result.passed is False
    assert ctx.markers == ["repair_rejected"]
    assert ctx.parsed == parsed, "被拒绝的候选不得覆盖原结果"
    assert ctx.repaired is True, "README §7.2：repaired 包含被闸门拒绝的情况"


async def test_candidate_still_invalid_is_rejected(config, settings):
    provider = MockProvider(reply="修复模型也输出了非 JSON 文本")
    ctx = _ctx(settings, config=config, schema=Paper, provider=provider, valid=False, score=None, raw_output="x")
    result = await step(settings).run(ctx)
    assert result.passed is False and ctx.valid is False and ctx.markers == ["repair_rejected"]


async def test_retry_limit_is_respected(config, settings):
    retries = Settings(max_repair_retries=2, audit_path=settings.audit_path)
    provider = MockProvider(reply="仍然不是 JSON")
    ctx = _ctx(retries, config=config, schema=Paper, provider=provider, valid=False, score=None)
    await step(retries).run(ctx)
    assert provider.calls == ["repair", "repair"]
    assert ctx.markers == ["repair_rejected"], "多次尝试只记录一次拒绝标记"


async def test_provider_error_is_contained(config, settings):
    provider = ExplodingProvider()
    ctx = _ctx(
        settings,
        config=config,
        schema=Paper,
        provider=provider,
        valid=False,
        score=None,
        raw_output="原始输出",
    )
    result = await step(settings).run(ctx)
    assert result.passed is False
    assert ctx.repaired is True and "repair_failed" in ctx.errors
    assert ctx.parsed is None and ctx.raw_output == "原始输出"
    assert provider.calls == 1


async def test_usage_tokens_are_accumulated(config, settings):
    class CountingProvider(BaseProvider):
        name = "counting"

        async def chat(self, **kwargs) -> ChatResponse:
            return ChatResponse(text='{"title": "T"}', prompt_tokens=120, completion_tokens=40)

    ctx = _ctx(settings, config=config, schema=Paper, provider=CountingProvider(), valid=False, raw_output="x")
    await step(settings).run(ctx)
    assert ctx.usage.as_dict() == {"prompt_tokens": 120, "completion_tokens": 40}
