from __future__ import annotations

from app.optimizer.steps.simplify import GROWTH_LIMIT, SimplifyStep, check_facts, fact_tokens
from app.providers.base import BaseProvider, ChatResponse, ProviderError
from app.providers.mock import MockProvider
from app.schemas import Paper
from app.settings import StepConfig

DEFENSIVE = (
    "值得注意的是，实验结果充分表明，本文方法在 3 个数据集上取得了 12.5% 的提升，具有显著的性能优势和广阔的应用前景。"
)
PLAIN = "实验表明，本文方法在 3 个数据集上取得了 12.5% 的提升，性能优于对比方法。"


def step(settings, **updates) -> SimplifyStep:
    return SimplifyStep(StepConfig(name="simplify", **updates), settings)


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
    ctx = make_ctx(raw_output=DEFENSIVE, text=DEFENSIVE)
    assert SimplifyStep().should_run(ctx) is False
    result = await SimplifyStep().run(ctx)
    assert result.passed is True and result.reason == "未配置模型 provider，跳过通俗化"


async def test_prose_is_simplified_and_adopted(make_ctx, provider, settings):
    ctx = make_ctx(raw_output=DEFENSIVE, text=DEFENSIVE, provider=provider, schema=None, valid=True, score=0.4)
    result = await step(settings).run(ctx)
    assert result.passed is True
    assert ctx.text == PLAIN
    assert ctx.changes == ["采纳通俗化重写"]
    assert ctx.markers == []
    # reason 里必须带改前/改后指标，否则审计无法回答"到底变简洁了没有"。
    assert "防御措辞" in result.reason and "→" in result.reason
    assert "plainness" in result.reason


async def test_structured_output_is_left_alone_by_default(make_ctx, provider, settings):
    ctx = make_ctx(
        raw_output="{}",
        schema=Paper,
        parsed={"title": "T", "abstract": DEFENSIVE},
        provider=provider,
        valid=True,
        score=0.4,
    )
    # 闸门在 should_run：管线会先问它，结构化默认进不来。
    assert SimplifyStep().should_run(ctx) is False, "结构化改写默认关闭"
    assert step(settings).should_run(ctx) is False
    assert provider.calls == []


async def test_structured_rewrite_requires_prose_only_off(make_ctx, provider, settings):
    ctx = make_ctx(
        raw_output="{}",
        schema=Paper,
        parsed={"title": "T", "abstract": DEFENSIVE},
        provider=provider,
        valid=True,
        score=0.4,
    )
    result = await step(settings, prose_only=False).run(ctx)
    assert result.passed is True
    assert ctx.parsed["abstract"] == PLAIN
    assert provider.calls == ["simplify"]
    assert ctx.model.abstract == PLAIN


async def test_fabricated_number_discards_the_rewrite(make_ctx, settings):
    ctx = make_ctx(
        raw_output=DEFENSIVE,
        text=DEFENSIVE,
        provider=MockProvider(reply="实验表明，本文方法在 5 个数据集上提升了 99.9%。"),
        schema=None,
        valid=True,
        score=0.4,
    )
    result = await step(settings).run(ctx)
    assert result.passed is False
    assert ctx.text == DEFENSIVE
    assert ctx.markers == ["simplify_rejected"]
    assert "数值变化" in result.reason


async def test_longer_rewrite_is_rejected(make_ctx, settings):
    bloated = DEFENSIVE + "此外还需要说明的是，该方法的应用范围还有待进一步验证。"
    assert len(bloated) > len(DEFENSIVE) * GROWTH_LIMIT
    ctx = make_ctx(
        raw_output=DEFENSIVE,
        text=DEFENSIVE,
        provider=MockProvider(reply=bloated),
        schema=None,
        valid=True,
        score=0.4,
    )
    result = await step(settings).run(ctx)
    assert result.passed is False
    assert ctx.markers == ["simplify_rejected"]
    assert "未变简洁" in result.reason
    assert ctx.text == DEFENSIVE


async def test_empty_rewrite_is_rejected(make_ctx, settings):
    ctx = make_ctx(
        raw_output=DEFENSIVE, text=DEFENSIVE, provider=MockProvider(reply="   "), schema=None, valid=True, score=0.4
    )
    result = await step(settings).run(ctx)
    assert result.passed is False and ctx.text == DEFENSIVE


async def test_provider_failure_keeps_original_text(make_ctx, settings):
    ctx = make_ctx(
        raw_output=DEFENSIVE, text=DEFENSIVE, provider=ExplodingProvider(), schema=None, valid=True, score=0.4
    )
    result = await step(settings).run(ctx)
    assert result.passed is False
    assert ctx.text == DEFENSIVE
    assert "simplify_failed" in ctx.errors


async def test_short_text_is_not_worth_a_model_call(make_ctx, provider, settings):
    ctx = make_ctx(raw_output="结论。", text="结论。", provider=provider, schema=None, valid=True, score=1.0)
    result = await step(settings).run(ctx)
    assert result.passed is True
    assert result.reason == "没有值得改写的叙述文本"
    assert provider.calls == []


async def test_structured_rewrite_skips_identifier_fields(make_ctx, settings):
    ctx = make_ctx(
        raw_output="{}",
        schema=Paper,
        parsed={"doi": "10.1/abc", "title": "很短"},
        provider=MockProvider(reply="不该被调用"),
        valid=True,
        score=0.4,
    )
    targets = SimplifyStep._targets(ctx)
    assert [name for name, _ in targets] == []
