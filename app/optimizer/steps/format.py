"""``format`` 步骤：去围栏、去空白、统一日期/数值/单位与引用格式、剔除空字段。

格式化后必须重新通过 Schema 校验，否则保留格式化前结果——格式化不允许引入新错误。
"""

from __future__ import annotations

import re
import time

from pydantic import ValidationError

from app.optimizer.context import StepContext
from app.optimizer.steps import register_step
from app.optimizer.steps.base import BaseStep, StepResult
from app.optimizer.steps.references import normalize_reference
from app.optimizer.steps.schema import is_filled
from app.textio import strip_markdown_fences, trim_whitespace

_DATE = re.compile(r"(?P<year>(?:1[89]|20)\d{2})[年/.](?P<month>\d{1,2})[月/.](?P<day>\d{1,2})日?")
_LIST_STRIP = " .;；、,，"


def normalize_dates(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        groups = match.groupdict()
        return f"{groups['year']}-{int(groups['month']):02d}-{int(groups['day']):02d}"

    return _DATE.sub(replace, text)


@register_step
class FormatStep(BaseStep):
    name = "format"

    async def run(self, ctx: StepContext) -> StepResult:
        started = time.perf_counter()
        config = self.config
        do_fences = config.strip_markdown_fences if config is not None else True
        do_trim = config.trim_whitespace if config is not None else True
        style = config.reference_style if config is not None else "gb-t-7714"
        changes: list[str] = []

        if ctx.parsed is None:
            text = ctx.text if ctx.text is not None else ctx.raw_output
            cleaned = self._clean_text(text, fences=do_fences, trim=do_trim)
            if cleaned != text:
                changes.append("清理围栏与空白")
            ctx.text = cleaned
            ctx.changes.extend(changes)
            return StepResult(
                name=self.name,
                passed=True,
                score=1.0,
                reason="；".join(changes) or "无需格式化",
                ms=self.elapsed_ms(started),
            )

        data = dict(ctx.parsed)
        for key in [name for name, value in data.items() if not is_filled(value)]:
            data.pop(key)
            changes.append(f"剔除空字段({key})")

        for key, value in list(data.items()):
            if isinstance(value, str):
                cleaned = self._clean_text(value, fences=do_fences, trim=do_trim)
                if cleaned != value:
                    data[key] = cleaned
                    changes.append(f"规范化文本({key})")
            elif isinstance(value, list):
                items = []
                for index, item in enumerate(value):
                    if not isinstance(item, str):
                        items.append(item)
                        continue
                    cleaned = self._clean_text(item, fences=do_fences, trim=do_trim)
                    if key == "references":
                        restyled, changed = normalize_reference(cleaned, style)
                        if changed:
                            changes.append(f"引用格式({key}[{index})->{style})")
                        cleaned = restyled
                    elif key in {"authors", "keywords"}:
                        cleaned = cleaned.strip(_LIST_STRIP)
                    items.append(cleaned)
                if items != value:
                    data[key] = items

        if isinstance(data.get("unit"), str):
            unit = data["unit"].strip().replace("％", "%").replace(" %", "%")
            if unit != data["unit"]:
                data["unit"] = unit
                changes.append("统一单位写法")

        if ctx.schema is not None:
            try:
                model = ctx.schema.model_validate(data)
            except ValidationError as exc:
                details = "；".join(
                    f"{'.'.join(str(part) for part in error['loc']) or 'body'}: {error['msg']}"
                    for error in exc.errors()[:3]
                )
                return StepResult(
                    name=self.name,
                    passed=False,
                    score=0.5,
                    reason=f"格式化后校验未通过，保留格式化前结果（{details}）",
                    ms=self.elapsed_ms(started),
                )
            ctx.model = model
            dumped = model.model_dump()
            ctx.parsed = {key: value for key, value in dumped.items() if is_filled(value)}
        else:
            ctx.parsed = data

        ctx.changes.extend(changes)
        return StepResult(
            name=self.name,
            passed=True,
            score=1.0,
            reason="；".join(dict.fromkeys(changes)) or "无需格式化",
            ms=self.elapsed_ms(started),
        )

    @staticmethod
    def _clean_text(text: str, *, fences: bool, trim: bool) -> str:
        cleaned = text
        if fences:
            cleaned = strip_markdown_fences(cleaned)
        if trim:
            cleaned = trim_whitespace(cleaned)
        return normalize_dates(cleaned).strip()
