"""``repair`` 步骤：未通过校验或低分时调用修复模型。

采纳闸门：候选须**通过 Schema 校验**且**得分不低于原始输出**，否则整体丢弃（README §10）。
"""

from __future__ import annotations

import time

from app.optimizer.context import StepContext
from app.optimizer.scoring import score_text
from app.optimizer.steps import register_step
from app.optimizer.steps.base import BaseStep, StepResult
from app.providers.base import ProviderError
from app.textio import load_prompt, render_prompt

MAX_CALL_S = 6.0
SYSTEM = "你只输出一个 JSON 对象，不要输出解释、客套话或代码围栏。"
DEFAULT_PROMPT = load_prompt("prompts/repair.txt")


@register_step
class RepairStep(BaseStep):
    name = "repair"

    def should_run(self, ctx: StepContext) -> bool:
        if ctx.provider is None or ctx.remaining_s() <= 0.2:
            return False
        if not ctx.valid:
            return ctx.score is None or ctx.score < ctx.settings.score_threshold
        # 评分步骤被关闭时没有质量信号，不因“缺分数”而触发修复调用。
        return ctx.score is not None and ctx.score < ctx.settings.score_threshold

    async def run(self, ctx: StepContext) -> StepResult:
        started = time.perf_counter()
        provider = ctx.provider
        if provider is None:
            return StepResult(
                name=self.name,
                passed=False,
                reason="未配置模型 provider，无法修复",
                ms=self.elapsed_ms(started),
            )
        template = load_prompt(self.config.prompt if self.config else None, fallback=DEFAULT_PROMPT)
        prompt = render_prompt(
            template,
            instruction=ctx.instruction or "（未提供）",
            schema_json=ctx.schema.schema_hint() if ctx.schema is not None else "（未指定 Schema，输出 JSON 对象即可）",
            errors="；".join(ctx.errors) or "（无）",
            input_text=ctx.input_text or "（未提供）",
            raw_output=ctx.raw_output,
        )
        base_score = ctx.score or 0.0
        attempts = max(1, ctx.settings.max_repair_retries)
        reason = "修复未触发"

        for attempt in range(1, attempts + 1):
            timeout_s = min(MAX_CALL_S, max(0.5, ctx.remaining_s()))
            try:
                response = await provider.chat(
                    system=SYSTEM, user=prompt, task="repair", json_mode=True, timeout_s=timeout_s
                )
            except ProviderError as exc:
                ctx.repaired = True
                ctx.errors.append("repair_failed")
                return StepResult(
                    name=self.name,
                    passed=False,
                    reason=f"第 {attempt} 次修复调用失败，保留原输出：{exc}",
                    ms=self.elapsed_ms(started),
                )

            ctx.repaired = True
            ctx.usage.add(response.prompt_tokens, response.completion_tokens)
            probe = await score_text(ctx, response.text)
            if probe.valid and (probe.score or 0.0) >= base_score:
                ctx.parsed, ctx.model, ctx.text = probe.parsed, probe.model, probe.text
                ctx.valid, ctx.score, ctx.dimensions = probe.valid, probe.score, probe.dimensions
                ctx.errors = probe.errors
                ctx.raw_output = response.text
                ctx.changes.append(f"采纳修复结果（第 {attempt} 次，得分 {base_score:.2f} → {probe.score:.2f}）")
                return StepResult(
                    name=self.name,
                    passed=True,
                    score=probe.score,
                    reason="修复结果通过校验且得分不低于原输出，已采纳",
                    ms=self.elapsed_ms(started),
                )
            reason = (
                f"修复结果未通过采纳闸门（valid={probe.valid}，score={probe.score}，原得分 {base_score:.2f}），已丢弃"
            )

        ctx.markers.append("repair_rejected")
        return StepResult(
            name=self.name,
            passed=False,
            reason=reason,
            ms=self.elapsed_ms(started),
        )
