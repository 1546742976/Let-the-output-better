"""``simplify`` 步骤：把过度防御与晦涩的表达改成通俗、简洁的说法。

本项目的主目标就在这一步。采纳需要同时满足两条，缺一即丢弃并标记
``simplify_rejected``：

- **事实不变**：数值 multiset 相等、大写缩写锚点不丢；
- **没有变得更长**：改写后超过原文 ``GROWTH_LIMIT`` 视为没达成目标。

"更短"只作反向保护，不设必须压缩的配额——硬指标会诱使模型删掉必要信息。
"""

from __future__ import annotations

import time
from collections import Counter

from app.optimizer.context import StepContext
from app.optimizer.steps import register_step
from app.optimizer.steps.base import BaseStep, StepResult
from app.plain import (
    GROUP_WEIGHTS,
    acronym_anchors,
    canonical_numbers,
    normalize_text,
    qualifications,
    referents,
    report,
    scope_group,
    unit_classes,
)
from app.providers.base import ProviderError
from app.textio import load_prompt, render_prompt

MAX_CALL_S = 6.0
MIN_LENGTH = 12
#: 改写后长度超过原文这么多就拒绝，认为没做到"更简洁"。
GROWTH_LIMIT = 1.10
#: 一次运行最多改写的文本字段数，避免多字段时成倍付出调用成本。
MAX_FIELDS = 3
SYSTEM = "你是去防御化改写器：像作者本人陈述结论那样重写，只输出改写后的文本，不解释、不加围栏。"
DEFAULT_PROMPT = load_prompt("prompts/simplify.txt")

#: 结构化输出里不参与改写的字段（标识符与短枚举改写风险高于收益）。
SKIP_FIELDS = frozenset({"doi", "type", "year", "value", "baseline", "unit"})


def fact_tokens(text: str) -> tuple[Counter[str], set[str]]:
    return canonical_numbers(text), acronym_anchors(text)


def check_facts(before: str, after: str) -> str | None:
    """返回首个事实不一致的原因；一致则返回 ``None``。

    顺序是"数值 → 单位类别 → 缩写锚点"：前两条管数字有没有被改动或换了说法，第三条
    管 `CVPR` 这类不可再生的专名有没有被抹掉。锚点只查缺失不查多余——改写里出现的
    新缩写另有事实溯源维度去管，闸门在这里从严只会误杀合法改写。
    """
    numbers_before, anchors_before = fact_tokens(before)
    numbers_after, anchors_after = fact_tokens(after)
    if numbers_before != numbers_after:
        return f"数值变化：{dict(numbers_before)} → {dict(numbers_after)}"
    units_before, units_after = unit_classes(before), unit_classes(after)
    if units_before != units_after:
        return f"单位类别变化：{dict(units_before)} → {dict(units_after)}"
    missing = anchors_before - anchors_after
    if missing:
        return f"关键表述丢失：{'、'.join(sorted(missing))}"
    return None


def _scope_groups(text: str, source: str | None) -> list[frozenset[str]]:
    """改写必须留住的范围指称，按"同一概念的等价写法"分组。

    两处刻意的选择：
    - 取 `item["sentence"]` 而不是展示用的 `text[:48]`——落在第 48 字之后的指称
      （"……未在任何公开数据集上评测"）此前对闸门完全不可见，删掉也不会被拦；
    - 族内写法归进同一组，组里任意一个写法还在就算保住，与判定层的同族互认同源。
    """
    groups: list[frozenset[str]] = []
    for item in qualifications(text, source):
        if item["verdict"] not in ("boundary", "unverified"):
            continue
        for token in referents(str(item["sentence"])):
            normalized = frozenset({normalize_text(member) for member in scope_group(token)}) - {""}
            if normalized:
                groups.append(normalized)
    return groups


def check_boundaries(before: str, after: str, source: str | None) -> str | None:
    """改写前判为研究边界的限定语，其范围指称若在改写后**整族消失**即为误删。

    这是"保留真正影响结论的研究边界"唯一的强制执行方式——写在提示词里只是希望。
    判"整族消失"而非"逐字消失"：把"仅在 3 个数据集上验证"改成"仅在 3 个基准上验证"
    范围没变，逐字比对会把合法改写连字段一起作废，且拒绝理由本身是假话。
    """
    required = _scope_groups(before, source)
    if not required:
        return None
    # 键是归一化后的指称，比对对象也必须归一化：否则 "12.5" 归一成 "125" 后
    # 在原文里"找不到"，会把合法改写误判成丢失边界。
    haystack = normalize_text(after)
    lost = [group for group in required if not any(key in haystack for key in group)]
    if lost:
        names = "、".join("/".join(sorted(group)) for group in lost[:3])
        return f"丢失研究边界（{len(lost)} 处范围指称整族消失：{names}）"
    return None


def _clip(text: str) -> str:
    """留痕里只显示前 18 字，但截出来的半句不该以逗号收尾。"""
    return text[:18].rstrip("，、；：,;: ")


def _comparison(before: str, after: str, source: str | None = None) -> str:
    """改前/改后的通俗化与自信度对照，直接进 reason 与审计留痕。

    命中条目与判定结果也写进来：只给分数无法回答"到底哪句是废话、哪句是边界"。
    """
    b, a = report(before, source), report(after, source)
    labels = [f.label for f in b.findings if f.group in GROUP_WEIGHTS]
    order = list(dict.fromkeys(labels))[:6]
    cut = [_clip(str(item["text"])) for item in b.qualifications if item["verdict"] == "ungrounded"]
    keep = [_clip(str(item["text"])) for item in b.qualifications if item["verdict"] in ("boundary", "unverified")]
    tails = [f"｜命中：{'、'.join(order)}" if order else ""]
    if cut:
        tails.append(f"｜删除无据：{'、'.join(cut[:3])}")
    if keep:
        tails.append(f"｜保留边界：{'、'.join(keep[:3])}")
    tails.append("｜主张先行：是" if a.ordering.get("claim_first") else "｜主张先行：否")
    return (
        f"防御措辞 {int(b.metrics['hard_hits'])} → {int(a.metrics['hard_hits'])} 处；"
        f"句均 {b.metrics['avg_len']:.0f} → {a.metrics['avg_len']:.0f} 字；"
        f"plainness {b.plainness} → {a.plainness}；"
        f"concision {b.concision} → {a.concision}；"
        f"confidence {b.confidence} → {a.confidence}" + "".join(tail for tail in tails if tail)
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
            if violation is None:
                # 顺序：先保事实，再保研究边界，最后看有没有变长。
                violation = check_boundaries(text, candidate, ctx.input_text or None)
            if violation:
                rejected.append(f"{field_name}：{violation}")
                continue
            if len(candidate) > len(text) * GROWTH_LIMIT:
                rejected.append(f"{field_name}：未变简洁（{len(text)} → {len(candidate)} 字）")
                continue
            self._apply(ctx, field_name, candidate)
            adopted.append(_comparison(text, candidate, ctx.input_text or None))

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
