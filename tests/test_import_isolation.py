"""导入隔离：每个对外模块都必须能作为**第一个**导入的模块被加载。

模块间的依赖只有在没有环时才可比；`import app.settings` 单独失败这类问题，靠跑完整
测试套件是发现不了的——套件里 `app.main` 之类总是先被导入，环被顺序掩盖了。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 对外可达的入口，按字母序；顺序无关紧要，关键是每个都在干净解释器里单独导入。
ENTRIES = (
    "app.audit",
    "app.harness",
    "app.harness.api",
    "app.harness.command",
    "app.harness.detect",
    "app.harness.profiles",
    "app.main",
    "app.optimizer",
    "app.plain",
    "app.providers",
    "app.schemas",
    "app.settings",
    "app.textio",
    "app.viewer",
)


@pytest.mark.parametrize("module", ENTRIES)
def test_module_imports_standalone(module: str):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"import {module} 失败：\n{result.stderr[-800:]}"


def test_harness_package_does_not_eagerly_pull_providers():
    """`app.harness` 导入后不得已经把 `app.providers` 拖进 sys.modules。

    这是那条反指边的实际形状：ApiHarness 需要 providers，providers 需要 settings，
    settings 需要 app.harness.command。按名取用时才付这次导入。
    """
    result = subprocess.run(
        [sys.executable, "-c", "import sys, app.harness; print('app.providers' in sys.modules)"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    assert result.stdout.strip() == "False"
