"""``schema`` 步骤：JSON 解析（含剥离 Markdown 围栏重试）与 Pydantic 校验。"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ValidationError

from app.optimizer.context import StepContext
from app.optimizer.steps import register_step
from app.optimizer.steps.base import BaseStep, StepResult
from app.schemas import parse_model_text, required_field_names
from app.textio import extract_json, strip_markdown_fences


def is_filled(value: Any) -> bool:
    return value not in (None, "", [], {}, ())


def alias_keys(model_cls: type[BaseModel], name: str) -> list[str]:
    field_info = model_cls.model_fields[name]
    keys = [name]
    if field_info.alias:
        keys.append(str(field_info.alias))
    alias = field_info.validation_alias
    if isinstance(alias, str):
        keys.append(alias)
    elif alias is not None:
        keys.extend(str(choice) for choice in getattr(alias, "choices", []) or [str(alias)])
    return keys


def allowed_keys(model_cls: type[BaseModel]) -> set[str]:
    allowed: set[str] = set()
    for name in model_cls.model_fields:
        allowed.update(alias_keys(model_cls, name))
    return allowed


def value_for(model_cls: type[BaseModel], data: dict[str, Any], name: str) -> Any:
    for key in alias_keys(model_cls, name):
        if key in data:
            return data[key]
    return None


@register_step
class SchemaStep(BaseStep):
    name = "schema"

    async def run(self, ctx: StepContext) -> StepResult:
        started = time.perf_counter()
        ctx.errors.clear()
        model_cls = ctx.schema

        if model_cls is None:
            data = extract_json(ctx.raw_output)
            if isinstance(data, dict):
                ctx.parsed, ctx.model, ctx.text = data, None, None
            else:
                ctx.parsed, ctx.model = None, None
                ctx.text = strip_markdown_fences(ctx.raw_output).strip()
            ctx.valid = bool(ctx.parsed) or bool((ctx.text or "").strip())
            return StepResult(
                name=self.name,
                passed=ctx.valid,
                score=1.0 if ctx.valid else 0.0,
                reason="" if ctx.valid else "输出为空文本",
                ms=self.elapsed_ms(started),
            )

        data = extract_json(ctx.raw_output)
        if not isinstance(data, dict):
            data = parse_model_text(model_cls, ctx.raw_output)
        if not isinstance(data, dict):
            ctx.parsed, ctx.model, ctx.valid = None, None, False
            ctx.text = strip_markdown_fences(ctx.raw_output).strip()
            ctx.errors.append("output_not_json")
            return StepResult(
                name=self.name,
                passed=False,
                score=0.0,
                reason="输出不是可解析的 JSON 对象（剥离围栏后仍未取得对象结构）",
                ms=self.elapsed_ms(started),
            )

        extras = sorted(str(key) for key in set(data) - allowed_keys(model_cls))
        strict = bool(self.config is not None and self.config.strict)
        if extras and strict:
            ctx.parsed, ctx.model, ctx.valid = None, None, False
            ctx.errors.append("extra_fields")
            return StepResult(
                name=self.name,
                passed=False,
                score=0.0,
                reason=f"strict 模式下存在 Schema 之外的字段：{'、'.join(extras)}",
                ms=self.elapsed_ms(started),
            )

        try:
            model = model_cls.model_validate(data)
        except ValidationError as exc:
            ctx.model, ctx.parsed, ctx.valid = None, None, False
            messages: list[str] = []
            for error in exc.errors()[:5]:
                location = ".".join(str(part) for part in error["loc"]) or "body"
                messages.append(f"{location}: {error['msg']}")
                ctx.errors.append(location)
            return StepResult(
                name=self.name,
                passed=False,
                score=round(self._required_coverage(model_cls, data), 3),
                reason="；".join(messages),
                ms=self.elapsed_ms(started),
            )

        ctx.model = model
        ctx.parsed = model.model_dump()
        ctx.text = None
        ctx.valid = True
        reason = ""
        if extras:
            reason = f"剔除多余字段：{'、'.join(extras)}"
            ctx.changes.append(f"schema:drop_extra({'、'.join(extras)})")
        return StepResult(name=self.name, passed=True, score=1.0, reason=reason, ms=self.elapsed_ms(started))

    @staticmethod
    def _required_coverage(model_cls: type[BaseModel], data: dict[str, Any]) -> float:
        required = required_field_names(model_cls) or []
        if not required:
            return 0.0
        hits = sum(1 for name in required if is_filled(value_for(model_cls, data, name)))
        return hits / len(required)
