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
    assert "plainness" in result.reason and "confidence" in result.reason


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


async def test_dropping_a_research_boundary_is_rejected(make_ctx, settings):
    """ "保留真正影响结论的研究边界"只有被强制执行才成立。

    数字全部保留，确保触发的是边界闸门而不是事实闸门。
    """
    before = "值得注意的是，本结论仅适用于横截面数据；这或许只能被视为探索性证据。误差降低 12.5%。"
    overconfident = "本方法将误差降低 12.5%，可普遍适用。"
    ctx = make_ctx(
        raw_output=before,
        text=before,
        provider=MockProvider(reply=overconfident),
        schema=None,
        valid=True,
        score=0.2,
        input_text="实验使用横截面数据，误差由 8.0% 降至 7.15%。",
    )
    result = await step(settings).run(ctx)
    assert result.passed is False
    assert ctx.text == before, "误删边界的改写必须整体丢弃"
    assert ctx.markers == ["simplify_rejected"]
    assert "丢失研究边界" in result.reason


async def test_removing_ungrounded_while_keeping_boundary_is_adopted(make_ctx, settings):
    before = "本结论仅在 3 个数据集上得到验证；这或许只能被视为探索性证据，需谨慎解读。"
    after = "本结论仅在 3 个数据集上得到验证。"
    ctx = make_ctx(
        raw_output=before,
        text=before,
        provider=MockProvider(reply=after),
        schema=None,
        valid=True,
        score=0.2,
        input_text="样本覆盖 3 个数据集（ImageNet、CIFAR-10、COCO）。",
    )
    result = await step(settings).run(ctx)
    assert result.passed is True
    assert ctx.text == after
    assert "｜保留边界：" in result.reason and "｜删除无据：" in result.reason


async def test_boundary_protection_applies_without_source_text(make_ctx, settings):
    """无原文时判定为 unverified，同样受保护——宁可少删。"""
    before = "该结论仅适用于横截面数据，或许还需更多证据。"
    ctx = make_ctx(
        raw_output=before,
        text=before,
        provider=MockProvider(reply="该结论成立，且可推广。"),
        schema=None,
        valid=True,
        score=0.2,
        input_text="",
    )
    result = await step(settings).run(ctx)
    assert result.passed is False
    assert "丢失研究边界" in result.reason


async def test_claim_first_flag_is_reported(make_ctx, settings):
    before = "或许只能被视为探索性证据。本方法将误差降低 12.5%。"
    after = "本方法将误差降低 12.5%。"
    ctx = make_ctx(
        raw_output=before,
        text=before,
        provider=MockProvider(reply=after),
        schema=None,
        valid=True,
        score=0.2,
        input_text="误差由 8.0% 降至 7.0%。",
    )
    result = await step(settings).run(ctx)
    assert "｜主张先行：是" in result.reason


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
