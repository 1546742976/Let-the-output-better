"""管线编排：按 ``pipeline.yaml`` 顺序执行步骤、硬超时、fail-open 与采纳判定。"""

from __future__ import annotations

import asyncio
import importlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from typing import Any, get_args

from app.audit import AuditLog
from app.optimizer.context import StepContext
from app.optimizer.steps import REGISTRY
from app.optimizer.steps.base import BaseStep, StepResult
from app.optimizer.steps.rules import RulesStep
from app.providers import build_provider
from app.providers.base import BaseProvider
from app.schemas import resolve_schema
from app.settings import (
    PipelineConfig,
    Settings,
    StepConfig,
    apply_overrides,
    get_pipeline_config,
    get_settings,
)

#: 单次请求开关 → 管线步骤名。`main.Options` 的字段名、页面开关与这里的键必须一致，
#: 由 tests/test_field_name_contract.py 钉住；此前这份对应关系在 main 与 pipeline
#: 各写一遍，页面把 `score` 写成 `rules` 都没人发现。
OPTION_TO_STEP: dict[str, str] = {
    "score": "rules",
    "repair": "repair",
    "format": "format",
    "simplify": "simplify",
    "strict": "schema",
}

_request_counter = count(1)
#: 进程级随机前缀：同一天多进程（并行评估、多个 uvicorn worker）不再撞号。
_request_nonce = uuid.uuid4().hex[:6]


def new_request_id() -> str:
    """形如 ``req_20261116_a1b2c3_0001``：日期 + 进程 nonce + 进程内序号。

    仅用日期加进程内计数器时，多进程各自从 0001 起会写出完全相同的 id
    （实测 118 条里 13 组重号，最多一条重复 8 次）。补一段进程启动时生成的
    随机 nonce 后，id 在同一审计文件内即可唯一，查看页不再需要靠行号兜底。
    """
    return f"req_{time.strftime('%Y%m%d')}_{_request_nonce}_{next(_request_counter):04d}"


@dataclass
class OptimizeResult:
    request_id: str
    final_output: Any
    valid: bool
    score: float | None
    repaired: bool
    degraded: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    markers: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dimensions: dict[str, float | None] = field(default_factory=dict)
    degraded_reason: str = ""
    ms: int = 0

    def as_response(self) -> dict[str, Any]:
        """HTTP 响应形态（README §7.2）。"""
        return {
            "request_id": self.request_id,
            "final_output": self.final_output,
            "valid": self.valid,
            "score": self.score,
            "repaired": self.repaired,
            "degraded": self.degraded,
            "steps": self.steps,
            "usage": self.usage,
        }


async def _execute_step(
    ctx: StepContext,
    step_class: type[BaseStep],
    config: StepConfig,
    *,
    error_key: str | None = None,
    on_step: Callable[[str, str], None] | None = None,
) -> StepResult:
    """执行一个步骤并隔离异常：单步故障不断链，只记录原因。

    管线里每一次步骤执行都必须走这里。"有的调用走隔离、有的不走"会让同一次运行出现
    两种失败语义——此前 `_remeasure` 在 try 之外直接调 `RulesStep(...).run(ctx)`，
    rules 一抛错就冒到最外层 except，把**已经采纳的改写**连同整条结果退回原文。
    `except Exception` 有意不含 `CancelledError`（它不是 Exception），取消照常上抛。
    ``on_step`` 只上报 start/end 供进度展示，抛错不影响步骤语义（登记处自己兜底）。
    """
    started = time.perf_counter()
    if on_step is not None:
        on_step("start", config.name)
    try:
        return await step_class(config, ctx.settings).run(ctx)
    except Exception as exc:
        ctx.errors.append(error_key or f"step_error({config.name})")
        return StepResult(
            name=config.name,
            passed=False,
            reason=f"步骤异常已隔离：{type(exc).__name__}: {exc}",
            ms=int((time.perf_counter() - started) * 1000),
        )
    finally:
        if on_step is not None:
            on_step("end", config.name)


async def _run_steps(ctx: StepContext, on_step: Callable[[str, str], None] | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for step_config in ctx.config.steps:
        if not step_config.enabled:
            continue
        step_class = REGISTRY.get(step_config.name)
        if step_class is None and step_config.module:
            importlib.import_module(step_config.module)
            step_class = REGISTRY.get(step_config.name)
        if step_class is None:
            ctx.errors.append(f"unknown_step({step_config.name})")
            continue
        step = step_class(step_config, ctx.settings)
        if not step.should_run(ctx):
            continue
        recorded = (await _execute_step(ctx, step_class, step_config, on_step=on_step)).as_dict()
        if step_config.name == "simplify" and "采纳通俗化重写" in ctx.changes:
            # rules 跑在 simplify 之前；改写发生后不重算，score 描述的就只是输入而非交付物。
            status = await _remeasure(ctx)
            if status == "measured":
                recorded["score_after"] = ctx.score
            elif status == "failed":
                # 没重算成功就诚实留空：写改前的分数会把"交付物"说成"输入"。
                recorded["score_after"] = None
        results.append(recorded)
    return results


async def _remeasure(ctx: StepContext) -> str:
    """改写之后重算一次评分，只更新上下文，不再产生第二个 rules 步骤。

    返回 `measured` / `failed` / `skipped`：评分被关闭时压根不该重算（否则"关了评分
    却悄悄算了一次"是另一种静默不一致），失败时保留改前分数并记因，不得牵连改写。
    """
    config = ctx.config.step("rules")
    if config is None or not config.enabled:
        return "skipped"
    key = "remeasure_error(rules)"
    await _execute_step(ctx, RulesStep, config, error_key=key)
    return "failed" if key in ctx.errors else "measured"


def _final_output(ctx: StepContext) -> Any:
    if ctx.parsed is not None:
        return ctx.parsed
    if ctx.text is not None:
        return ctx.text
    return ctx.raw_output


async def optimize(
    raw_output: str,
    *,
    schema: str | type | None = None,
    instruction: str = "",
    input_text: str = "",
    provider: BaseProvider | None = None,
    settings: Settings | None = None,
    config: PipelineConfig | None = None,
    repair: bool | None = None,
    simplify: bool | None = None,
    fmt: bool | None = None,
    score: bool | None = None,
    strict: bool | None = None,
    timeout_ms: int | None = None,
    request_id: str | None = None,
    audit: AuditLog | None = None,
    audit_extra: dict[str, Any] | None = None,
    reference_style: str | None = None,
    on_step: Callable[[str, str], None] | None = None,
) -> OptimizeResult:
    """对一次模型输出执行后置优化；任何异常都以“返回原始输出”收口。"""
    base_settings = settings or get_settings()
    pipeline = get_pipeline_config() if config is None else config
    pipeline = pipeline.model_copy(deep=True)

    # 单次请求的开关直接映射到步骤启用状态，保持对外只有 optimize() 一个入口。
    for name, flag in (("score", score), ("repair", repair), ("format", fmt), ("simplify", simplify)):
        if flag is not None:
            pipeline = pipeline.with_step(OPTION_TO_STEP[name], enabled=bool(flag))
    if strict is not None:
        pipeline = pipeline.with_step(OPTION_TO_STEP["strict"], strict=bool(strict))
    if reference_style is not None:
        # model_copy(update=…) 不做校验：坏值会静默落进 render 的 else 分支，
        # 表现是"选了 APA 却按 GB/T 排"。入口炸掉才是可执行的诊断。
        allowed = get_args(StepConfig.model_fields["reference_style"].annotation)
        if reference_style not in allowed:
            raise ValueError(f"未知 reference_style={reference_style!r}，可选：{', '.join(allowed)}")
        pipeline = pipeline.with_step("format", reference_style=reference_style)

    effective = apply_overrides(base_settings, pipeline)
    if timeout_ms is not None:
        effective = effective.model_copy(update={"timeout_ms": int(timeout_ms)})

    model_cls = resolve_schema(schema)
    timeout_s = max(0.05, effective.timeout_ms / 1000)
    active_provider = provider if provider is not None else build_provider(effective)
    ctx = StepContext(
        raw_output=raw_output,
        settings=effective,
        config=pipeline,
        provider=active_provider,
        schema=model_cls,
        instruction=instruction,
        input_text=input_text,
        timeout_s=timeout_s,
        deadline=time.monotonic() + timeout_s,
    )

    identifier = request_id or new_request_id()
    started = time.perf_counter()
    steps: list[dict[str, Any]] = []
    degraded = False
    degraded_reason = ""
    try:
        steps = await asyncio.wait_for(_run_steps(ctx, on_step), timeout=timeout_s)
    except TimeoutError:
        degraded, degraded_reason = True, f"管线超时（{effective.timeout_ms}ms），回退原始输出"
        ctx.markers.append("timeout")
    except Exception as exc:
        if not effective.fail_open:
            raise
        degraded, degraded_reason = True, f"管线异常（{type(exc).__name__}: {exc}），回退原始输出"
    if degraded and not effective.fail_open:
        raise RuntimeError(degraded_reason)

    if degraded:
        ctx.parsed, ctx.text = None, None
        ctx.markers.append("degraded")

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = OptimizeResult(
        request_id=identifier,
        final_output=_final_output(ctx),
        valid=ctx.valid and not degraded,
        score=ctx.score,
        repaired=ctx.repaired,
        degraded=degraded,
        steps=steps,
        usage=ctx.usage.as_dict(),
        markers=list(ctx.markers),
        changes=list(ctx.changes),
        errors=list(ctx.errors),
        dimensions=dict(ctx.dimensions),
        degraded_reason=degraded_reason,
        ms=elapsed_ms,
    )

    if pipeline.audit:
        log = audit or AuditLog(effective.audit_path)
        format_step = pipeline.step("format")
        record: dict[str, Any] = {
            "request_id": identifier,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "provider": getattr(active_provider, "name", "unknown"),
            "schema": getattr(model_cls, "__name__", None),
            # 记"实际按哪种风格排的"，不是"请求里写了什么"：请求可以不带这个键，
            # 而事后翻审计的人要的是能复现那条输出的取值。
            "reference_style": format_step.reference_style if format_step is not None else None,
            "instruction": instruction,
            "input_text": input_text,
            "valid": result.valid,
            "score": result.score,
            "dimensions": result.dimensions,
            "repaired": result.repaired,
            "degraded": result.degraded,
            "degraded_reason": degraded_reason,
            "markers": result.markers,
            "changes": result.changes,
            "errors": result.errors,
            "steps": steps,
            "usage": result.usage,
            "ms": elapsed_ms,
            "raw_output": raw_output,
            "final_output": result.final_output,
        }
        # 调用方（如 harness 驱动）可补充来源信息，但不得覆盖核心键：
        # 冲突键不静默丢弃，而是记进 errors，让丢数据这件事本身可见。
        for key, value in (audit_extra or {}).items():
            if key in record:
                record["errors"] = [*record["errors"], f"audit_extra_key_conflict({key})"]
            else:
                record[key] = value
        log.write(record)
    return result
