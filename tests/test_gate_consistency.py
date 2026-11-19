"""判定层与采纳闸门必须共用同一套词汇知识。

这两条不变式各自都有过反例：同族写法（数据集↔基准）在判定层互认、在闸门层逐字比对，
结果合法改写被整字段拒绝且拒绝理由是假话；另一侧，闸门从被截断到 48 字的展示文本里
取指称，位于第 48 字之后的"数据集"删掉也不拦。
"""

from __future__ import annotations

import pytest

from app.optimizer.steps.simplify import _scope_groups, check_boundaries
from app.plain import SCOPE_FAMILIES, classify_qualification, normalize_text

PAIRS = [(a, b) for family in SCOPE_FAMILIES for a in family for b in family if a != b]


def test_scope_families_is_not_empty():
    """空表会让下面的 parametrize 静默通过——守卫自己也得被守卫。"""
    assert SCOPE_FAMILIES
    assert all(len(family) > 1 for family in SCOPE_FAMILIES)


@pytest.mark.parametrize("before_noun,after_noun", PAIRS)
def test_same_family_wording_is_not_a_boundary_loss(before_noun: str, after_noun: str):
    before = f"本结论仅在 3 个{before_noun}上得到验证。"
    after = f"本结论仅在 3 个{after_noun}上得到验证。"
    source = f"实验覆盖 3 个{before_noun}。"
    assert classify_qualification(before, normalize_text(source)) == "boundary"
    assert classify_qualification(after, normalize_text(source)) == "boundary"
    assert check_boundaries(before, after, source) is None, f"{before_noun}→{after_noun} 是合法同族写法"


def test_deleting_the_whole_family_is_rejected():
    before = "本结论仅在 3 个数据集上得到验证。"
    for after in ("本结论成立。", "本结论在多个场合得到认可。"):
        assert check_boundaries(before, after, "实验覆盖 3 个数据集。") is not None


@pytest.mark.parametrize("family", SCOPE_FAMILIES)
def test_every_family_survives_across_positions(family: tuple[str, ...]) -> None:
    """把族词放在句子第 48 字之后也必须受保护——旧写法取自截断文本，这里必然漏。"""
    first, second = family[0], family[-1]
    pad = "该结论受到多项实验条件约束，其中人群构成、随访周期与指标口径均已逐项核对，"
    before = f"{pad}仅在 3 个{first}上得到验证。"
    after = f"{pad}仅在 3 个{second}上得到验证。"
    source = f"实验覆盖 3 个{first}。"
    assert len(before) > 48, "构造失效： padding 没把族词推到截断点之后"
    assert check_boundaries(before, after, source) is None
    lost = "、".join("/".join(sorted(group)) for group in _scope_groups(before, source))
    assert normalize_text(first) in lost and normalize_text(second) in lost, "同族写法必须进同一保护组"


def test_ungrounded_removal_still_allowed_without_source():
    """无原文时边界按未验证受保护，但零指称的降级句依然允许删除。"""
    before = "这或许只能被视为探索性证据，需谨慎解读。"
    assert check_boundaries(before, "本方法优于基线。", None) is None
