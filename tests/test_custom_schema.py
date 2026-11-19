"""自定义 Pydantic 模型的可用性：README §7.1/§7.3 宣称支持，行为必须配得上这句话。

这批用例只断言外部可见结果（是否报错、分数是否存在、维度取值），不比快照字节；
把 `required_field_names` 换回内置模型才有的 `required_fields()`，两条立即变红。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from app.optimizer import optimize
from app.schemas import describe_model, optional_field_names, parse_model_text, required_field_names, resolve_schema


class Book(BaseModel):
    """一个没有继承 AcademicModel 的普通模型：只有 Pydantic 通用面。"""

    title: str
    pages: int | None = None


class Nested(BaseModel):
    kind: str = Field(validation_alias="type")
    items: list[str] = []


def test_non_pydantic_schema_raises_at_entry():
    """传错类型是调用方的编程错误，要在入口炸，而不是被 fail-open 伪装成一次降级。"""

    class Legacy:
        pass

    with pytest.raises(TypeError):
        resolve_schema(Legacy)


def test_generic_field_names_cover_plain_models():
    assert required_field_names(Book) == ["title"]
    assert optional_field_names(Book) == ["pages"]
    assert required_field_names(None) is None
    assert optional_field_names(object) is None, "不是 Pydantic 模型要报不可测，不是报空必填"


def test_describe_model_falls_back_to_json_schema():
    hint = describe_model(Book)
    assert '"title"' in hint
    assert describe_model(None).startswith("（未指定 Schema")


def test_parse_model_text_returns_none_without_custom_parser():
    assert parse_model_text(Book, "He K. Deep residual learning. CVPR 2016.") is None


@pytest.mark.asyncio
async def test_custom_model_is_scored_and_valid(settings):
    result = await optimize('{"title":"D","pages":320}', schema=Book, input_text="《D》共 320 页。", settings=settings)
    assert result.errors == [], f"步骤异常被吞掉了：{result.errors}"
    assert result.valid is True
    assert result.score is not None, "自定义模型此前整块评分不可用（score=None）"
    assert result.dimensions["completeness"] == 1.0


@pytest.mark.asyncio
async def test_custom_model_partial_fill_lowers_completeness(settings):
    """必填齐、选填缺 → 完整度按 0.7/0.3 折算，而不是把自定义模型一律算满分。"""
    result = await optimize('{"title":"D"}', schema=Book, settings=settings)
    assert result.valid is True
    assert result.errors == [], f"步骤异常被吞掉了：{result.errors}"
    assert result.dimensions["completeness"] == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_custom_model_invalid_output_is_not_silently_scored(settings):
    """校验不过时 structured 维度不出现（走散文口径），但绝不许留下被吞掉的步骤异常。"""
    result = await optimize('{"pages":10}', schema=Book, settings=settings)
    assert result.valid is False
    assert "step_error(rules)" not in result.errors, "步骤异常被吞掉＝评分静默失效"
    assert result.degraded is False


@pytest.mark.asyncio
async def test_alias_keys_work_for_plain_models(settings):
    """内置模型的别名键能力来自 model_fields，自定义模型同样该享受。"""
    result = await optimize('{"type":"paper","items":["a"]}', schema=Nested, settings=settings)
    assert result.valid is True
    assert result.errors == []
    assert result.dimensions["completeness"] == pytest.approx(1.0)
    # 只给必填时按 0.7/0.3 折算，不是"必填齐就满分"
    partial = await optimize('{"type":"paper"}', schema=Nested, settings=settings)
    assert partial.dimensions["completeness"] == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_unmeasurable_schema_does_not_poison_the_score(settings):
    """不可测 ≠ 0 分：维度必须被剔除并按剩余权重重归一，否则自定义模型会被凭空扣分。

    非 Pydantic 类走不到管线（入口已 TypeError），所以这里直接构造上下文。
    """
    from app.optimizer.context import StepContext
    from app.optimizer.steps.rules import RulesStep
    from app.settings import PipelineConfig, StepConfig

    ctx = StepContext(
        raw_output='{"whatever":1}',
        settings=settings,
        config=PipelineConfig(),
        schema=object,
        parsed={"whatever": 1},
        valid=True,
        input_text="whatever 1",
    )
    result = await RulesStep(StepConfig(name="rules"), settings).run(ctx)
    assert ctx.dimensions["completeness"] is None
    assert "不可测" in result.reason
    assert ctx.score is not None and 0.0 < ctx.score <= 1.0
