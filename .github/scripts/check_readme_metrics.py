"""Fail CI when README §13 quotes test or coverage numbers that this run disproves.

历史背景：同一句话在本仓库被写错过多次（259/91.06 → 262/89.61 → 263/89.91 → 299/93.21），
每次都要人肉拉 CI 日志才发现。这个守卫把 README 的宣称值与**同一次运行**产出的
``junit.xml`` / ``coverage.json`` 直接比对，漂移即失败。

只在 windows runner 上跑：README §13 的数字标注的是 win32，换平台会差 0.1~0.2pp
（实测 linux 恒高约 0.15pp），在 ubuntu 上比对只会产生假告警。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# README §13 的固定句式，两处宣称值各缺一处都视为口径被改写、需要人工确认。
TEST_CLAIM = re.compile(r"(\d+)\s*项用例通过")
COVERAGE_CLAIM = re.compile(r"综合覆盖率\s*([\d.]+)%")
# 同平台同解释器下 coverage.py 的两位小数应可复现；留 0.05pp 余量吸收末位舍入。
COVERAGE_TOLERANCE = 0.05


def passed_count(junit_path: Path) -> tuple[int, int]:
    """返回 (通过数, 跳过数)。junit 的 tests 含 skipped，而 README 说的是"通过"。"""
    suite = ET.parse(junit_path).getroot().find("testsuite")
    if suite is None:
        raise ValueError(f"{junit_path} 里没有 testsuite 节点")
    tests = int(suite.get("tests", "0"))
    skipped = int(suite.get("skipped", "0"))
    failures = int(suite.get("failures", "0")) + int(suite.get("errors", "0"))
    if failures:
        raise ValueError(f"{junit_path} 报告 {failures} 个失败/错误，应先修测试再来核对文档")
    return tests - skipped, skipped


def covered_percent(coverage_path: Path) -> float:
    payload: dict[str, Any] = json.loads(coverage_path.read_text(encoding="utf-8"))
    totals = payload.get("totals") or {}
    if "percent_covered" not in totals:
        raise ValueError(f"{coverage_path} 缺少 totals.percent_covered")
    return float(totals["percent_covered"])


def check_readme(readme_path: Path, junit_path: Path, coverage_path: Path) -> list[str]:
    text = readme_path.read_text(encoding="utf-8")
    tests, skipped = passed_count(junit_path)
    actual_coverage = covered_percent(coverage_path)
    problems: list[str] = []

    claimed_tests = TEST_CLAIM.findall(text)
    if not claimed_tests:
        problems.append(f"{readme_path} 里找不到「N 项用例通过」的说法，守卫失去比对对象")
    for raw in claimed_tests:
        if int(raw) != tests:
            problems.append(f"用例数不符：README 写 {raw}，本次实跑 {tests}")

    claimed_coverage = COVERAGE_CLAIM.findall(text)
    if not claimed_coverage:
        problems.append(f"{readme_path} 里找不到「综合覆盖率 X%」的说法，守卫失去比对对象")
    for raw in claimed_coverage:
        if abs(float(raw) - actual_coverage) > COVERAGE_TOLERANCE:
            problems.append(
                f"覆盖率不符：README 写 {raw}%，本次实跑 {actual_coverage:.2f}%（容差 {COVERAGE_TOLERANCE}pp）"
            )

    note = f"（另有 {skipped} 项跳过）" if skipped else ""
    print(f"README 指标核对基准：{tests} 项通过{note}，覆盖率 {actual_coverage:.2f}%")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--junit", type=Path, required=True, help="pytest --junitxml 产物")
    parser.add_argument("--coverage", type=Path, required=True, help="pytest --cov-report=json 产物")
    args = parser.parse_args()
    try:
        problems = check_readme(args.readme, args.junit, args.coverage)
    except (OSError, UnicodeError, ValueError, ET.ParseError) as exc:
        print(f"Cannot verify README metrics: {exc}", file=sys.stderr)
        return 1
    if problems:
        print("README metrics drift detected:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        print("→ 用本次实跑值更新 README §13，或在 PR 里说明为何该数字应当保持。", file=sys.stderr)
        return 1
    print("README metrics match this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
