from __future__ import annotations

from app.optimizer.steps.rules import RulesStep, citation_is_grounded, normalize_text, numeric_tokens
from app.plain import report
from app.schemas import Experiment, Paper
from app.settings import PROSE_WEIGHTS, STRUCTURED_WEIGHTS, StepConfig

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

DEFENSIVE_PROSE = (
    "值得注意的是，实验结果充分表明，本文方法在 3 个数据集上取得了 12.5% 的提升，具有显著的性能优势和广阔的应用前景。"
)
PLAIN_PROSE = "实验表明，本文方法在 3 个数据集上取得了 12.5% 的提升。"
DEFENSIVE_ABSTRACT = "值得注意的是，实验结果充分表明，本方法具有广阔的应用前景。"


def rules_config(**updates) -> StepConfig:
    return StepConfig(name="rules", **updates)


class TestStructuredDimensions:
    async def test_scoring_is_reproducible(self, make_ctx, settings):
        first = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=True, input_text=SOURCE)
        second = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=True, input_text=SOURCE)
        one = await RulesStep(rules_config(), settings).run(first)
        two = await RulesStep(rules_config(), settings).run(second)
        assert (one.score, one.passed, first.dimensions) == (two.score, two.passed, second.dimensions)

    async def test_all_four_dimensions_at_full_mark(self, make_ctx, settings):
        ctx = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=True, input_text=SOURCE)
        result = await RulesStep(rules_config(), settings).run(ctx)
        assert set(ctx.dimensions) == {"completeness", "format", "grounding", "plainness"}
        assert all(value == 1.0 for value in ctx.dimensions.values())
        assert result.score == 1.0 and result.passed

    async def test_grounding_is_skipped_without_source_text(self, make_ctx, settings):
        ctx = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=True, input_text="")
        result = await RulesStep(rules_config(), settings).run(ctx)
        assert ctx.dimensions["grounding"] is None
        assert "未提供原始材料，一致性维度不参与计分" in result.reason
        assert result.score == 1.0

    async def test_hallucinated_number_lowers_score(self, make_ctx, settings):
        parsed = dict(PAPER, year=1999)
        ctx = make_ctx(raw_output="", schema=Paper, parsed=parsed, valid=True, input_text=SOURCE)
        result = await RulesStep(rules_config(), settings).run(ctx)
        assert ctx.dimensions["grounding"] < 1.0
        assert result.score < 1.0

    async def test_defensive_abstract_lowers_plainness_only(self, make_ctx, settings):
        """结构三维不变，只因叙述变防御而扣分——这就是新增那一维的作用。"""
        ctx = make_ctx(
            raw_output="", schema=Paper, parsed={**PAPER, "abstract": DEFENSIVE_ABSTRACT}, valid=True, input_text=SOURCE
        )
        result = await RulesStep(rules_config(), settings).run(ctx)
        assert ctx.dimensions["completeness"] == 1.0
        assert ctx.dimensions["format"] == 1.0
        assert ctx.dimensions["grounding"] == 1.0
        assert ctx.dimensions["plainness"] < 0.5
        assert result.score == 0.833

    async def test_weighted_average_uses_documented_structured_ratios(self, make_ctx, settings):
        parsed = {**PAPER, "abstract": DEFENSIVE_ABSTRACT}
        ctx = make_ctx(raw_output="", schema=Paper, parsed=parsed, valid=True, input_text=SOURCE)
        await RulesStep(rules_config(), settings).run(ctx)
        dims = ctx.dimensions
        expected = sum(STRUCTURED_WEIGHTS[key] * value for key, value in dims.items()) / sum(
            STRUCTURED_WEIGHTS.values()
        )
        assert ctx.score == round(expected, 3)


class TestProseDimensions:
    async def test_defensive_prose_scores_far_below_plain_prose(self, make_ctx, settings):
        defensive = make_ctx(raw_output=DEFENSIVE_PROSE, text=DEFENSIVE_PROSE, schema=None, valid=True, input_text="")
        plain = make_ctx(raw_output=PLAIN_PROSE, text=PLAIN_PROSE, schema=None, valid=True, input_text="")
        await RulesStep(rules_config(), settings).run(defensive)
        await RulesStep(rules_config(), settings).run(plain)
        assert defensive.dimensions["plainness"] == 0.0
        assert plain.dimensions["plainness"] == 1.0
        assert plain.score - defensive.score > 0.5

    async def test_prose_dimensions_exclude_structural_keys(self, make_ctx, settings):
        ctx = make_ctx(raw_output=PLAIN_PROSE, text=PLAIN_PROSE, schema=None, valid=True, input_text="")
        await RulesStep(rules_config(), settings).run(ctx)
        assert set(ctx.dimensions) == {"plainness", "concision", "preservation"}
        assert ctx.dimensions["preservation"] is None, "没有原始材料时一致性维度不参与计分"

    async def test_prose_weighted_average_uses_documented_ratios(self, make_ctx, settings):
        ctx = make_ctx(raw_output=DEFENSIVE_PROSE, text=DEFENSIVE_PROSE, schema=None, valid=True, input_text="")
        await RulesStep(rules_config(), settings).run(ctx)
        scored = {key: value for key, value in ctx.dimensions.items() if value is not None}
        expected = sum(PROSE_WEIGHTS[key] * value for key, value in scored.items()) / sum(
            PROSE_WEIGHTS[key] for key in scored
        )
        assert ctx.score == round(expected, 3)

    async def test_reason_carries_the_language_summary(self, make_ctx, settings):
        ctx = make_ctx(raw_output=DEFENSIVE_PROSE, text=DEFENSIVE_PROSE, schema=None, valid=True, input_text="")
        result = await RulesStep(rules_config(), settings).run(ctx)
        assert "防御措辞" in result.reason and "句均" in result.reason

    async def test_citation_text_is_not_judged_by_plainness(self, make_ctx, settings):
        """引用串按规范写就，用通俗度标准衡量它是错的度量。"""
        parsed = dict(
            PAPER,
            references=["Zhang S, Li S. 由于数据量较大因此使得实验在计算资源的基础上进行了相应的优化[C]//CVPR. 2023."],
        )
        ctx = make_ctx(raw_output="", schema=Paper, parsed=parsed, valid=True, input_text=SOURCE)
        await RulesStep(rules_config(), settings).run(ctx)
        assert ctx.dimensions["plainness"] == report(PAPER["abstract"]).plainness


class TestWeightOverrides:
    async def test_grouped_weights_from_step_config_are_honored(self, make_ctx, settings):
        ctx = make_ctx(raw_output=DEFENSIVE_PROSE, text=DEFENSIVE_PROSE, schema=None, valid=True, input_text="")
        await RulesStep(
            rules_config(weights={"prose": {"plainness": 1.0, "concision": 0.0, "preservation": 0.0}}), settings
        ).run(ctx)
        assert ctx.score == ctx.dimensions["plainness"]

    async def test_grouped_weights_apply_per_scenario(self, make_ctx, settings):
        """同一份覆盖只作用于它那一组：改 prose 不该动结构化得分。"""
        weights = {
            "prose": {"plainness": 1.0, "concision": 0.0, "preservation": 0.0},
            "structured": {"completeness": 1.0, "format": 0.0, "grounding": 0.0, "plainness": 0.0},
        }
        prose_ctx = make_ctx(raw_output=DEFENSIVE_PROSE, text=DEFENSIVE_PROSE, schema=None, valid=True, input_text="")
        struct_ctx = make_ctx(raw_output="", schema=Paper, parsed=PAPER, valid=True, input_text=SOURCE)
        await RulesStep(rules_config(weights=weights), settings).run(prose_ctx)
        await RulesStep(rules_config(weights=weights), settings).run(struct_ctx)
        assert prose_ctx.score == prose_ctx.dimensions["plainness"]
        assert struct_ctx.score == 1.0

    def test_documented_default_weights(self):
        assert PROSE_WEIGHTS == {"plainness": 0.5, "concision": 0.3, "preservation": 0.2}
        assert STRUCTURED_WEIGHTS == {"completeness": 0.3, "format": 0.25, "grounding": 0.25, "plainness": 0.2}


class TestStructuralHelpers:
    async def test_completeness_counts_required_more_than_optional(self, make_ctx, settings):
        ctx = make_ctx(raw_output="", schema=Paper, parsed={"title": "T"}, valid=True, input_text="《T》。")
        await RulesStep(rules_config(), settings).run(ctx)
        assert ctx.dimensions["completeness"] == 0.7

    async def test_format_dimension_rejects_unsplit_author_string(self, make_ctx, settings):
        ctx = make_ctx(
            raw_output="",
            schema=Paper,
            parsed=dict(PAPER, authors=["张三、李四"]),
            valid=True,
            input_text=SOURCE,
        )
        await RulesStep(rules_config(), settings).run(ctx)
        assert ctx.dimensions["format"] < 1.0

    async def test_format_dimension_checks_percent_bounds(self, make_ctx, settings):
        ctx = make_ctx(
            raw_output="",
            schema=Experiment,
            parsed={"method": "M", "metric": "Acc", "value": 142.0, "unit": "%"},
            valid=True,
            input_text="M 的 Acc 为 142。",
        )
        await RulesStep(rules_config(), settings).run(ctx)
        assert ctx.dimensions["format"] < 1.0

    async def test_invalid_output_cannot_pass_rules_even_with_high_scores(self, make_ctx, settings):
        ctx = make_ctx(raw_output="", schema=Paper, parsed=dict(PAPER), valid=False, input_text=SOURCE)
        result = await RulesStep(rules_config(), settings).run(ctx)
        assert result.passed is False
        assert result.reason == "Schema 校验未通过"


def test_numeric_tokens_ignore_single_digits():
    assert numeric_tokens(["3 个数据集中的 12.5% 提升"]) == ["12.5"]
    assert numeric_tokens([2023]) == ["2023"]


def test_normalize_text_drops_punctuation_and_case():
    assert normalize_text("CVPR 2023，张三") == "cvpr2023张三"


def test_citation_grounding_requires_year_and_anchor():
    assert citation_is_grounded("Zhang S, Li S. Deep learning[C]//CVPR. 2023.", normalize_text(SOURCE))
    assert not citation_is_grounded("Unknown A. Some title[C]//ICXX. 2019.", normalize_text(SOURCE))
