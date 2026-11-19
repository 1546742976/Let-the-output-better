"""FastAPI 入口：``POST /v1/optimize``、``GET /healthz``、本地查看页与默认任务路径 ``POST /v1/run``。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import progress
from app.harness import (
    HarnessBusy,
    HarnessError,
    HarnessTimeout,
    HarnessUnavailable,
    build_harness,
)
from app.optimizer import OptimizeResult, new_request_id, optimize
from app.providers import build_provider
from app.schemas import SCHEMA_REGISTRY
from app.settings import get_pipeline_config, get_settings
from app.viewer import mount as mount_viewer

#: 请求体长度上限。审计会把 raw_output/input_text 整段落盘，入口封顶比落盘截断诚实：
#: 后者会制造"审计里没有但发生过"的黑洞。上限与 HARNESS_MAX_OUTPUT_BYTES 同量级。
MAX_OUTPUT_CHARS = 200_000
MAX_INPUT_CHARS = 500_000
MAX_INSTRUCTION_CHARS = 8_000


class Options(BaseModel):
    """单次请求的步骤开关，语义与 ``pipeline.yaml`` 的 ``enabled`` 一致。

    ``extra="forbid"``：本服务唯一的第一方客户端是随包的本地页面。留着 "ignore" 时，
    开关名写错（页面曾把 `score` 写成 `rules`）就是彻底的静默失效——勾选框看着能点，
    管线照旧全跑，没有任何地方会报。未知键直接 422 是这里唯一可执行的诊断方式。
    """

    model_config = ConfigDict(extra="forbid")

    score: bool | None = None
    repair: bool | None = None
    format: bool | None = None
    simplify: bool | None = None
    strict: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=50, le=120000)


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    raw_output: str = Field(min_length=1, max_length=MAX_OUTPUT_CHARS)
    schema_name: str | None = Field(default=None, alias="schema")
    instruction: str = Field(default="", max_length=MAX_INSTRUCTION_CHARS)
    input_text: str = Field(default="", max_length=MAX_INPUT_CHARS)
    #: 逐请求覆盖 format 步骤的引用风格；缺省跟随 pipeline.yaml。
    reference_style: Literal["gb-t-7714", "apa"] | None = None
    options: Options = Field(default_factory=Options)


class RunRequest(BaseModel):
    """默认任务路径的请求体：只有 ``task`` 会交给 harness，命令本身不可由请求指定。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    task: str = Field(min_length=1, max_length=MAX_INSTRUCTION_CHARS)
    schema_name: str | None = Field(default=None, alias="schema")
    #: 只能选内置 profile 名；命令、参数、模型与 base_url 均不可由请求体指定。
    harness: str | None = None
    instruction: str = Field(default="", max_length=MAX_INSTRUCTION_CHARS)
    input_text: str = Field(default="", max_length=MAX_INPUT_CHARS)
    reference_style: Literal["gb-t-7714", "apa"] | None = None
    options: Options = Field(default_factory=Options)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.pipeline = get_pipeline_config()
    # 启动期即构造 provider：配置缺失或密钥为空在这里失败，不留到运行期。
    app.state.provider = build_provider(settings)
    app.state.harness = build_harness(settings)
    try:
        yield
    finally:
        await app.state.provider.aclose()
        # API 形态持有 httpx 客户端，必须一并关闭。
        harness = getattr(app.state, "harness", None)
        if harness is not None:
            await harness.aclose()


app = FastAPI(title="Let-the-output-better", version="0.2.0", lifespan=lifespan)
mount_viewer(app)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "provider": app.state.settings.provider}


def _check_schema(schema_name: str | None) -> None:
    if schema_name is not None and schema_name.lower() not in SCHEMA_REGISTRY:
        known = ", ".join(SCHEMA_REGISTRY)
        raise HTTPException(status_code=422, detail=f"未知 schema={schema_name!r}，内置可选：{known}")


async def _optimize_output(
    *,
    raw_output: str,
    schema_name: str | None,
    instruction: str,
    input_text: str,
    options: Options,
    audit_extra: dict[str, Any] | None = None,
    reference_style: str | None = None,
    request_id: str | None = None,
) -> OptimizeResult:
    """/v1/optimize 与 /v1/run 共用同一条后处理路径，避免两处参数漂移。"""

    def on_step(event: str, step: str) -> None:
        if request_id is None:
            return
        if event == "start":
            progress.phase(request_id, step)
        else:
            progress.step_done(request_id, step)

    return await optimize(
        raw_output,
        schema=schema_name,
        instruction=instruction,
        input_text=input_text,
        provider=app.state.provider,
        settings=app.state.settings,
        config=app.state.pipeline,
        repair=options.repair,
        simplify=options.simplify,
        fmt=options.format,
        score=options.score,
        strict=options.strict,
        timeout_ms=options.timeout_ms,
        audit_extra=audit_extra,
        reference_style=reference_style,
        request_id=request_id,
        on_step=on_step if request_id is not None else None,
    )


@app.post("/v1/optimize")
async def optimize_endpoint(payload: OptimizeRequest) -> dict[str, Any]:
    _check_schema(payload.schema_name)
    run_id = new_request_id()
    progress.begin(run_id, "optimize", "pipeline")
    try:
        result = await _optimize_output(
            raw_output=payload.raw_output,
            schema_name=payload.schema_name,
            instruction=payload.instruction,
            input_text=payload.input_text,
            options=payload.options,
            reference_style=payload.reference_style,
            request_id=run_id,
        )
    except Exception as exc:
        progress.finish(run_id, ok=False, detail=str(exc))
        raise
    progress.finish(run_id)
    return result.as_response()


def _resolve_harness(harness: Any, name: str | None) -> Any:
    """按 profile 名取执行体。未知名回 422，与未知 schema 的处理方式一致。"""
    profiles = getattr(harness, "profiles", None)
    if not callable(profiles):
        current = getattr(harness, "name", "")
        if name and name != current:
            raise HTTPException(status_code=422, detail=f"未知 harness={name!r}，当前可用：{current}")
        return harness
    members = profiles()
    if not name:
        return harness.select(None)
    if name not in members:
        raise HTTPException(status_code=422, detail=f"未知 harness={name!r}，可用：{', '.join(members)}")
    return members[name]


@app.post("/v1/run")
async def run_endpoint(payload: RunRequest) -> dict[str, Any]:
    """默认任务路径：驱动 harness 取回最终回答，再交给同一条 post-hoc 管线。

    时间预算是两个独立值：harness 用 ``HARNESS_TIMEOUT_MS``，优化管线用
    ``options.timeout_ms``；harness 失败或超时时**不进入**优化，直接 5xx。
    """
    harness = getattr(app.state, "harness", None)
    if harness is None:
        settings = getattr(app.state, "settings", None)
        cause = "HARNESS_ENABLED=false" if not getattr(settings, "harness_enabled", False) else "已开启但没有可驱动成员"
        raise HTTPException(
            status_code=503,
            detail=(
                f"没有可用的 harness（{cause}）；`GET /v1/harnesses` 查看检测与凭据状态。"
                "已有输出请直接调用 POST /v1/optimize"
            ),
        )
    _check_schema(payload.schema_name)
    target = _resolve_harness(harness, payload.harness)
    timeout_s = app.state.settings.harness_timeout_ms / 1000
    run_id = new_request_id()
    progress.begin(run_id, "run", "harness")
    try:
        try:
            run = await target.run(task=payload.task, timeout_s=timeout_s)
        except HarnessBusy as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except HarnessTimeout as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except HarnessUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except HarnessError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        progress.phase(run_id, "pipeline")
        result = await _optimize_output(
            raw_output=run.text,
            schema_name=payload.schema_name,
            instruction=payload.instruction,
            input_text=payload.input_text,
            options=payload.options,
            audit_extra={"task": payload.task, "harness": run.as_audit(target.name)},
            reference_style=payload.reference_style,
            request_id=run_id,
        )
    except Exception as exc:
        progress.finish(run_id, ok=False, detail=str(exc))
        raise
    progress.finish(run_id)
    return {**result.as_response(), "harness": {**run.as_audit(target.name), "chars": len(run.text)}}
