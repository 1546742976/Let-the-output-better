from __future__ import annotations

from app.optimizer.steps import REGISTRY
from app.optimizer.steps.schema import SchemaStep
from app.schemas import Experiment, Paper, Reference
from app.settings import StepConfig

FENCED_WITH_PROSE = (
    "好的，以下是这篇论文的相关信息，希望对您有帮助！\n"
    '```json\n{"title": "T", "authors": "张三、李四", "year": "2023"}\n```'
)


async def test_schema_step_is_registered_under_expected_name():
    assert REGISTRY["schema"] is SchemaStep


async def test_fenced_json_with_prose_is_parsed(make_ctx):
    ctx = make_ctx(raw_output=FENCED_WITH_PROSE, schema=Paper)
    result = await SchemaStep().run(ctx)
    assert result.passed and result.score == 1.0
    assert ctx.parsed["authors"] == ["张三", "李四"]


async def test_plain_citation_uses_schema_text_parser(make_ctx):
    ctx = make_ctx(
        raw_output="He K, Zhang X. Deep residual learning for image recognition. CVPR 2016: 770-778.",
        schema=Reference,
    )
    result = await SchemaStep().run(ctx)
    assert result.passed
    assert ctx.parsed["venue"] == "CVPR" and ctx.parsed["year"] == 2016


async def test_unparseable_output_fails_and_keeps_text(make_ctx):
    ctx = make_ctx(raw_output="抱歉，我没有找到相关信息。", schema=Paper)
    result = await SchemaStep().run(ctx)
    assert result.passed is False and result.score == 0.0
    assert "output_not_json" in ctx.errors
    assert ctx.text == "抱歉，我没有找到相关信息。"


async def test_type_error_reports_partial_required_coverage(make_ctx):
    ctx = make_ctx(
        raw_output='{"method": "ResNet-50", "metric": "Top-1"}',
        schema=Experiment,
    )
    result = await SchemaStep().run(ctx)
    assert result.passed is False
    assert result.score == round(2 / 3, 3)  # method/metric 有值，value 缺失
    assert "value" in result.reason


async def test_strict_mode_rejects_extra_fields(make_ctx, settings):
    ctx = make_ctx(raw_output='{"title": "T", "note": "客套"}', schema=Paper)
    strict = StepConfig(name="schema", strict=True)
    result = await SchemaStep(strict, settings).run(ctx)
    assert result.passed is False and "note" in result.reason


async def test_lenient_mode_drops_extra_fields_and_records_change(make_ctx, settings):
    ctx = make_ctx(raw_output='{"title": "T", "note": "客套"}', schema=Paper)
    result = await SchemaStep(StepConfig(name="schema", strict=False), settings).run(ctx)
    assert result.passed and "note" in result.reason
    assert ctx.changes == ["schema:drop_extra(note)"]


async def test_without_schema_dict_and_text_are_both_supported(make_ctx):
    as_json = make_ctx(raw_output='{"answer": 42}', schema=None)
    await SchemaStep().run(as_json)
    assert as_json.parsed == {"answer": 42} and as_json.valid

    as_text = make_ctx(raw_output="一段纯文本结论。", schema=None)
    result = await SchemaStep().run(as_text)
    assert result.passed and as_text.parsed is None and as_text.text == "一段纯文本结论。"


async def test_errors_are_cleared_between_runs(make_ctx):
    ctx = make_ctx(raw_output="无法解析", schema=Paper)
    await SchemaStep().run(ctx)
    ctx.raw_output = '{"title": "T"}'
    result = await SchemaStep().run(ctx)
    assert result.passed and ctx.errors == []
