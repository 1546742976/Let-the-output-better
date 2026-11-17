"""``rules`` 步骤：字段完整度、格式合规、与输入一致性三维加权计分。

全程不依赖模型调用，同一输入必得同一得分，可单测、可复现（README §2）。
"""

from __future__ import annotations

import datetime as dt
import math
import re
import time
from typing import Any

from app.optimizer.context import StepContext
from app.optimizer.steps import register_step
from app.optimizer.steps.base import BaseStep, StepResult
from app.optimizer.steps.references import is_compliant
from app.optimizer.steps.schema import is_filled
from app.settings import DEFAULT_WEIGHTS

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_YEAR = re.compile(r"\b(?:1[89]|20)\d{2}\b")
_WORD = re.compile(r"[A-Za-z]{3,}|[一-鿿]{2,}")
_LEFTOVER_SPLIT = re.compile(r"[、;；]")
_GREETING = re.compile(r"^(好的[，,！!]?)|(希望对您有帮助)|(请随时告诉我)|(如有需要)")

MAX_TITLE = 300
MAX_LIST_ITEM = 200


def normalize_text(text: str) -> str:
    """只保留字母、数字与汉字，用于可定位性比较。"""
    return re.sub(r"[^0-9a-z一-鿿]+", "", text.lower())


def numeric_tokens(values: list[Any]) -> list[str]:
    tokens: list[str] = []
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            tokens.append(str(value))
        elif isinstance(value, str):
            tokens.extend(_NUMBER.findall(value))
        elif isinstance(value, (list, tuple, set)):
            tokens.extend(numeric_tokens(list(value)))
    # 单位数（如“3 个作者”里的 3）在原文里太容易巧合，不参与溯源判定。
    return [token for token in tokens if len(token.replace(",", "").replace(".", "")) >= 2]


def flatten_values(payload: dict[str, Any] | None, text: str | None) -> list[Any]:
    if payload is not None:
        return list(payload.values())
    return [text or ""]


def citation_is_grounded(reference: str, haystack: str) -> bool:
    years = _YEAR.findall(reference)
    if any(year not in haystack for year in years):
        return False
    words = [normalize_text(word) for word in _WORD.findall(reference)]
    if not words:
        return True
    return any(word in haystack for word in words)


@register_step
class RulesStep(BaseStep):
    name = "rules"

    async def run(self, ctx: StepContext) -> StepResult:
        started = time.perf_counter()
        weights = dict(DEFAULT_WEIGHTS)
        if self.config is not None and self.config.weights:
            weights.update(self.config.weights)

        style = self.config.reference_style if self.config is not None else "gb-t-7714"
        completeness = self._completeness(ctx)
        format_score = self._format_compliance(ctx, style)
        grounding = self._grounding(ctx) if ctx.input_text.strip() else None

        dimensions = {"completeness": completeness, "format": format_score, "grounding": grounding}
        ctx.dimensions = dimensions
        scored = {key: value for key, value in dimensions.items() if value is not None}
        total_weight = sum(weights.get(key, 0.0) for key in scored)
        score = (
            sum(weights.get(key, 0.0) * value for key, value in scored.items()) / total_weight if total_weight else 0.0
        )
        ctx.score = round(score, 3)
        threshold = ctx.settings.score_threshold
        passed = ctx.valid and ctx.score >= threshold

        weak = [f"{key}={value:.2f}" for key, value in scored.items() if value < threshold]
        reason = ""
        if not ctx.valid:
            reason = "Schema 校验未通过"
        elif not passed:
            reason = f"得分 {ctx.score:.2f} 低于阈值 {threshold:.2f}（{', '.join(weak)}）"
        elif grounding is None:
            reason = "未提供原始材料，一致性维度不参与计分"
        return StepResult(
            name=self.name,
            passed=passed,
            score=ctx.score,
            reason=reason,
            ms=self.elapsed_ms(started),
        )

    def _completeness(self, ctx: StepContext) -> float:
        if ctx.schema is None:
            return 1.0 if (ctx.text or "").strip() else 0.0
        if ctx.parsed is None:
            return 0.0
        required = ctx.schema.required_fields()
        optional = ctx.schema.optional_fields()
        required_hits = sum(1 for name in required if is_filled(ctx.parsed.get(name)))
        optional_hits = sum(1 for name in optional if is_filled(ctx.parsed.get(name)))
        required_ratio = required_hits / len(required) if required else 1.0
        optional_ratio = optional_hits / len(optional) if optional else 1.0
        return 0.7 * required_ratio + 0.3 * optional_ratio

    def _format_compliance(self, ctx: StepContext, style: str) -> float:
        checks: list[bool] = []
        if ctx.schema is None or ctx.parsed is None:
            text = ctx.text or ctx.raw_output
            checks.append("```" not in text)
            checks.append(_GREETING.search(text) is None)
            return sum(checks) / len(checks)

        parsed = ctx.parsed
        year = parsed.get("year")
        if year is not None:
            checks.append(isinstance(year, int) and 1500 <= year <= dt.date.today().year + 1)

        for name in ("authors", "keywords"):
            items = parsed.get(name)
            if isinstance(items, list) and items:
                checks.extend(
                    isinstance(item, str) and not _LEFTOVER_SPLIT.search(item) and len(item) <= MAX_LIST_ITEM
                    for item in items
                )

        references = parsed.get("references")
        if isinstance(references, list) and references:
            checks.extend(is_compliant(str(item), style) for item in references)

        doi = parsed.get("doi")
        if doi:
            checks.append(re.match(r"\b10\.\d{4,9}/\S+", str(doi)) is not None)

        metric_value = parsed.get("value")
        if isinstance(metric_value, (int, float)):
            checks.append(math.isfinite(metric_value))
            if parsed.get("unit") == "%":
                checks.append(0 <= metric_value <= 100)

        title = parsed.get("title")
        if isinstance(title, str) and title:
            checks.append(len(title) <= MAX_TITLE)
            checks.append("```" not in title)

        abstract = parsed.get("abstract")
        if isinstance(abstract, str) and abstract:
            checks.append(_GREETING.search(abstract) is None)

        return sum(checks) / len(checks) if checks else 1.0

    def _grounding(self, ctx: StepContext) -> float:
        haystack = normalize_text(ctx.input_text)
        checks = [
            token.replace(",", "").replace(".", "") in haystack
            for token in numeric_tokens(flatten_values(ctx.parsed, ctx.text))
        ]
        references = (ctx.parsed or {}).get("references") or []
        checks.extend(citation_is_grounded(str(reference), haystack) for reference in references)
        return sum(checks) / len(checks) if checks else 1.0
