"""回归测试：以金标集的 final_output 作为基线快照（README §13）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.optimizer import optimize
from app.providers.mock import MockProvider
from app.settings import PipelineConfig, Settings

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "golden.jsonl"
BASELINE = Path(__file__).with_name("golden_baseline.json")
UPDATE_FLAG = "OPTIMIZE_UPDATE_GOLDEN"


def load_samples() -> list[dict[str, Any]]:
    return [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]


async def build_snapshot() -> dict[str, Any]:
    settings = Settings(provider="mock", audit_path="unused-in-tests")
    provider = MockProvider()
    config = PipelineConfig(audit=False)
    snapshot: dict[str, Any] = {}
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
        snapshot[sample["id"]] = {
            "valid": result.valid,
            "score": result.score,
            "repaired": result.repaired,
            "markers": result.markers,
            "final_output": result.final_output,
        }
    return snapshot


async def test_golden_snapshot_matches_baseline():
    snapshot = await build_snapshot()
    if os.environ.get(UPDATE_FLAG):
        BASELINE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pytest.skip(f"已通过 {UPDATE_FLAG}=1 重写基线快照")
    assert BASELINE.exists(), f"缺少基线快照 {BASELINE.name}，执行 OPTIMIZE_UPDATE_GOLDEN=1 pytest 生成"
    assert json.loads(BASELINE.read_text(encoding="utf-8")) == snapshot


async def test_snapshot_covers_every_golden_sample():
    snapshot = await build_snapshot()
    assert set(snapshot) == {sample["id"] for sample in load_samples()}
