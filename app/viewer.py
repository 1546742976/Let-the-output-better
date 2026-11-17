"""本地查看页：页面挂载、环境信息与只读审计尾部。

稳定对外契约仍是 ``optimize()`` 与 ``POST /v1/optimize``；本模块只提供本地调试用的
只读视图，不参与优化逻辑，也不接受调用方指定文件路径。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.harness.detect import as_dict, scan
from app.harness.detect import probe as probe_candidate
from app.harness.profiles import PROFILES
from app.schemas import SCHEMA_REGISTRY
from app.settings import apply_overrides

WEB_DIR = Path(__file__).parent / "web"
DEFAULT_AUDIT_ITEMS = 50
MAX_AUDIT_ITEMS = 200
# 读取窗口上限：避免审计文件很大时为了统计损坏行而解析整个文件。
MAX_SCANNED_LINES = 2000


def read_audit_tail(path: str | Path, limit: int) -> dict[str, Any]:
    """倒序读取审计记录，损坏行跳过。

    ``skipped`` 只统计本次扫描窗口内的损坏行；``total_lines`` 是整个文件的非空行数。
    每条记录附带 ``line_no``（文件内 1 基行号）：``request_id`` 由进程内计数器生成，
    多进程（如多次运行评估脚本）会写出重复 id，因此页面选中态必须用行号区分。
    """
    audit_path = Path(path)
    wanted = max(1, min(limit, MAX_AUDIT_ITEMS))
    if not audit_path.exists():
        return {"items": [], "path": str(audit_path), "total_lines": 0, "scanned": 0, "skipped": 0}

    lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    offset = max(0, len(lines) - max(wanted * 10, MAX_SCANNED_LINES))
    items: list[dict[str, Any]] = []
    skipped = 0
    for line_no in range(len(lines) - 1, offset - 1, -1):
        try:
            record = json.loads(lines[line_no])
        except ValueError:
            skipped += 1
            continue
        if isinstance(record, dict):
            items.append({**record, "line_no": line_no + 1})
        else:
            skipped += 1
        if len(items) >= wanted:
            break
    return {
        "items": items,
        "path": str(audit_path),
        "total_lines": len(lines),
        "scanned": len(lines) - offset,
        "skipped": skipped,
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
                "required": model_cls.required_fields(),
                "optional": model_cls.optional_fields(),
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
        candidates = list(
            await asyncio.gather(*(probe_candidate(candidate, timeout_s=timeout_s) for candidate in candidates))
        )
    available = sorted(_available_profiles(request))
    return {
        "enabled": settings.harness_enabled,
        "selected": _harness_view(request)["options"],
        "available": available,
        "api_ready": bool(settings.openai_api_key),
        "probed": probed,
        "detected": [as_dict(candidate) for candidate in candidates],
        "notes": {profile.name: list(profile.notes) for profile in PROFILES.values()},
    }


def mount(app: FastAPI) -> None:
    """挂载页面路由与静态资源；静态目录固定为包内 ``app/web``，不接受外部路径。"""
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    app.include_router(router)
