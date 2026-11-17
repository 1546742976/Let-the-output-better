"""``simplify`` 步骤：把过度防御与晦涩的表达改成通俗、简洁的说法。

本项目的主目标就在这一步。采纳需要同时满足两条，缺一即丢弃并标记
``simplify_rejected``：

- **事实不变**：数值 multiset 相等、大写缩写锚点不丢；
- **没有变得更长**：改写后超过原文 ``GROWTH_LIMIT`` 视为没达成目标。

"更短"只作反向保护，不设必须压缩的配额——硬指标会诱使模型删掉必要信息。
"""

from __future__ import annotations

import re
import time
from collections import Counter

from app.optimizer.context import StepContext
from app.optimizer.steps import register_step
from app.optimizer.steps.base import BaseStep, StepResult
from app.plain import GROUP_WEIGHTS, report
from app.providers.base import ProviderError
from app.textio import load_prompt, render_prompt

MAX_CALL_S = 6.0
MIN_LENGTH = 12
#: 改写后长度超过原文这么多就拒绝，认为没做到"更简洁"。
GROWTH_LIMIT = 1.10
#: 一次运行最多改写的文本字段数，避免多字段时成倍付出调用成本。
MAX_FIELDS = 3
SYSTEM = "你是通俗改写器，只输出改写后的文本，不解释、不加引号或代码围栏。"
DEFAULT_PROMPT = load_prompt("prompts/simplify.txt")

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
# 只把全大写缩写（CVPR、GPT 之类）当作引用锚点：普通词的大小写变化不应触发拒绝。
_ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")
#: 结构化输出里不参与改写的字段（标识符与短枚举改写风险高于收益）。
SKIP_FIELDS = frozenset({"doi", "type", "year", "value", "baseline", "unit"})


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


def _comparison(before: str, after: str) -> str:
    """改前/改后的通俗化指标对照，直接进 reason 与审计留痕。

    命中条目也写进来：只给一个分数无法回答"到底哪几句是防御性废话"。
    """
    b, a = report(before), report(after)
    labels = [f.label for f in b.findings if f.group in GROUP_WEIGHTS]
    order = list(dict.fromkeys(labels))[:6]
    tail = f"｜命中：{'、'.join(order)}" if order else ""
    return (
        f"防御措辞 {int(b.metrics['hard_hits'])} → {int(a.metrics['hard_hits'])} 处；"
        f"句均 {b.metrics['avg_len']:.0f} → {a.metrics['avg_len']:.0f} 字；"
        f"plainness {b.plainness} → {a.plainness}；"
        f"concision {b.concision} → {a.concision}{tail}"
    )


@register_step
class SimplifyStep(BaseStep):
    name = "simplify"

    def should_run(self, ctx: StepContext) -> bool:
        if ctx.provider is None:
            return False
        # 默认只改写散文：结构化字段被重写会动到已校验过的内容，需显式打开。
        prose_only = bool(self.config.prose_only) if self.config is not None else True
        return not (prose_only and ctx.parsed is not None)

    async def run(self, ctx: StepContext) -> StepResult:
        started = time.perf_counter()
        provider = ctx.provider
        if provider is None:
            return StepResult(
                name=self.name,
                passed=True,
                score=1.0,
                reason="未配置模型 provider，跳过通俗化",
                ms=self.elapsed_ms(started),
            )
        targets = self._targets(ctx)
        if not targets:
            return StepResult(
                name=self.name,
                passed=True,
                score=1.0,
                reason="没有值得改写的叙述文本",
                ms=self.elapsed_ms(started),
            )

        template = load_prompt(self.config.prompt if self.config else None, fallback=DEFAULT_PROMPT)
        adopted: list[str] = []
        rejected: list[str] = []
        unchanged: list[str] = []
        for field_name, text in targets:
            prompt = render_prompt(template, text=text, instruction=ctx.instruction or "（未提供）")
            try:
                response = await provider.chat(
                    system=SYSTEM,
                    user=prompt,
                    task="simplify",
                    json_mode=False,
                    timeout_s=min(MAX_CALL_S, max(0.5, ctx.remaining_s())),
                )
            except ProviderError as exc:
                ctx.errors.append("simplify_failed")
                return StepResult(
                    name=self.name,
                    passed=False,
                    reason=f"通俗化调用失败，保留原文：{exc}",
                    ms=self.elapsed_ms(started),
                )
            ctx.usage.add(response.prompt_tokens, response.completion_tokens)
            candidate = (response.text or "").strip()
            if not candidate:
                rejected.append(f"{field_name}：改写结果为空")
                continue
            if candidate == text.strip():
                # 模型原样退回：记成"采纳"是假话，还白烧了一次调用。
                unchanged.append(field_name)
                continue
            violation = check_facts(text, candidate)
            if violation:
                rejected.append(f"{field_name}：{violation}")
                continue
            if len(candidate) > len(text) * GROWTH_LIMIT:
                rejected.append(f"{field_name}：未变简洁（{len(text)} → {len(candidate)} 字）")
                continue
            self._apply(ctx, field_name, candidate)
            adopted.append(_comparison(text, candidate))

        if adopted:
            ctx.changes.append("采纳通俗化重写")
        if rejected and not adopted:
            ctx.markers.append("simplify_rejected")

        if adopted and rejected:
            reason = f"采纳 {len(adopted)} 段；拒绝 {len(rejected)} 段（{rejected[0]}）｜{adopted[0]}"
        elif adopted:
            reason = f"已通俗化｜{adopted[0]}"
        elif rejected:
            reason = f"全部拒绝：{rejected[0]}"
        elif unchanged:
            reason = f"已是通俗表述，无需改写（{', '.join(unchanged)}）"
        else:
            reason = "没有值得改写的叙述文本"
        return StepResult(
            name=self.name,
            passed=not rejected,
            score=1.0 if adopted or unchanged else 0.0,
            reason=reason,
            ms=self.elapsed_ms(started),
        )

    @staticmethod
    def _targets(ctx: StepContext) -> list[tuple[str, str]]:
        """散文整段改写；结构化输出改写较长的叙述字段，默认由开关决定是否走到这里。"""
        if ctx.parsed is None:
            text = ctx.text or ""
            return [("text", text)] if len(text.strip()) >= MIN_LENGTH else []
        candidates: list[tuple[str, str]] = []
        for key, value in ctx.parsed.items():
            if key in SKIP_FIELDS or not isinstance(value, str):
                continue
            if len(value.strip()) < MIN_LENGTH:
                continue
            candidates.append((key, value))
        # 长文本先改，短标题风险更低地留在后面（受 MAX_FIELDS 截断）。
        candidates.sort(key=lambda item: len(item[1]), reverse=True)
        return candidates[:MAX_FIELDS]

    @staticmethod
    def _apply(ctx: StepContext, field: str, candidate: str) -> None:
        if field == "text":
            ctx.text = candidate
            return
        if ctx.parsed is None:
            return
        ctx.parsed[field] = candidate
        if ctx.schema is not None:
            ctx.model = ctx.schema.model_validate(ctx.parsed)
