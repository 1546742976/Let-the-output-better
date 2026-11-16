from __future__ import annotations

from app.optimizer.steps.rules import RulesStep, citation_is_grounded, normalize_text, numeric_tokens
from app.schemas import Experiment, Paper
from app.settings import StepConfig

PAPER = {
    "title": "基于深度学习的图像分类方法研究",
    "authors": ["张三", "李四"],
    "year": 2023,
    "venue": "CVPR",
    "keywords": ["深度学习"],
    "abstract": "本文提出一种面向图像分类的深度学习方法。",
    "references": ["Zhang S, Li S. Research on image classification[C]//CVPR. 2023."],
}
SOURCE = "《基于深度学习的图像分类方法研究》，张三、李四，发表于 CVPR 2023。关键词：深度学习。"


def rules_config(**updates) -> StepConfig:
    return StepConfig(name="rules", **updates)


async def test_scoring_is_reproducible(make_ctx, settings):
    first = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=True, input_text=SOURCE)
    second = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=True, input_text=SOURCE)
    one = await RulesStep(rules_config(), settings).run(first)
    two = await RulesStep(rules_config(), settings).run(second)
    assert (one.score, one.passed, first.dimensions) == (two.score, two.passed, second.dimensions)


async def test_all_three_dimensions_at_full_mark(make_ctx, settings):
    ctx = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=True, input_text=SOURCE)
    result = await RulesStep(rules_config(), settings).run(ctx)
    assert ctx.dimensions["completeness"] == 1.0
    assert ctx.dimensions["format"] == 1.0
    assert ctx.dimensions["grounding"] == 1.0
    assert result.score == 1.0 and result.passed


async def test_grounding_is_skipped_without_source_text(make_ctx, settings):
    ctx = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=True, input_text="")
    result = await RulesStep(rules_config(), settings).run(ctx)
    assert ctx.dimensions["grounding"] is None
    assert result.reason == "未提供原始材料，一致性维度不参与计分"
    assert result.score == 1.0


async def test_hallucinated_number_lowers_score(make_ctx, settings):
    parsed = dict(PAPER, year=1999)
    ctx = make_ctx(raw_output="", schema=Paper, parsed=parsed, valid=True, input_text=SOURCE)
    result = await RulesStep(rules_config(), settings).run(ctx)
    assert ctx.dimensions["grounding"] < 1.0
    assert result.score < 1.0


async def test_weights_are_read_from_step_config(make_ctx, settings):
    parsed = dict(PAPER, keywords=[], references=[], authors=[])
    grounded_only = make_ctx(raw_output="", schema=Paper, parsed=parsed, valid=True, input_text=SOURCE)
    await RulesStep(rules_config(weights={"completeness": 0.0, "format": 0.0, "grounding": 1.0}), settings).run(
        grounded_only
    )
    complete_only = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=True, input_text=SOURCE)
    await RulesStep(rules_config(weights={"completeness": 1.0, "format": 0.0, "grounding": 0.0}), settings).run(
        complete_only
    )
    assert grounded_only.score == 1.0
    assert complete_only.score == 1.0


async def test_weighted_average_uses_documented_ratios(make_ctx, settings):
    ctx = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=True, input_text=SOURCE)
    await RulesStep(rules_config(weights={"completeness": 0.4, "format": 0.3, "grounding": 0.3}), settings).run(ctx)
    dims = ctx.dimensions
    expected = 0.4 * dims["completeness"] + 0.3 * dims["format"] + 0.3 * dims["grounding"]
    assert ctx.score == round(expected, 3)


async def test_invalid_output_cannot_pass_rules_even_with_high_scores(make_ctx, settings):
    ctx = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=False, input_text=SOURCE)
    result = await RulesStep(rules_config(), settings).run(ctx)
    assert result.passed is False
    assert result.reason == "Schema 校验未通过"


async def test_completeness_counts_required_more_than_optional(make_ctx, settings):
    sparse = {"title": "T"}
    ctx = make_ctx(raw_output="", schema=Paper, parsed=sparse, valid=True, input_text="《T》。")
    await RulesStep(rules_config(), settings).run(ctx)
    # 必填全中（0.7 × 1），6 个选填字段全空（0.3 × 0）
    assert ctx.dimensions["completeness"] == 0.7


async def test_format_dimension_rejects_unsplit_author_string(make_ctx, settings):
    ctx = make_ctx(
        raw_output="",
        schema=Paper,
        parsed=dict(PAPER, authors=["张三、李四"]),
        valid=True,
        input_text=SOURCE,
    )
    await RulesStep(rules_config(), settings).run(ctx)
    assert ctx.dimensions["format"] < 1.0


async def test_format_dimension_checks_percent_bounds(make_ctx, settings):
    ctx = make_ctx(
        raw_output="",
        schema=Experiment,
        parsed={"method": "M", "metric": "Acc", "value": 142.0, "unit": "%"},
        valid=True,
        input_text="M 的 Acc 为 142。",
    )
    await RulesStep(rules_config(), settings).run(ctx)
    assert ctx.dimensions["format"] < 1.0


def test_numeric_tokens_ignore_single_digits():
    assert numeric_tokens(["3 个数据集中的 12.5% 提升"]) == ["12.5"]
    assert numeric_tokens([2023]) == ["2023"]


def test_normalize_text_drops_punctuation_and_case():
    assert normalize_text("CVPR 2023，张三") == "cvpr2023张三"


def test_citation_grounding_requires_year_and_anchor():
    assert citation_is_grounded("Zhang S, Li S. Deep learning[C]//CVPR. 2023.", normalize_text(SOURCE))
    assert not citation_is_grounded("Unknown A. Some title[C]//ICXX. 2019.", normalize_text(SOURCE))
