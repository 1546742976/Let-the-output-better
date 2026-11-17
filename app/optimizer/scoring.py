"""候选文本的确定性重评分：只跑 ``schema`` + ``rules``，供采纳闸门比较。"""

from __future__ import annotations

import time

from app.optimizer.context import StepContext
from app.optimizer.steps.rules import RulesStep
from app.optimizer.steps.schema import SchemaStep
from app.settings import StepConfig


def step_config(ctx: StepContext, name: str) -> StepConfig:
    return ctx.config.step(name) or StepConfig(name=name)


async def score_text(ctx: StepContext, text: str) -> StepContext:
    """在独立探针上下文上评分，不改动 ``ctx``。"""
    probe = StepContext(
        raw_output=text,
        settings=ctx.settings,
        config=ctx.config,
        schema=ctx.schema,
        instruction=ctx.instruction,
        input_text=ctx.input_text,
        timeout_s=ctx.timeout_s,
        deadline=time.monotonic() + max(0.0, ctx.remaining_s()),
    )
    await SchemaStep(step_config(ctx, "schema"), ctx.settings).run(probe)
    await RulesStep(step_config(ctx, "rules"), ctx.settings).run(probe)
    return probe
