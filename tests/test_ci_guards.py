"""CI 守卫自身的比对语义。

这条守卫只在 Windows job 上跑，本地永远碰不到它的分支；而它判红判绿的依据是文档
句式，句式一改就可能把"永远通过"或"永远失败"写进 CI 而无人察觉。这里用合成产物
把三种情形钉住：下界成立、下界失守、下界过时。
"""

from __future__ import annotations

import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "check_readme_metrics.py"


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    assert SCRIPT.is_file(), f"找不到守卫脚本：{SCRIPT}"
    spec = importlib.util.spec_from_file_location("check_readme_metrics", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(
    guard: ModuleType, tmp_path: Path, claim: str, *, tests: int, coverage: float = 94.74
) -> tuple[list[str], list[str]]:
    """README 里的覆盖率宣称值固定写 94.74%，`coverage` 是产物里的实跑值。"""
    junit = tmp_path / "junit.xml"
    # pytest 的产物是 <testsuites><testsuite …/></testsuites>，守卫按子节点找 testsuite。
    root = ET.Element("testsuites")
    ET.SubElement(root, "testsuite", name="pytest", tests=str(tests), skipped="0", failures="0", errors="0")
    ET.ElementTree(root).write(junit, encoding="unicode")
    metrics = tmp_path / "coverage.json"
    metrics.write_text(json.dumps({"totals": {"percent_covered": coverage}}), encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(f"实测：{claim}，`app/` 行与分支综合覆盖率 94.74%。\n", encoding="utf-8")
    return guard.check_readme(readme, junit, metrics)


def test_floor_holds_without_exact_match(guard: ModuleType, tmp_path: Path) -> None:
    """加用例不该回来改文档：实跑高于下界即通过。"""
    problems, warnings = check(guard, tmp_path, "≥550 项用例通过", tests=600)
    assert problems == []
    assert warnings == []


def test_below_the_floor_is_a_failure(guard: ModuleType, tmp_path: Path) -> None:
    """文档说有 550 条只跑出 549 条，才是真漂移。"""
    problems, _ = check(guard, tmp_path, "≥550 项用例通过", tests=549)
    assert any("低于" in problem and "549" in problem for problem in problems), problems


def test_stale_floor_warns_without_failing(guard: ModuleType, tmp_path: Path) -> None:
    """下界落后太多是维护事项：抬下界归人，不该由 CI 拦下整个 PR。"""
    gap = int(guard.FLOOR_STALE_GAP)
    problems, warnings = check(guard, tmp_path, "≥550 项用例通过", tests=550 + gap)
    assert problems == []
    assert any("过时" in warning for warning in warnings), warnings


def test_absolute_count_is_no_longer_accepted(guard: ModuleType, tmp_path: Path) -> None:
    """写回绝对数必须被抓住：那正是本次要消掉的失败模式，不能悄悄退回旧口径。"""
    problems, _ = check(guard, tmp_path, "554 项用例通过", tests=554)
    assert any("下界" in problem for problem in problems), problems


def test_coverage_still_compared_value_by_value(guard: ModuleType, tmp_path: Path) -> None:
    """覆盖率没有"只增不减"的性质，逐值比对必须还在。"""
    drift = float(guard.COVERAGE_TOLERANCE) + 1
    problems, _ = check(guard, tmp_path, "≥550 项用例通过", tests=554, coverage=94.74 + drift)
    assert any("覆盖率不符" in problem for problem in problems), problems
