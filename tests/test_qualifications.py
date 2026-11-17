"""标注集回归：限定语判定的唯一抓手。

`data/qualifications.jsonl` 由本轮起草、待用户审改；在此之前这些期望值代表的是
我的判断，不是外部事实。改动词表或判定规则时必须先跑这里。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.plain import (
    classify_qualification,
    confidence,
    normalize_text,
    ordering,
    plainness,
    qualifications,
    referents,
)

DATASET = Path(__file__).resolve().parents[1] / "data" / "qualifications.jsonl"


def load() -> list[dict[str, Any]]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


SAMPLES = load()


def source_norm(item: dict[str, Any]) -> str | None:
    return normalize_text(item["source"]) if item.get("source") else None


@pytest.mark.parametrize("item", SAMPLES, ids=[str(s["id"]) for s in SAMPLES])
def test_classification_matches_annotation(item: dict[str, Any]):
    got = classify_qualification(item["sentence"], source_norm(item))
    assert got == item["expect"], f"{item['why']}（句子：{item['sentence'][:30]}）"


def test_dataset_covers_every_verdict_and_needs_review_markers():
    verdicts = {str(item["expect"]) for item in SAMPLES}
    assert verdicts == {"ungrounded", "boundary", "unverified", "None"}
    assert len(SAMPLES) >= 12
    assert all(item.get("why") for item in SAMPLES), "每条判定都要写理由，否则无法审改"


def test_ungrounded_and_boundary_are_separated_by_confidence():
    """这两句此前 plainness 都是 1.0——分类器就是为了把它们分开。"""
    source = normalize_text("样本覆盖 3 个数据集，2023 年采购的算力，使用横截面数据。")
    boundary = "受制于 2023 年的算力条件，本结论仅在 3 个数据集上得到验证。"
    ungrounded = "这或许只能被视为探索性证据，并不意味着该方法在任何场景下都有效。"
    assert confidence(boundary, source) == 1.0
    assert confidence(ungrounded, source) == 0.0
    assert plainness(ungrounded, source) == 0.0
    # 边界句里的 hedge 不该被当冗余罚掉
    assert plainness(boundary, source) == 1.0


def test_boundary_hedge_is_exempt_only_for_boundaries():
    source = normalize_text("本文方法优于基线。")
    assert plainness("本方法可能在一定程度上优于基线。", source) < plainness(
        "受制于 2023 年算力，本方法仅在 3 个数据集上验证。", normalize_text("2023 年采购了 8 卡集群，覆盖 3 个数据集。")
    )


def test_claim_first_ordering_detection():
    hedge_first = "或许只能被视为探索性证据。本方法将误差降低 12.5%。"
    claim_first = "本方法将误差降低 12.5%。这或许只是探索性证据。"
    assert ordering(hedge_first)["claim_first"] is False
    assert ordering(claim_first)["claim_first"] is True
    assert confidence(claim_first, None) > confidence(hedge_first, None)


def test_scope_nouns_are_matched_across_wording_families():
    """句子说"样本量"、原文写"被试"：逐字匹配会误判成无据限定。"""
    item = next(sample for sample in SAMPLES if sample["id"] == "q12")
    assert classify_qualification(item["sentence"], source_norm(item)) == "boundary"


def test_english_stop_words_are_not_referents():
    assert referents("This may only be regarded as preliminary evidence.") == []


def test_single_digit_referent_is_kept():
    """rules.numeric_tokens 会丢弃单位数；这里必须保留，否则边界判定失效。"""
    assert "3" in referents("本结论仅在 3 个数据集上得到验证。")


def test_qualifications_payload_is_shaped_for_the_page():
    marks = qualifications("本结论仅在 3 个数据集上得到验证。这或许只是探索性证据。", None)
    assert {str(item["verdict"]) for item in marks} == {"unverified", "ungrounded"}
    assert all({"index", "verdict", "text", "referents"} == set(item) for item in marks)
