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
# 同平台同解释器下 coverage.py 的结果也只能近似复现：依赖补丁版本、临时目录路径
# 这类因素就能让分支计数差出 0.05pp 量级（实测本机 win32 3.13 报 94.15%，CI
# windows-latest 3.13 判为漂移）。所以容差取 0.5pp：仍然拦得住"文档停留在上一个
# 时代"这类真实漂移（历史上 91.06/89.61/89.91 与当前值相差 4pp 以上），又不把
# 最后一位小数变成 CI 的随机失败源。用例数保持逐位精确比对。
COVERAGE_TOLERANCE = 0.5


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


def _annotate(level: str, message: str) -> None:
    """写成 workflow command，让差异进 annotation。

    作业日志需要仓库 admin 才能下载，贡献者（含 CI 之外的排查者）看不到 stderr；
    annotation 走 check-runs API 匿名可读。差异内容本身就是一行数字，不落进可机器
    读取的通道，等于每次都要人肉找有权限的人拉日志。
    """
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{escaped}")


#: 延迟类指标不比对：runner 的计时抖动是噪声，比了只会制造假红。
UNSTABLE_METRICS = frozenset({"额外延迟 P95"})


def check_evaluation(readme_path: Path, metrics_path: Path) -> list[str]:
    """README §9 的实测表必须与同一次 evaluate 跑出的机读汇总一致。

    指标全部由 `scripts/evaluate.py` 计算并落成 JSON，这里只做比对——不在守卫里
    重算一遍公式，等于制造第二份会各自漂移的实现。
    """
    payload: dict[str, Any] = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics = payload.get("metrics") or {}
    if not metrics:
        return [f"{metrics_path} 里没有 metrics，守卫失去比对对象"]
    text = readme_path.read_text(encoding="utf-8")
    problems: list[str] = []
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [part.strip() for part in line.strip().strip("|").split("|")]
        # §9 的表是"指标 | 目标 | 实测 | 达标"四列，其余四列表格（如指标定义表）名字对不上自然被跳过
        if len(cells) == 4 and cells[0] in metrics:
            rows[cells[0]] = cells
    for name, expected in metrics.items():
        if name in UNSTABLE_METRICS:
            continue
        if name not in rows:
            problems.append(f"README §9 缺少指标行：{name}")
            continue
        _, claimed_target, claimed_measured, claimed_met = rows[name]
        if claimed_measured != expected["measured"]:
            problems.append(f"{name}：README 写 {claimed_measured!r}，本次实跑 {expected['measured']!r}")
        if claimed_met != ("是" if expected["met"] else "否"):
            problems.append(
                f"{name}：README 达标列为 {claimed_met!r}，本次实跑判为 {'是' if expected['met'] else '否'}"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument("--junit", type=Path, help="pytest --junitxml 产物")
    parser.add_argument("--coverage", type=Path, help="pytest --cov-report=json 产物")
    parser.add_argument("--evaluation", type=Path, help="scripts/evaluate.py 的 .metrics.json 汇总")
    args = parser.parse_args()
    if not args.junit and not args.evaluation:
        print("至少要给 --junit/--coverage 或 --evaluation 中的一组", file=sys.stderr)
        return 2
    problems: list[str] = []
    if args.junit and args.coverage:
        try:
            problems += check_readme(args.readme, args.junit, args.coverage)
        except (OSError, UnicodeError, ValueError, ET.ParseError) as exc:
            problems.append(f"无法核对用例数/覆盖率：{exc}")
    if args.evaluation:
        try:
            problems += check_evaluation(args.readme, args.evaluation)
        except (OSError, UnicodeError, ValueError, KeyError) as exc:
            problems.append(f"无法核对 §9 指标表：{exc}")
    if problems:
        print("README metrics drift detected:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
            _annotate("error", problem)
        print("→ 用本次实跑值更新 README §13 或 §9，或在 PR 里说明为何该数字应当保持。", file=sys.stderr)
        return 1
    print("README metrics match this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
