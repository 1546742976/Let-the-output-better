"""事实锚点与数值口径：采纳闸门第一条检查的确切边界。

这里每一条都对应一个真实缺陷：千分位被拆成两个 token 造成误拒、负号与单位被
悄悄改掉造成漏拦、缩写紧贴汉字时 `\b` 根本不匹配。把 `_ACRONYM`/`_NUMBER` 换回
旧写法，本文件立刻变红。
"""

from __future__ import annotations

import pytest

from app.optimizer.steps.simplify import check_facts
from app.plain import acronym_anchors, canonical_number, canonical_numbers, unit_classes

CASES_PASS = [
    ("样本共 1,200,300 条。", "样本共 1200300 条。"),  # 千分位写法不该算事实变化
    ("准确率为 12.50%。", "准确率为 12.5%。"),  # 精度写法
    ("发表于 CVPR 2023。", "发表于 CVPR2023。"),
    ("研究期为 2019-2023 年。", "研究期为 2019 至 2023 年。"),  # 区间连接号不是负号
    ("在 3 个数据集上验证。", "共覆盖 3 个数据集。"),
]

CASES_REJECT = [
    ("误差降低 -12.5%。", "误差降低 12.5%。", "数值变化"),  # 丢符号＝改了断言
    ("准确率为 88.5%。", "准确率为 88.5 个百分点。", "单位类别变化"),
    ("该方法发表于 CVPR。", "该方法发表于顶级会议。", "关键表述丢失"),
    ("发表于CVPR，样本量 24。", "发表于顶级会议，样本量 24。", "关键表述丢失"),  # 紧贴汉字
    ("在 NEURIPS 上评测。", "在会议上评测。", "关键表述丢失"),  # 旧写法上限 6 字母会漏
    ("提升 12.5%。", "提升明显。", "数值变化"),
]


@pytest.mark.parametrize("before,after", CASES_PASS)
def test_legitimate_rewrites_are_not_rejected(before: str, after: str):
    assert check_facts(before, after) is None, f"{before} → {after} 被误拒"


@pytest.mark.parametrize("before,after,reason", CASES_REJECT)
def test_fact_changes_are_rejected(before: str, after: str, reason: str):
    violation = check_facts(before, after)
    assert violation is not None and reason in violation, f"{before} → {after} 没被拦住"


def test_canonical_number_forms():
    assert canonical_number("1,200,300") == "1200300"
    assert canonical_number("12.50") == "12.5"
    assert canonical_number("−12.5") == "-12.5", "排版用的短横也要认"
    assert canonical_number("100") == "100", "归一后不能变成 1E+2"


def test_canonical_numbers_treat_range_as_two_years():
    assert canonical_numbers("研究期为 2019-2023 年。") == canonical_numbers("研究期为 2019 至 2023 年。")


def test_unit_classes_separate_percent_from_points():
    assert unit_classes("提升 88.5%。")["百分号"] == 1
    assert unit_classes("提升 88.5 个百分点。")["百分点"] == 1
    assert unit_classes("提升 88.5% 即 2.3 个百分点。") == {"百分号": 1, "百分点": 1}


def test_acronym_anchors_ignore_all_caps_function_words():
    assert "CVPR" in acronym_anchors("发表于CVPR，样本量 24。")
    assert acronym_anchors("AND the results") == set(), "全大写功能词不是不可再生的专名"
    assert acronym_anchors("Abbott 公司") == set(), "首字母大写的普通单词不该被当缩写"
