from __future__ import annotations

from app.optimizer.steps.humanize import HumanizeStep, check_facts, fact_tokens
from app.providers.base import BaseProvider, ChatResponse, ProviderError
from app.providers.mock import MockProvider
from app.schemas import Paper
from app.settings import StepConfig

BOILER = (
    "值得注意的是，实验结果充分表明，本文方法在 3 个数据集上取得了 12.5% 的提升，具有显著的性能优势和广阔的应用前景。"
)
CONCISE = "实验表明，本文方法在 3 个数据集上取得了 12.5% 的提升，性能优于对比方法。"


def step(settings) -> HumanizeStep:
    return HumanizeStep(StepConfig(name="humanize", prompt="prompts/humanize.txt"), settings)


class ExplodingProvider(BaseProvider):
    name = "exploding"

    async def chat(self, **kwargs) -> ChatResponse:
        raise ProviderError("服务不可用")


class TestFactCheck:
    def test_numbers_must_be_identical(self):
        assert check_facts("提升 12.5%", "提升 12.5%") is None
        assert "数值变化" in check_facts("提升 12.5%", "提升 99.9%")

    def test_acronyms_must_survive(self):
        assert "关键表述丢失" in check_facts("CVPR 2023 的结果", "2023 的结果")

    def test_single_numbers_are_counted(self):
        assert fact_tokens("3 个数据集")[0]["3"] == 1


async def test_step_is_skipped_without_provider(make_ctx):
    ctx = make_ctx(raw_output="值得注意的是，本文方法充分表明具有显著的性能优势与广阔的应用前景。", text="x")
    assert HumanizeStep().should_run(ctx) is False
    result = await HumanizeStep().run(ctx)
    assert result.passed is True and result.reason == "未配置模型 provider，跳过重写"


async def test_plain_text_is_condensed_and_adopted(make_ctx, provider, settings):
    ctx = make_ctx(raw_output=BOILER, text=BOILER, provider=provider, schema=None, valid=True, score=1.0)
    result = await step(settings).run(ctx)
    assert result.passed
    assert ctx.text == CONCISE
    assert ctx.markers == []
    assert ctx.usage.prompt_tokens == 0


async def test_fabricated_number_discards_the_rewrite(make_ctx, settings):
    ctx = make_ctx(
        raw_output=BOILER,
        text=BOILER,
        provider=MockProvider(reply="实验表明，本文方法在 5 个数据集上提升了 99.9%。"),
        schema=None,
        valid=True,
        score=1.0,
    )
    result = await step(settings).run(ctx)
    assert result.passed is False
    assert "数值变化" in result.reason
    assert ctx.text == BOILER
    assert ctx.markers == ["humanize_rejected"]


async def test_lost_acronym_discards_the_rewrite(make_ctx, settings):
    ctx = make_ctx(
        raw_output="该结果发表于 CVPR 2023，值得注意。",
        text="该结果发表于 CVPR 2023，值得注意。",
        provider=MockProvider(reply="该结果于 2023 年发表。"),
        schema=None,
        valid=True,
        score=1.0,
    )
    result = await step(settings).run(ctx)
    assert result.passed is False and ctx.markers == ["humanize_rejected"]


async def test_empty_rewrite_is_discarded(make_ctx, settings):
    ctx = make_ctx(
        raw_output=BOILER, text=BOILER, provider=MockProvider(reply="   "), schema=None, valid=True, score=1.0
    )
    result = await step(settings).run(ctx)
    assert result.passed is False and ctx.text == BOILER


async def test_provider_failure_keeps_original_text(make_ctx, settings):
    ctx = make_ctx(raw_output=BOILER, text=BOILER, provider=ExplodingProvider(), schema=None, valid=True, score=1.0)
    result = await step(settings).run(ctx)
    assert result.passed is False
    assert ctx.text == BOILER
    assert "humanize_failed" in ctx.errors


async def test_structured_output_only_rewrites_abstract(make_ctx, provider, settings):
    abstract = "值得注意的是，本文方法充分表明具有显著的性能优势。"
    ctx = make_ctx(
        raw_output="{}",
        schema=Paper,
        parsed={"title": "T", "abstract": abstract},
        provider=provider,
        valid=True,
        score=1.0,
    )
    result = await step(settings).run(ctx)
    assert result.passed
    assert ctx.parsed["abstract"] != abstract
    assert ctx.parsed["title"] == "T"
    assert ctx.model.title == "T"


async def test_short_text_is_not_worth_a_model_call(make_ctx, provider, settings):
    ctx = make_ctx(raw_output="结论。", text="结论。", provider=provider, schema=None, valid=True, score=1.0)
    result = await step(settings).run(ctx)
    assert result.passed and result.reason == "文本段过短，不值得调用模型重写"
    assert provider.calls == []
    assert ctx.text == "结论。"
