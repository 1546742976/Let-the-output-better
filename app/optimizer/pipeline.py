"""管线编排：按 ``pipeline.yaml`` 顺序执行步骤、硬超时、fail-open 与采纳判定。"""

from __future__ import annotations

import asyncio
import importlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from typing import Any

from app.audit import AuditLog
from app.optimizer.context import StepContext
from app.optimizer.steps import REGISTRY
from app.optimizer.steps.base import StepResult
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

_request_counter = count(1)


def new_request_id() -> str:
    """形如 ``req_20261116_0001``：日期 + 进程内序号。"""
    return f"req_{time.strftime('%Y%m%d')}_{next(_request_counter):04d}"


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


async def _run_steps(ctx: StepContext) -> list[dict[str, Any]]:
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
        started = time.perf_counter()
        try:
            result = await step.run(ctx)
        except Exception as exc:  # 步骤级隔离：单步异常不断链，仅记录原因
            result = StepResult(
                name=step_config.name,
                passed=False,
                reason=f"步骤异常已隔离：{type(exc).__name__}: {exc}",
                ms=int((time.perf_counter() - started) * 1000),
            )
            ctx.errors.append(f"step_error({step_config.name})")
        recorded = result.as_dict()
        if step_config.name == "simplify" and "采纳通俗化重写" in ctx.changes:
            # rules 跑在 simplify 之前；改写发生后不重算，score 描述的就只是输入而非交付物。
            await _remeasure(ctx)
            recorded["score_after"] = ctx.score
        results.append(recorded)
    return results


async def _remeasure(ctx: StepContext) -> None:
    """改写之后重算一次评分，只更新上下文，不再产生第二个 rules 步骤。"""
    config = ctx.config.step("rules") or StepConfig(name="rules")
    await RulesStep(config, ctx.settings).run(ctx)


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
) -> OptimizeResult:
    """对一次模型输出执行后置优化；任何异常都以“返回原始输出”收口。"""
    base_settings = settings or get_settings()
    pipeline = get_pipeline_config() if config is None else config
    pipeline = pipeline.model_copy(deep=True)

    # 单次请求的开关直接映射到步骤启用状态，保持对外只有 optimize() 一个入口。
    for name, flag in (("repair", repair), ("simplify", simplify), ("format", fmt), ("rules", score)):
        if flag is not None:
            pipeline = pipeline.with_step(name, enabled=bool(flag))
    if strict is not None:
        pipeline = pipeline.with_step("schema", strict=bool(strict))

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
        steps = await asyncio.wait_for(_run_steps(ctx), timeout=timeout_s)
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
        record: dict[str, Any] = {
            "request_id": identifier,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "provider": getattr(active_provider, "name", "unknown"),
            "schema": getattr(model_cls, "__name__", None),
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
