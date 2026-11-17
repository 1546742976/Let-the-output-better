from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

from app.harness import detect
from app.harness.profiles import CODEX, OPENCODE

SCRIPT_STUB = b"""#!/bin/sh
echo "Error: postinstall script was not run." >&2
exit 1
"""


def make_exe(path: Path, *, payload: bytes = b"MZ" + b"\x00" * 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_npm_tree(bin_dir: Path) -> None:
    """构造与真实机器同形的最小 npm 全局布局：node_modules 与命令 shim 同级。"""
    root = bin_dir / "node_modules"
    codex = root / "@openai" / "codex"
    (codex / "bin").mkdir(parents=True, exist_ok=True)
    (codex / "bin" / "codex.js").write_text("// codex entry\n", encoding="utf-8")
    (codex / "package.json").write_text(
        json.dumps({"name": "@openai/codex", "version": "0.1.0", "bin": {"codex": "./bin/codex.js"}}),
        encoding="utf-8",
    )
    opencode = root / "opencode-ai"
    # 声明的 bin 是 479 字节的 sh 桩，真实 exe 藏在平台子包里（本机实测形态）。
    make_exe(opencode / "bin" / "opencode.exe", payload=SCRIPT_STUB)
    nested = opencode / "node_modules" / "opencode-windows-x64"
    make_exe(nested / "bin" / "opencode.exe")
    (opencode / "package.json").write_text(
        json.dumps({"name": "opencode-ai", "version": "1.0.0", "bin": {"opencode": "./bin/opencode.exe"}}),
        encoding="utf-8",
    )


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    make_exe(bin_dir / "node.exe")
    make_npm_tree(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


class TestScanNeverExecutes:
    def test_scan_does_not_spawn_any_process(self, fake_env: Path, monkeypatch: pytest.MonkeyPatch):
        async def boom(*args, **kwargs):
            raise AssertionError("scan 阶段不允许执行任何程序")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        assert detect.scan()  # 没抛异常即证明未执行


class TestScan:
    def test_finds_codex_with_host_and_script_prefix(self, fake_env: Path):
        found = {c.profile: c for c in detect.scan()}
        assert "codex" in found, f"未找到 codex：{list(found)}"
        codex = found["codex"]
        assert codex.status == "可执行" and codex.drivable is True
        # argv 前缀必须同时含 node 与 codex.js，缺脚本会探测到宿主的版本。
        assert [Path(part).name for part in codex.launch] == ["node.exe", "codex.js"]

    def test_skips_stub_and_uses_nested_native_binary_for_opencode(self, fake_env: Path):
        opencode = {c.profile: c for c in detect.scan()}["opencode"]
        assert opencode.drivable is False
        assert opencode.status == "不支持驱动"
        assert "opencode-windows-x64" in str(opencode.entry)

    def test_nothing_detected_on_clean_machine(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        assert detect.scan() == []

    def test_cmd_wrapper_on_path_is_not_an_executable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "opencode.CMD").write_text("@echo off\r\n", encoding="utf-8")
        monkeypatch.setenv("PATH", str(bin_dir))
        found = detect._which_runnable("opencode")
        assert found is None

    def test_entry_outside_package_dir_is_rejected(self, tmp_path: Path):
        pkg = tmp_path / "bin" / "node_modules" / "opencode-ai"
        (pkg / "bin").mkdir(parents=True)
        make_exe(tmp_path / "evil.exe")
        # 唯一的入口声明指向包外：越界候选必须被拒，不能因为"文件存在"就放行。
        (pkg / "package.json").write_text(
            json.dumps({"name": "opencode-ai", "bin": {"opencode": "../../../../evil.exe"}}), encoding="utf-8"
        )
        entry, detail = detect._entry_for(OPENCODE, pkg)
        assert entry is None
        assert "未找到" in detail

    def test_missing_host_degrades_instead_of_failing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_npm_tree(bin_dir)  # 有 node_modules，但 PATH 上没有 node.exe
        monkeypatch.setenv("PATH", str(bin_dir))
        codex = {c.profile: c for c in detect.scan()}["codex"]
        assert codex.status == "已检测未验证"
        assert "宿主" in codex.detail


class TestProbe:
    async def test_probe_fills_version_and_login(self, fake_env: Path, monkeypatch: pytest.MonkeyPatch):
        codex = {c.profile: c for c in detect.scan()}["codex"]
        calls: list[list[str]] = []

        async def fake_run(argv, timeout_s):
            calls.append(list(argv))
            if "--version" in argv:
                return 0, "codex-cli 9.9.9"
            return 0, "Logged in using ChatGPT"

        monkeypatch.setattr(detect, "_run_probe", fake_run)
        probed = await detect.probe(codex, timeout_s=5)
        assert probed.version == "codex-cli 9.9.9"
        assert probed.login == "Logged in using ChatGPT"
        assert probed.status == "可执行"
        # 探测参数只能来自 profile 常量，且总是带完整前缀。
        assert all(call[:2] == codex.launch for call in calls)

    async def test_failed_version_probe_downgrades_status(self, fake_env: Path, monkeypatch: pytest.MonkeyPatch):
        codex = {c.profile: c for c in detect.scan()}["codex"]

        async def failing(argv, timeout_s):
            return 1, "boom"

        monkeypatch.setattr(detect, "_run_probe", failing)
        probed = await detect.probe(codex, timeout_s=5)
        assert probed.status == "已检测未验证"
        assert probed.version is None

    async def test_probe_never_touches_undrivable_profile(self, fake_env: Path, monkeypatch: pytest.MonkeyPatch):
        opencode = {c.profile: c for c in detect.scan()}["opencode"]

        async def boom(argv, timeout_s):
            raise AssertionError("不可驱动的 profile 不得被探测")

        monkeypatch.setattr(detect, "_run_probe", boom)
        assert (await detect.probe(opencode, timeout_s=5)).status == "不支持驱动"

    async def test_run_probe_swallows_spawn_errors(self, tmp_path: Path):
        code, text = await detect._run_probe([str(tmp_path / "definitely-missing")], timeout_s=1)
        assert code is None and text == ""

    async def test_run_probe_uses_devnull_stdin_and_constant_args(self, tmp_path: Path):
        """探测不喂 task：stdin 是 DEVNULL，只跑常量参数。"""
        script = tmp_path / "peek.py"
        script.write_text("import sys\nprint('got:' + repr(sys.stdin.read()))\n", encoding="utf-8")
        code, text = await detect._run_probe([sys.executable, str(script)], timeout_s=20)
        assert code == 0
        assert text == "got:''"  # stdin 是 DEVNULL，读不到 task


class TestAsDict:
    def test_shape_is_flat_and_path_only(self, fake_env: Path):
        item = detect.as_dict(detect.scan()[0])
        assert set(item) == {
            "profile",
            "label",
            "drivable",
            "status",
            "detail",
            "version",
            "login",
            "executable",
            "entry",
        }
        assert item["version"] is None  # 未探测前不得有版本

    def test_profiles_are_constants(self):
        assert CODEX.drivable is True and CODEX.requires_model_match is True
        assert "-s" in CODEX.argv_template and "read-only" in CODEX.argv_template
        assert OPENCODE.drivable is False
