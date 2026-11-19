"""本地查看页：页面挂载、环境信息与只读审计尾部。

稳定对外契约仍是 ``optimize()`` 与 ``POST /v1/optimize``；本模块只提供本地调试用的
只读视图，不参与优化逻辑，也不接受调用方指定文件路径。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import progress
from app.harness.detect import as_dict, probe_cache_size, scan
from app.harness.detect import probe as probe_candidate
from app.harness.profiles import PROFILES
from app.schemas import SCHEMA_REGISTRY, optional_field_names, required_field_names
from app.settings import apply_overrides, collect_config_warnings

WEB_DIR = Path(__file__).parent / "web"
DEFAULT_AUDIT_ITEMS = 50
MAX_AUDIT_ITEMS = 200
# 读取窗口上限：避免审计文件很大时为了统计损坏行而解析整个文件。
MAX_SCANNED_LINES = 2000
#: 窗口字节预算。审计每行含 raw_output 与 final_output 全文，单行可达数百 KB：
#: 只按行数封顶时 2000 行足以把整段请求体堆进内存，页面一次刷新就变成放大点。
MAX_SCANNED_BYTES = 8 * 1024 * 1024
# 反向读取的块大小。窗口只物化 scan_cap 行，不再整文件 read_text() 进内存。
READ_BLOCK = 65536


def _read_tail_window(handle: Any, size: int, cap: int, byte_cap: int) -> tuple[list[bytes], bool, int]:
    """从文件尾部按块倒序收集至多 ``cap`` 个非空行，累计不超过 ``byte_cap`` 字节。

    返回 ``(window, reached_bof, full_count)``：``window`` 按文件顺序（旧→新），至多
    ``cap`` 行；``reached_bof`` 表示是否已读到文件头。每块切分时最左一段可能是被块边界
    截断的半行，留作 ``carry`` 与更左一块拼接，因此单行长度不受 ``READ_BLOCK`` 限制。
    ``full_count`` 是本次读取实际见到的非空行数——``reached_bof`` 为真时它就是全文件总数，
    否则只是窗口下界（据此把 ``total_lines`` 标成不精确）。
    """
    if size == 0:
        return [], True, 0
    out: list[bytes] = []
    collected = 0
    carry = b""
    pos = size
    reached_bof = False
    while pos > 0:
        start = max(0, pos - READ_BLOCK)
        handle.seek(start)
        data = handle.read(pos - start)
        pos = start
        parts = (data + carry).split(b"\n")
        carry = parts[0]
        # parts[1:] 右边界与上一块对齐，是本次已完整的行；过滤空白行后拼到窗口前部
        out = [part for part in parts[1:] if part.strip()] + out
        if pos == 0:
            reached_bof = True
            break
        collected = sum(len(part) for part in out)
        if len(out) >= cap or collected >= byte_cap:
            break
    if reached_bof and carry.strip():
        out = [carry] + out
    return (out[-cap:] if len(out) > cap else out), reached_bof, len(out)


def read_audit_tail(path: str | Path, limit: int) -> dict[str, Any]:
    """倒序读取审计记录尾部，损坏行跳过。

    只反向读窗口所需的块，不再把整个文件载入内存。``skipped`` 统计扫描窗口内的损坏行。
    ``reached_bof`` 时 ``total_lines`` 是全文件非空行数、``total_exact`` 为真；窗口被截断
    （大文件）时 ``total_lines`` 只是下界、``total_exact`` 转假，页面据此不再展示"总数"。
    每条记录附带 ``line_no``（读到文件头时为全文件 1 基行号，否则为窗口内序号）作为页面
    选中依据——``request_id`` 已含进程 nonce 不跨进程重号，但行号定位到具体一行更直接。
    """
    audit_path = Path(path)
    wanted = max(1, min(limit, MAX_AUDIT_ITEMS))
    empty = {
        "items": [],
        "path": str(audit_path),
        "total_lines": 0,
        "scanned": 0,
        "skipped": 0,
        "total_exact": True,
        "error": "",
    }
    if not audit_path.exists():
        return empty
    # AUDIT_PATH 被配成目录、或当前账号读不动时，只让历史面板坏掉：
    # 500 会让整页看起来失败，而优化面本来与它无关。
    if not audit_path.is_file():
        return {**empty, "total_exact": False, "error": f"AUDIT_PATH 不是文件：{audit_path}"}

    scan_cap = min(max(wanted * 10, 200), MAX_SCANNED_LINES)
    try:
        size = audit_path.stat().st_size
        with audit_path.open("rb") as handle:
            window, reached_bof, full_count = _read_tail_window(handle, size, scan_cap, MAX_SCANNED_BYTES)
    except OSError as exc:
        return {**empty, "total_exact": False, "error": f"审计文件读取失败：{exc}"}

    # line_no 以全量计数为基准，窗口被截断时仍指向文件内真实行号（未读到的部分不改变其序号）
    items: list[dict[str, Any]] = []
    skipped = 0
    for offset, raw in enumerate(reversed(window)):
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            skipped += 1
            continue
        if isinstance(record, dict):
            items.append({**record, "line_no": full_count - offset})
        else:
            skipped += 1
        if len(items) >= wanted:
            break
    return {
        "items": items,
        "path": str(audit_path),
        "total_lines": full_count,
        "scanned": len(window),
        "skipped": skipped,
        "total_exact": reached_bof,
        "error": "",
    }


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=page)


def _available_profiles(request: Request) -> dict[str, Any]:
    harness = getattr(request.app.state, "harness", None)
    if harness is None:
        return {}
    profiles = getattr(harness, "profiles", None)
    if callable(profiles):
        return profiles()
    return {getattr(harness, "name", "command"): harness}


def _harness_view(request: Request) -> dict[str, Any]:
    """页面据此决定下拉项与按钮状态；可用集合来自实际装配结果，不是配置文件猜测。"""
    settings = request.app.state.settings
    harness = getattr(request.app.state, "harness", None)
    members = _available_profiles(request)
    default = getattr(harness, "default", None) or next(iter(members), None)
    return {
        "enabled": settings.harness_enabled,
        "name": default,
        "timeout_ms": settings.harness_timeout_ms,
        "max_concurrency": settings.harness_max_concurrency,
        "detect": settings.harness_detect,
        "api_ready": bool(settings.openai_api_key),
        "options": sorted(members),
    }


@router.get("/v1/config")
async def runtime_config(request: Request) -> dict[str, Any]:
    """页面右栏与表单默认值所需的环境信息，取值口径与实际执行一致（含 pipeline.yaml 覆盖）。"""
    pipeline = request.app.state.pipeline
    settings = apply_overrides(request.app.state.settings, pipeline)
    format_step = pipeline.step("format")
    schema_step = pipeline.step("schema")
    return {
        "provider": settings.provider,
        # 装配期用实际生效的配置算，页面据此解释"为什么下拉里没有我刚写的那个 profile"
        "warnings": collect_config_warnings(request.app.state.settings),
        "score_threshold": settings.score_threshold,
        "timeout_ms": settings.timeout_ms,
        "max_repair_retries": settings.max_repair_retries,
        "fail_open": settings.fail_open,
        "audit_path": settings.audit_path,
        "harness": _harness_view(request),
        "reference_style": format_step.reference_style if format_step is not None else "gb-t-7714",
        "schema_strict": schema_step.strict if schema_step is not None else False,
        "steps": {step.name: {"enabled": step.enabled, "strict": step.strict} for step in pipeline.steps},
        "schemas": {
            name: {
                "required": required_field_names(model_cls) or [],
                "optional": optional_field_names(model_cls) or [],
            }
            for name, model_cls in SCHEMA_REGISTRY.items()
        },
    }


@router.get("/v1/audits")
async def audit_history(
    request: Request,
    limit: int = Query(default=DEFAULT_AUDIT_ITEMS, ge=1, le=MAX_AUDIT_ITEMS),
) -> dict[str, Any]:
    return read_audit_tail(request.app.state.settings.audit_path, limit)


@router.get("/v1/progress")
async def run_progress() -> dict[str, Any]:
    """在飞运行的阶段快照：页面轮询它把"转圈"换成"卡在哪一段"。只读，不收参数。"""
    return progress.snapshot()


@router.get("/v1/harnesses")
async def harness_status(
    request: Request,
    probe: bool = Query(default=False, description="为 true 时才执行版本/登录态探测"),
) -> dict[str, Any]:
    """本地 harness 健康表。

    默认只做文件系统扫描，**不执行任何被发现的程序**；``probe=true`` 才会按
    profile 常量参数跑一次 ``--version`` 之类的探测，且受独立短超时约束。
    """
    settings = request.app.state.settings
    candidates = scan()
    probed = bool(probe and settings.harness_enabled)
    if probed:
        timeout_s = settings.harness_probe_timeout_ms / 1000
        # 顺序 await 而不是 gather：探测会执行"从磁盘找到的文件"，
        # 与驱动面一样要限流；缓存与单飞锁在 detect 内部，这里只保证不同时排队多份。
        fresh = []
        for candidate in candidates:
            fresh.append(await probe_candidate(candidate, timeout_s=timeout_s))
        candidates = fresh
    available = sorted(_available_profiles(request))
    return {
        "enabled": settings.harness_enabled,
        "selected": _harness_view(request)["options"],
        "available": available,
        "api_ready": bool(settings.openai_api_key),
        "probed": probed,
        # 让"这次点检测其实没起新进程"这件事可见，否则缓存看起来像结果没刷新
        "probe_cached": probe_cache_size(),
        "detected": [as_dict(candidate) for candidate in candidates],
        "notes": {profile.name: list(profile.notes) for profile in PROFILES.values()},
    }


def mount(app: FastAPI) -> None:
    """挂载页面路由与静态资源；静态目录固定为包内 ``app/web``，不接受外部路径。"""
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    app.include_router(router)
