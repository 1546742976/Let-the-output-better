"""金标回归：拆成"我们自己代码的字节漂移"与"采纳不变式"两件事（README §13）。

此前只有一条：用 MockProvider 现跑现比对快照。那测的是 mock 词表的字符串替换——
改进 REPLACES 表会让基线集体变红，而它跟优化质量无关。现在分开：
快照走 `provider=None` 的确定性路径（schema+rules+format），mock 改写只断言
闸门不变式，不锁字节。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.optimizer import optimize
from app.optimizer.steps.simplify import check_boundaries, check_facts
from app.providers.mock import MockProvider
from app.settings import PipelineConfig, Settings

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "golden.jsonl"
BASELINE = Path(__file__).with_name("golden_baseline.json")
UPDATE_FLAG = "OPTIMIZE_UPDATE_GOLDEN"
GROWTH_LIMIT = 1.10


def load_samples() -> list[dict[str, Any]]:
    return [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]


def _deterministic_config() -> PipelineConfig:
    """只留不调模型的步骤：校验、评分、格式化。"""
    config = PipelineConfig(audit=False)
    for name in ("repair", "simplify"):
        config = config.with_step(name, enabled=False)
    return config


async def build_snapshot() -> dict[str, Any]:
    settings = Settings(provider="mock", audit_path="unused-in-tests")
    snapshot: dict[str, Any] = {}
    for sample in load_samples():
        result = await optimize(
            sample["raw_output"],
            schema=sample["schema"],
            instruction=sample.get("instruction", ""),
            input_text=sample.get("input_text", ""),
            provider=None,
            settings=settings,
            config=_deterministic_config(),
        )
        snapshot[sample["id"]] = {
            "provider": "none",
            "valid": result.valid,
            "score": result.score,
            "degraded": result.degraded,
            "final_output": result.final_output,
        }
    return snapshot


async def test_deterministic_snapshot_matches_baseline():
    snapshot = await build_snapshot()
    if os.environ.get(UPDATE_FLAG):
        BASELINE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pytest.skip(f"已通过 {UPDATE_FLAG}=1 重写基线快照")
    assert BASELINE.exists(), f"缺少基线快照 {BASELINE.name}，执行 OPTIMIZE_UPDATE_GOLDEN=1 pytest 生成"
    assert json.loads(BASELINE.read_text(encoding="utf-8")) == snapshot


async def test_snapshot_covers_every_golden_sample():
    snapshot = await build_snapshot()
    assert set(snapshot) == {sample["id"] for sample in load_samples()}


async def test_mock_rewrites_obey_the_adoption_invariants():
    """开 simplify 跑一遍，只断言闸门不变式。

    不比字节：mock 是词表替换，词表扩充不该让回归变红；但改写一旦被采纳，
    事实、边界与长度三条必须同时成立，否则就是闸门在交付错误。
    """
    settings = Settings(provider="mock", audit_path="unused-in-tests")
    provider = MockProvider()
    config = PipelineConfig(audit=False)
    adopted = 0
    for sample in load_samples():
        result = await optimize(
            sample["raw_output"],
            schema=sample["schema"],
            instruction=sample.get("instruction", ""),
            input_text=sample.get("input_text", ""),
            provider=provider,
            settings=settings,
            config=config,
        )
        assert result.degraded is False, sample["id"]
        if "采纳通俗化重写" not in result.changes:
            continue
        adopted += 1
        before = str(sample["raw_output"])
        after = (
            result.final_output
            if isinstance(result.final_output, str)
            else json.dumps(result.final_output, ensure_ascii=False)
        )
        source = sample.get("input_text") or None
        assert check_facts(before, after) is None, f"{sample['id']} 采纳了改变事实的改写"
        assert check_boundaries(before, after, source) is None, f"{sample['id']} 采纳了丢边界的改写"
        assert len(after) <= len(before) * GROWTH_LIMIT + 1, f"{sample['id']} 采纳了变长的改写"
    assert adopted > 0, "一条都没采纳，说明这套不变式没被检验过"
