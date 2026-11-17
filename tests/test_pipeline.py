from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.audit import AuditLog
from app.optimizer import optimize
from app.optimizer.context import StepContext
from app.optimizer.pipeline import new_request_id
from app.optimizer.steps import REGISTRY, register_step
from app.optimizer.steps.base import BaseStep, StepResult
from app.providers.base import BaseProvider, ChatResponse, ProviderError
from app.providers.mock import MockProvider
from app.schemas import Paper
from app.settings import PipelineConfig, Settings

README_RAW = '{"title":"基于深度学习的图像分类方法研究","year":"2023"}'
README_INPUT = "《基于深度学习的图像分类方法研究》，张三、李四，发表于 CVPR 2023。"


def names(result) -> list[str]:
    return [step["name"] for step in result.steps]


class SlowProvider(BaseProvider):
    name = "slow"

    def __init__(self, delay_s: float = 0.5) -> None:
        self.delay_s = delay_s
        self.calls = 0

    async def chat(self, **kwargs) -> ChatResponse:
        import asyncio

        self.calls += 1
        await asyncio.sleep(self.delay_s)
        return ChatResponse(text='{"title": "T"}')


@register_step
class ExplodingStep(BaseStep):
    name = "exploding"

    async def run(self, ctx) -> StepResult:
        raise RuntimeError("步骤内部崩溃")


@register_step
class CcfVenueStep(BaseStep):
    """README §7.3 的自定义步骤示例：校验 venue 是否在 CCF 推荐目录内。"""

    name = "ccf_venue"

    async def run(self, ctx) -> StepResult:
        venue = (ctx.parsed or {}).get("venue", "")
        if venue not in ("CVPR", "ACL"):
            return StepResult(name=self.name, passed=False, score=0.0, reason="发表 venue 不在 CCF 推荐目录内")
        return StepResult(name=self.name, passed=True, score=1.0)


class TestEntryContract:
    async def test_result_exposes_readme_attributes(self, settings):
        result = await optimize(README_RAW, schema=Paper, input_text=README_INPUT, settings=settings)
        assert set(result.final_output) == {"title", "year"}
        assert (result.valid, result.repaired, result.degraded) == (True, False, False)
        assert result.score is not None

    async def test_schema_accepts_class_or_builtin_name(self, settings):
        by_class = await optimize(README_RAW, schema=Paper, settings=settings)
        by_name = await optimize(README_RAW, schema="paper", settings=settings)
        assert by_class.final_output == by_name.final_output

    async def test_scored_steps_are_reported_in_pipeline_order(self, settings):
        result = await optimize(README_RAW, schema="paper", input_text=README_INPUT, settings=settings)
        assert names(result) == ["schema", "rules", "format"]

    async def test_usage_stays_zero_without_model_calls(self, settings):
        result = await optimize(README_RAW, schema="paper", settings=settings)
        assert result.usage == {"prompt_tokens": 0, "completion_tokens": 0}

    def test_request_id_shape(self):
        prefix, date_part, nonce, sequence = new_request_id().split("_")
        assert (prefix, len(date_part), len(nonce), len(sequence)) == ("req", 8, 6, 4)
        assert new_request_id() != new_request_id()

    def test_request_id_differs_across_processes(self):
        """进程内计数器在各进程都从 0001 起，靠 nonce 区分；同一天多进程不得重号。"""
        root = str(Path(__file__).resolve().parents[1])
        code = f"import sys; sys.path.insert(0, {root!r}); from app.optimizer.pipeline import new_request_id; print(new_request_id())"
        outputs = [
            subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
            for _ in range(2)
        ]
        assert len(set(outputs)) == 2, outputs


class TestStepToggles:
    async def test_disabled_steps_do_not_run(self, settings):
        result = await optimize('{"title":"T","authors":"张三、李四"}', schema="paper", settings=settings, fmt=False)
        assert "format" not in names(result)
        assert set(result.final_output) >= {"venue"}

    async def test_score_false_disables_rules_and_yields_null_score(self, settings):
        result = await optimize(README_RAW, schema="paper", settings=settings, score=False)
        assert names(result) == ["schema", "format"]
        assert result.score is None

    async def test_humanize_default_off_and_explicitly_on(self, settings):
        off = await optimize("值得注意的是，本文方法具有广阔的应用前景。", settings=settings)
        assert "humanize" not in names(off)
        on = await optimize("值得注意的是，本文方法具有广阔的应用前景。", settings=settings, humanize=True)
        assert "humanize" in names(on)

    async def test_repair_false_keeps_invalid_output_untouched(self, settings):
        raw = "抱歉，我找不到这篇论文。"
        result = await optimize(raw, schema="paper", settings=settings, repair=False)
        assert result.repaired is False and names(result) == ["schema", "rules", "format"]
        assert result.final_output == raw

    async def test_strict_true_rejects_extra_fields(self, settings):
        result = await optimize('{"title":"T","note":"客套"}', schema="paper", settings=settings, strict=True)
        assert result.valid is False
        assert "note" in result.steps[0]["reason"]


class TestCustomSteps:
    async def test_registered_step_can_be_added_by_name(self, settings):
        config = PipelineConfig().with_step("ccf_venue", enabled=True)
        result = await optimize('{"title":"T","venue":"ArXiv"}', schema="paper", settings=settings, config=config)
        assert "ccf_venue" in names(result)
        assert result.steps[-1]["reason"] == "发表 venue 不在 CCF 推荐目录内"

    async def test_unknown_step_name_is_recorded_as_error(self, settings):
        config = PipelineConfig().with_step("does_not_exist", enabled=True)
        result = await optimize(README_RAW, schema="paper", settings=settings, config=config)
        assert "unknown_step(does_not_exist)" in result.errors

    def test_builtin_steps_are_all_registered(self):
        assert set(REGISTRY) >= {"schema", "rules", "repair", "format", "humanize"}


class TestReliability:
    async def test_step_exception_is_isolated_and_pipeline_continues(self, settings):
        config = PipelineConfig().with_step("exploding", enabled=True)
        result = await optimize(README_RAW, schema="paper", settings=settings, config=config)
        exploding = next(step for step in result.steps if step["name"] == "exploding")
        assert exploding["passed"] is False and "步骤异常已隔离" in exploding["reason"]
        assert result.valid is True and "format" in names(result)

    async def test_hard_timeout_fails_open_to_raw_output(self, settings):
        raw = '{"authors":"张三、李四"}'
        result = await optimize(raw, schema="paper", provider=SlowProvider(1.0), settings=settings, timeout_ms=300)
        assert result.degraded is True
        assert result.final_output == raw
        assert result.valid is False
        assert "管线超时" in result.degraded_reason

    async def test_fail_open_false_propagates_timeout(self, settings):
        strict = Settings(fail_open=False, audit_path=settings.audit_path)
        # fail_open 以 pipeline.yaml 为准（README §8.2 的优先级），故这里必须用 config 关闭
        with pytest.raises(RuntimeError, match="管线超时"):
            await optimize(
                '{"authors":"张三"}',
                schema="paper",
                provider=SlowProvider(1.0),
                settings=strict,
                config=PipelineConfig(fail_open=False),
                timeout_ms=300,
            )

    async def test_provider_failure_does_not_degrade_the_request(self, settings):
        class Broken(MockProvider):
            async def chat(self, **kwargs):
                raise ProviderError("上游 503")

        result = await optimize(
            "抱歉，无法解析。", schema="paper", provider=Broken(), settings=settings, timeout_ms=8000
        )
        assert result.degraded is False
        assert result.repaired is True
        assert result.final_output == "抱歉，无法解析。"
        assert "repair_failed" in result.errors


class TestAudit:
    async def test_one_line_per_request_with_raw_and_final(self, settings, tmp_path: Path):
        log_path = tmp_path / "audit.jsonl"
        await optimize(README_RAW, schema="paper", input_text=README_INPUT, settings=settings, audit=AuditLog(log_path))
        await optimize(README_RAW, schema="paper", settings=settings, audit=AuditLog(log_path))
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        record = json.loads(lines[0])
        assert record["raw_output"] == README_RAW
        assert record["final_output"]["title"] == "基于深度学习的图像分类方法研究"
        assert [step["name"] for step in record["steps"]] == ["schema", "rules", "format"]
        assert {"score", "dimensions", "ms", "usage", "provider", "changes"} <= set(record)

    async def test_audit_can_be_disabled(self, settings, tmp_path: Path):
        log_path = tmp_path / "audit.jsonl"
        config = PipelineConfig(audit=False)
        await optimize(README_RAW, schema="paper", settings=settings, config=config, audit=AuditLog(log_path))
        assert not log_path.exists()

    async def test_audit_write_failure_does_not_break_the_response(self, settings, tmp_path: Path):
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        result = await optimize(README_RAW, schema="paper", settings=settings, audit=AuditLog(blocked))
        assert result.valid is True


class TestTextAndInvalidPaths:
    async def test_plain_text_output_without_schema_is_returned_as_string(self, settings):
        text = "本文方法在 3 个数据集上的 Top-1 准确率提升 12.5%。"
        result = await optimize(text, settings=settings, input_text=text)
        assert result.valid is True
        assert result.final_output == text
        assert isinstance(result.final_output, str)

    async def test_score_is_reported_with_three_dimensions(self, settings):
        result = await optimize(README_RAW, schema="paper", input_text=README_INPUT, settings=settings)
        assert set(result.dimensions) == {"completeness", "format", "grounding"}

    async def test_context_deadline_is_set_from_timeout(self, settings):
        ctx = StepContext(raw_output="x", settings=settings, config=PipelineConfig(), timeout_s=1.0)
        assert ctx.remaining_s() == 1.0
