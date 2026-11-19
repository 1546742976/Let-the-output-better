"""README 与代码的机械比对：把"文档停在另一个时代"变成 CI 红灯，而不是靠人眼。

历史上同一句话被写错过多次（259/91.06 → 262/89.61 → 263/89.91 → 299/93.21），
而运行指标数字只有 windows 作业那一条守卫管得到。这里覆盖的是**不依赖运行产物**
的那部分：环境变量默认值、评分权重、闸门阈值、样本数。
判据来自本轮审查：README 写了这个数，才允许有字面量断言——那就由本文件统一比对，
测试里不再各抄一份常量。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.optimizer.steps.simplify import GROWTH_LIMIT, MAX_FIELDS, MIN_LENGTH
from app.settings import PROSE_WEIGHTS, STRUCTURED_WEIGHTS, Settings

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def readme_lines() -> list[str]:
    return README.splitlines()


def env_defaults() -> dict[str, str]:
    """§8.1 的 ``| \`ENV\` | \`默认值\` | 说明 |`` 表格。"""
    table: dict[str, str] = {}
    for line in readme_lines():
        match = re.match(r"^\| `([A-Z][A-Z0-9_]*)` \| `([^`]*)` \|", line)
        if match:
            table[match.group(1)] = match.group(2)
    return table


def settings_defaults_by_alias() -> dict[str, Any]:
    # 必须 call_default_factory=True：list 字段只有 default_factory，
    # 不调用工厂就拿不到 []，比对会把正确写法判成不匹配。
    return {str(field.alias): field.get_default(call_default_factory=True) for field in Settings.model_fields.values()}


def test_every_env_row_matches_the_code_default():
    mismatches: list[str] = []
    for name, claimed in env_defaults().items():
        if name not in settings_defaults_by_alias():
            continue
        actual = settings_defaults_by_alias()[name]
        expected = _as_text(actual)
        if expected != claimed:
            line_no = next(index + 1 for index, line in enumerate(readme_lines()) if line.startswith(f"| `{name}` |"))
            mismatches.append(f"README 第 {line_no} 行说 {name} 默认 {claimed!r}，代码是 {expected!r}")
    assert not mismatches, "；".join(mismatches)


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value if value is not None else "")


def test_documented_weights_match_the_code_tables():
    """§2 与 §5 用中文散文写了两组权重，代码表变了它们就得跟着变。"""
    for key, value in PROSE_WEIGHTS.items():
        assert f"{key}: {value:g}" in README or f"{value:g}" in README, f"README 没有 {key} 的权重 {value}"
    # 中文表述形式："通俗度 0.35、自信度 0.3、简洁度 0.2、与原文一致性 0.15"
    prose_line = next(line for line in readme_lines() if line.startswith("- **确定性规则评分**"))
    for label, value in (
        ("通俗度", PROSE_WEIGHTS["plainness"]),
        ("自信度", PROSE_WEIGHTS["confidence"]),
        ("简洁度", PROSE_WEIGHTS["concision"]),
        ("一致性", PROSE_WEIGHTS["preservation"]),
    ):
        assert f"{label} {value:g}" in prose_line, f"§2 的 {label} 权重与代码 {value} 不一致"


def test_growth_limit_percent_is_documented():
    percent = f"{GROWTH_LIMIT:.0%}"
    assert f"超过原文 {percent}" in README, f"README 未同步 {percent} 的长度闸门"


def test_golden_sample_counts_match_the_dataset():
    rows = [
        json.loads(line) for line in (ROOT / "data" / "golden.jsonl").read_text(encoding="utf-8").splitlines() if line
    ]
    prose = [row for row in rows if row.get("schema") is None]
    section = README[README.index("#### mock 模式实测结果") :]
    assert f"{len(rows)} 条样本" in section, f"§9 写的样本数不是 {len(rows)}"
    assert f"其中 {len(prose)} 条散文" in section, f"§9 写的散文数不是 {len(prose)}"
    tree = next(line for line in readme_lines() if line.strip().startswith("│") and "golden.jsonl" in line)
    assert f"{len(rows)} 条" in tree, f"§12 结构树的金标集条数不是 {len(rows)}"
    assert f"{len(prose)} 条散文" in tree, f"§12 结构树的散文数不是 {len(prose)}"


def test_qualification_dataset_counts_match():
    rows = [
        json.loads(line)
        for line in (ROOT / "data" / "qualifications.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert rows, "标注集为空"
    # 历史增长写成"14 条 → 21 条"是叙述，不能当现状；现状由规范写法"标注集（现 N 条）"承担
    claim = re.search(r"标注集（现 (\d+) 条）", README)
    assert claim, 'README 缺少"标注集（现 N 条）"这一可核对的现状声明'
    assert int(claim.group(1)) == len(rows), f"README 说标注集现 {claim.group(1)} 条，文件里是 {len(rows)} 条"


@pytest.mark.parametrize("name", ["MAX_FIELDS", "MIN_LENGTH"])
def test_silent_truncation_limits_are_acknowledged(name: str) -> None:
    """这两个上限会静默少处理字段，README 必须写明——否则"没改写"看起来像"不需要改写"。"""
    value = {"MAX_FIELDS": MAX_FIELDS, "MIN_LENGTH": MIN_LENGTH}[name]
    assert str(value) in README or name in README, f"{name}={value} 这个截断行为在 README 里没有任何交代"
