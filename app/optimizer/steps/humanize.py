"""``humanize`` 步骤：调用模型去除空泛套话，重写为自然学术语体。

采纳前提是事实不变：数值、年份、venue/机构缩写等关键 token 重写前后必须一致，
否则丢弃重写结果并标记 ``humanize_rejected``（README §5、§10）。
"""

from __future__ import annotations

import re
import time
from collections import Counter

from app.optimizer.context import StepContext
from app.optimizer.steps import register_step
from app.optimizer.steps.base import BaseStep, StepResult
from app.providers.base import ProviderError
from app.textio import load_prompt, render_prompt

MAX_CALL_S = 6.0
MIN_LENGTH = 12
SYSTEM = "你是学术写作润色器，只输出改写后的文本。"
DEFAULT_PROMPT = load_prompt("prompts/humanize.txt")

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
# 只把全大写缩写（CVPR、GPT 之类）当作引用锚点：普通词的大小写变化不应触发拒绝。
_ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")


def fact_tokens(text: str) -> tuple[Counter, set[str]]:
    numbers = Counter(token.replace(",", "") for token in _NUMBER.findall(text))
    anchors = set(_ACRONYM.findall(text))
    return numbers, anchors


def check_facts(before: str, after: str) -> str | None:
    """返回首个事实不一致的原因；一致则返回 ``None``。"""
    numbers_before, anchors_before = fact_tokens(before)
    numbers_after, anchors_after = fact_tokens(after)
    if numbers_before != numbers_after:
        return f"数值变化：{dict(numbers_before)} → {dict(numbers_after)}"
    missing = anchors_before - anchors_after
    if missing:
        return f"关键表述丢失：{'、'.join(sorted(missing))}"
    return None


@register_step
class HumanizeStep(BaseStep):
    name = "humanize"

    def should_run(self, ctx: StepContext) -> bool:
        return ctx.provider is not None

    async def run(self, ctx: StepContext) -> StepResult:
        started = time.perf_counter()
        provider = ctx.provider
        target = self._target(ctx)
        if provider is None or target is None:
            reason = "未配置模型 provider，跳过重写" if provider is None else "没有需要重写的学术文本段"
            return StepResult(
                name=self.name,
                passed=True,
                score=1.0,
                reason=reason,
                ms=self.elapsed_ms(started),
            )
        field, text = target
        if len(text.strip()) < MIN_LENGTH:
            return StepResult(
                name=self.name,
                passed=True,
                score=1.0,
                reason="文本段过短，不值得调用模型重写",
                ms=self.elapsed_ms(started),
            )

        template = load_prompt(self.config.prompt if self.config else None, fallback=DEFAULT_PROMPT)
        prompt = render_prompt(template, text=text, instruction=ctx.instruction or "（未提供）")
        try:
            response = await provider.chat(
                system=SYSTEM,
                user=prompt,
                task="humanize",
                json_mode=False,
                timeout_s=min(MAX_CALL_S, max(0.5, ctx.remaining_s())),
            )
        except ProviderError as exc:
            ctx.errors.append("humanize_failed")
            return StepResult(
                name=self.name,
                passed=False,
                reason=f"重写调用失败，保留原文：{exc}",
                ms=self.elapsed_ms(started),
            )

        ctx.usage.add(response.prompt_tokens, response.completion_tokens)
        candidate = response.text.strip()
        if not candidate:
            ctx.markers.append("humanize_rejected")
            return StepResult(
                name=self.name, passed=False, reason="重写结果为空，保留原文", ms=self.elapsed_ms(started)
            )
        violation = check_facts(text, candidate)
        if violation:
            ctx.markers.append("humanize_rejected")
            return StepResult(
                name=self.name,
                passed=False,
                reason=f"重写未通过事实一致性校验（{violation}），已丢弃",
                ms=self.elapsed_ms(started),
            )

        self._apply(ctx, field, candidate)
        ctx.changes.append("采纳拟人化重写")
        return StepResult(
            name=self.name,
            passed=True,
            score=1.0,
            reason="重写通过事实一致性校验，已采纳",
            ms=self.elapsed_ms(started),
        )

    @staticmethod
    def _target(ctx: StepContext) -> tuple[str, str] | None:
        """结构化输出只重写 ``abstract``，纯文本输出整段重写。"""
        if ctx.parsed is not None:
            abstract = ctx.parsed.get("abstract")
            if isinstance(abstract, str) and abstract.strip():
                return "abstract", abstract
            return None
        if ctx.text:
            return "text", ctx.text
        return None

    @staticmethod
    def _apply(ctx: StepContext, field: str, candidate: str) -> None:
        if field == "text":
            ctx.text = candidate
            return
        if ctx.parsed is None:
            return
        ctx.parsed["abstract"] = candidate
        if ctx.schema is not None:
            ctx.model = ctx.schema.model_validate(ctx.parsed)
