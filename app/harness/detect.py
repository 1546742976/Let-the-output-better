"""本地 harness 自动发现：纯文件系统扫描，另提供显式的按需探测。

安全自洽性：为了填健康列需要执行"从磁盘找到的文件"，因此这里把可执行面压到最小——

- 只考虑内置 profile 声明过的包名与入口落点；
- 候选解析后必须仍在该包目录内（拒绝越界软链），Windows 上必须是带 ``MZ`` 头的
  真实 PE（``.cmd``/``.ps1``/``.sh`` 与 npm 的 postinstall 桩一律拒执）；
- 探测参数只来自 profile 常量元组，stdin 关闭、不喂 task、有独立短超时；
- :func:`scan` 不执行任何程序，可在导入期与启动期安全调用；:func:`probe` 才会执行，
  只由 ``GET /v1/harnesses?probe=true`` 显式触发，失败一律降级为"已检测未验证"。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from app.harness.profiles import PROFILES, HarnessProfile, cli_profiles

MAX_PACKAGE_JSON_BYTES = 256_000
MAX_NPM_ROOTS = 8
PROBE_TEXT_LIMIT = 200
PROBE_TIMEOUT_S = 5.0
#: Windows 下这些扩展名不是可直接 exec 的程序；PATH 命中它们只能当指针。
SCRIPT_SUFFIXES = {".cmd", ".bat", ".ps1", ".sh", ".txt", ".js", ".json"}
#: 小于该字节数的 .exe 基本可以断定是 npm 留下的 sh 桩。
STUB_MAX_BYTES = 4096


@dataclass(frozen=True)
class HarnessCandidate:
    profile: str
    label: str
    drivable: bool
    #: 实际要 exec 的程序（node-js 形态下是宿主 node），None 表示没找到可信入口。
    executable: Path | None = None
    #: 入口脚本或可执行文件本身。
    entry: Path | None = None
    root: Path | None = None
    status: str = "已检测未验证"
    detail: str = ""
    version: str | None = None
    login: str | None = None
    #: argv 前缀：native 是 (程序,)，node-js 是 (宿主, 脚本)。缺了脚本会探测到宿主的版本。
    prefix: tuple[str, ...] = ()

    @property
    def launch(self) -> list[str]:
        return list(self.prefix)


def _path_dirs() -> list[Path]:
    return [Path(item) for item in os.environ.get("PATH", "").split(os.pathsep) if item.strip()]


def _npm_roots() -> list[Path]:
    """找全局 node_modules：优先用 PATH 上的目录推断，再补几个常见安装位置。

    绝不用 ``npm root -g``——那等于在扫描阶段执行外部程序。
    """
    candidates = [directory / "node_modules" for directory in _path_dirs()]
    home = Path.home()
    candidates.extend(
        [
            home / ".npm-global" / "node_modules",
            Path("/usr/local/lib/node_modules"),
            Path("/usr/lib/node_modules"),
        ]
    )
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
        if len(roots) >= MAX_NPM_ROOTS:
            break
    return roots


def _is_pe(path: Path) -> bool:
    try:
        if path.stat().st_size <= STUB_MAX_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(2) == b"MZ"
    except OSError:
        return False


def _is_runnable(path: Path) -> bool:
    """能否被 create_subprocess_exec 直接执行（不含宿主+脚本形态）。"""
    if not path.is_file():
        return False
    if os.name == "nt":
        return path.suffix.lower() not in SCRIPT_SUFFIXES and _is_pe(path)
    return os.access(path, os.X_OK)


def _inside(child: Path, root: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _package_bins(package_dir: Path) -> list[str]:
    manifest = package_dir / "package.json"
    if not manifest.is_file():
        return []
    try:
        if manifest.stat().st_size > MAX_PACKAGE_JSON_BYTES:
            return []
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    bin_field = data.get("bin")
    if isinstance(bin_field, str):
        return [bin_field]
    if isinstance(bin_field, dict):
        return [value for value in bin_field.values() if isinstance(value, str)]
    return []


def _resolve_host(profile: HarnessProfile) -> Path | None:
    for name in profile.host_names:
        found = _which_runnable(name)
        if found is not None:
            return found
    return None


def _which_runnable(name: str) -> Path | None:
    """在 PATH 上找"真的能 exec 的那个"，跳过 .cmd/.ps1 这类包装。"""
    for directory in _path_dirs():
        for candidate in (directory / name, directory / f"{name}.exe"):
            if _is_runnable(candidate):
                return candidate
    return None


def _entry_candidates(profile: HarnessProfile, package_dir: Path) -> list[Path]:
    found: list[Path] = []
    for rel in profile.entrypoints:
        found.append(package_dir / rel)
    for rel in _package_bins(package_dir):
        found.append(package_dir / rel)
    nested = package_dir / "node_modules"
    if nested.is_dir():
        names = {Path(rel).name for rel in profile.entrypoints} | {c.name for c in found}
        with contextlib.suppress(OSError):
            for sub in sorted(nested.glob("*/bin/*")):
                if sub.name in names or sub.suffix.lower() == ".exe":
                    found.append(sub)
    return found


def _entry_for(profile: HarnessProfile, package_dir: Path) -> tuple[Path | None, str]:
    """返回(入口, 说明)。入口对 node-js 是脚本，对 native 必须本身可执行。"""
    for candidate in _entry_candidates(profile, package_dir):
        if not candidate.is_file() or not _inside(candidate, package_dir):
            continue
        if profile.entry == "node-js":
            if candidate.suffix.lower() == ".js":
                return candidate, ""
            continue
        if _is_runnable(candidate):
            return candidate, ""
    return None, "未找到可信入口（声明的 bin 可能是未跑 postinstall 留下的脚本桩）"


def scan(profiles: list[HarnessProfile] | None = None) -> list[HarnessCandidate]:
    """发现本机已安装的 profile harness。只读文件系统，不执行任何程序。"""
    targets = cli_profiles() if profiles is None else profiles
    roots = _npm_roots()
    results: list[HarnessCandidate] = []
    for profile in targets:
        candidate = _scan_profile(profile, roots)
        if candidate is not None:
            results.append(candidate)
    return results


def _scan_profile(profile: HarnessProfile, roots: list[Path]) -> HarnessCandidate | None:
    for root in roots:
        for package in profile.npm_packages:
            package_dir = root / package
            if not package_dir.is_dir():
                continue
            entry, detail = _entry_for(profile, package_dir)
            if entry is None:
                return HarnessCandidate(
                    profile=profile.name,
                    label=profile.label,
                    drivable=profile.drivable,
                    root=package_dir,
                    status="已检测未验证",
                    detail=detail,
                )
            host = _resolve_host(profile) if profile.entry == "node-js" else None
            executable = host if profile.entry == "node-js" else entry
            if profile.entry == "node-js" and host is None:
                return HarnessCandidate(
                    profile=profile.name,
                    label=profile.label,
                    drivable=profile.drivable,
                    entry=entry,
                    root=package_dir,
                    status="已检测未验证",
                    detail="找到入口脚本，但 PATH 上没有可执行的宿主（node）",
                )
            status = "可执行" if profile.drivable else "不支持驱动"
            detail = "" if profile.drivable else "非交互调用契约未验证，只检测不驱动"
            prefix = (str(host), str(entry)) if profile.entry == "node-js" else (str(entry),)
            return HarnessCandidate(
                profile=profile.name,
                label=profile.label,
                drivable=profile.drivable,
                executable=executable,
                entry=entry,
                root=package_dir,
                status=status,
                detail=detail,
                prefix=prefix,
            )
    # 退化路径：只装在 PATH 上（例如发行版直接放进 /usr/local/bin）。
    for name in profile.path_names:
        found = _which_runnable(name)
        if found is not None and profile.entry == "native":
            return HarnessCandidate(
                profile=profile.name,
                label=profile.label,
                drivable=profile.drivable,
                executable=found,
                entry=found,
                root=found.parent,
                status="可执行" if profile.drivable else "不支持驱动",
                prefix=(str(found),),
            )
    return None


async def _run_probe(argv: list[str], timeout_s: float) -> tuple[int | None, str]:
    """执行一次探测。永不抛错，返回(退出码, 首行输出)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError):
        return None, ""
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max(0.1, timeout_s))
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(asyncio.TimeoutError, OSError):
            await asyncio.wait_for(proc.wait(), 2.0)
        return None, "探测超时"
    text = (stdout or b"").decode("utf-8", errors="replace") or (stderr or b"").decode("utf-8", errors="replace")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return proc.returncode, first[:PROBE_TEXT_LIMIT]


async def probe(candidate: HarnessCandidate, *, timeout_s: float = PROBE_TIMEOUT_S) -> HarnessCandidate:
    """补版本与登录态。参数全部来自 profile 常量，失败只降级状态。"""
    profile = PROFILES.get(candidate.profile)
    if profile is None or candidate.executable is None or not candidate.drivable:
        return candidate
    base = candidate.launch
    if not base:
        return candidate

    version: str | None = None
    login: str | None = None
    status = candidate.status
    detail = candidate.detail

    if profile.probe_version:
        code, text = await _run_probe([*base, *profile.probe_version], timeout_s)
        if code == 0 and text:
            version = text
        else:
            status = "已检测未验证"
            detail = detail or f"版本探测未成功（退出码 {code}）"

    if profile.probe_login and version is not None:
        login_args, marker = profile.probe_login
        code, text = await _run_probe([*base, *login_args], timeout_s)
        if code == 0 and marker.lower() in text.lower():
            login = text
        elif code == 0:
            login = f"未登录或状态未知：{text}" if text else "未登录或状态未知"

    return replace(candidate, version=version, login=login, status=status, detail=detail)


def as_dict(candidate: HarnessCandidate) -> dict[str, object]:
    """给接口用的扁平视图；只输出路径字符串，不含任何可执行内容。"""
    return {
        "profile": candidate.profile,
        "label": candidate.label,
        "drivable": candidate.drivable,
        "status": candidate.status,
        "detail": candidate.detail,
        "version": candidate.version,
        "login": candidate.login,
        "executable": str(candidate.executable) if candidate.executable else None,
        "entry": str(candidate.entry) if candidate.entry else None,
    }
