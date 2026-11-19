"""``rules`` 步骤：确定性加权计分。

散文输出按 **plainness / concision / preservation** 计分——本项目的目标是把
过度防御与晦涩的表达改短、改懂。结构化输出按 **completeness / format /
grounding / plainness** 计分，三维结构指标沿用原口径，另加一维通俗度。

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
from app.plain import report
from app.schemas import optional_field_names, required_field_names
from app.settings import PROSE_WEIGHTS, STRUCTURED_WEIGHTS

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_YEAR = re.compile(r"\b(?:1[89]|20)\d{2}\b")
_WORD = re.compile(r"[A-Za-z]{3,}|[一-鿿]{2,}")
_LEFTOVER_SPLIT = re.compile(r"[、;；]")
_GREETING = re.compile(r"^(好的[，,！!]?)|(希望对您有帮助)|(请随时告诉我)|(如有需要)")

#: 结构化输出里不参与通俗度评估的字段：引用串与标识符不该用日常措辞衡量。
_NON_NARRATIVE = frozenset({"doi", "type", "year", "value", "baseline", "unit"})

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
        prose = ctx.parsed is None
        weights = self._weights(prose)
        style = self.config.reference_style if self.config is not None else "gb-t-7714"

        language = report(self._narrative(ctx), ctx.input_text or None)
        preservation = self._grounding(ctx) if ctx.input_text.strip() else None

        if prose:
            dimensions: dict[str, float | None] = {
                "plainness": language.plainness,
                "confidence": language.confidence,
                "concision": language.concision,
                "preservation": preservation,
            }
        else:
            dimensions = {
                "completeness": self._completeness(ctx),
                "format": self._format_compliance(ctx, style),
                "grounding": preservation,
                "plainness": language.plainness,
            }

        ctx.dimensions = dimensions
        scored = {key: value for key, value in dimensions.items() if value is not None}
        # 必须用 fsum 而不是 sum：sum() 自 3.12 起改用补偿求和，同一组权重在 3.11 上
        # 少 1 ULP（0.9999999999999999），恰好把 0.6125 这类值推到 round() 的两可边沿，
        # 得分就会随解释器版本变 0.001——"确定性可复现评分"不允许这种事。
        total_weight = math.fsum(weights.get(key, 0.0) for key in scored)
        score = (
            math.fsum(weights.get(key, 0.0) * value for key, value in scored.items()) / total_weight
            if total_weight
            else 0.0
        )
        ctx.score = round(score, 3)
        threshold = ctx.settings.score_threshold
        passed = ctx.valid and ctx.score >= threshold

        weak = [f"{key}={value:.2f}" for key, value in scored.items() if value < threshold]
        reason = language.summary()
        if not ctx.valid:
            reason = "Schema 校验未通过"
        elif not passed:
            reason = f"得分 {ctx.score:.2f} 低于阈值 {threshold:.2f}（{', '.join(weak)}）｜{language.summary()}"
        elif preservation is None:
            reason = f"{language.summary()}；未提供原始材料，一致性维度不参与计分"
        elif dimensions.get("completeness") is None:
            # 不可测要说清是不可测：留空会让人以为完整度已经评过且满分。
            reason = f"{language.summary()}；字段完整度对该 Schema 不可测，不参与计分"
        return StepResult(
            name=self.name,
            passed=passed,
            score=ctx.score,
            reason=reason,
            ms=self.elapsed_ms(started),
        )

    def _weights(self, prose: bool) -> dict[str, float]:
        """按场景取权重，允许 pipeline.yaml 分组覆盖。"""
        base = dict(PROSE_WEIGHTS if prose else STRUCTURED_WEIGHTS)
        configured = self.config.weights if self.config is not None else None
        if configured:
            override = configured.get("prose" if prose else "structured")
            if override:
                base.update(override)
        return base

    @staticmethod
    def _narrative(ctx: StepContext) -> str:
        """参与通俗度评估的文本：散文全文，或结构化里的叙述字段。

        引用串与标识符不算叙述——用日常措辞标准去要求 GB/T 引用是错的度量。
        """
        if ctx.parsed is None:
            return ctx.text or ctx.raw_output
        parts = [value for key, value in ctx.parsed.items() if isinstance(value, str) and key not in _NON_NARRATIVE]
        return "。".join(parts)

    def _completeness(self, ctx: StepContext) -> float | None:
        """字段完整度；`None` 表示这个维度**不可测**，而不是"测到了 0 分"。

        自定义 Pydantic 模型走 `model_fields` 通用面即可参与评分；此前直接调
        `required_fields()` 是内置模型才有的方法，任何自定义模型都会让本步骤抛错、
        整次评分变成 `None`。不可测时权重由聚合处剔除，不会稀释总分。
        """
        if ctx.schema is None:
            return 1.0 if (ctx.text or "").strip() else 0.0
        if ctx.parsed is None:
            return 0.0
        required = required_field_names(ctx.schema)
        optional = optional_field_names(ctx.schema)
        if required is None or optional is None:
            return None
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
