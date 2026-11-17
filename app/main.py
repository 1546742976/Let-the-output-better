"""FastAPI 入口：``POST /v1/optimize``、``GET /healthz``、本地查看页与实验面 ``POST /v1/run``。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.harness import (
    HarnessBusy,
    HarnessError,
    HarnessTimeout,
    HarnessUnavailable,
    build_harness,
)
from app.optimizer import OptimizeResult, optimize
from app.providers import build_provider
from app.schemas import SCHEMA_REGISTRY
from app.settings import get_pipeline_config, get_settings
from app.viewer import mount as mount_viewer


class Options(BaseModel):
    """单次请求的步骤开关，语义与 ``pipeline.yaml`` 的 ``enabled`` 一致。"""

    model_config = ConfigDict(extra="ignore")

    score: bool | None = None
    repair: bool | None = None
    format: bool | None = None
    humanize: bool | None = None
    strict: bool | None = None
    timeout_ms: int | None = Field(default=None, ge=50, le=120000)


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    raw_output: str = Field(min_length=1)
    schema_name: str | None = Field(default=None, alias="schema")
    instruction: str = ""
    input_text: str = ""
    options: Options = Field(default_factory=Options)


class RunRequest(BaseModel):
    """实验面请求体：只有 ``task`` 会交给 harness，命令本身不可由请求指定。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    task: str = Field(min_length=1)
    schema_name: str | None = Field(default=None, alias="schema")
    instruction: str = ""
    input_text: str = ""
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
) -> OptimizeResult:
    """/v1/optimize 与 /v1/run 共用同一条后处理路径，避免两处参数漂移。"""
    return await optimize(
        raw_output,
        schema=schema_name,
        instruction=instruction,
        input_text=input_text,
        provider=app.state.provider,
        settings=app.state.settings,
        config=app.state.pipeline,
        repair=options.repair,
        humanize=options.humanize,
        fmt=options.format,
        score=options.score,
        strict=options.strict,
        timeout_ms=options.timeout_ms,
        audit_extra=audit_extra,
    )


@app.post("/v1/optimize")
async def optimize_endpoint(payload: OptimizeRequest) -> dict[str, Any]:
    _check_schema(payload.schema_name)
    result = await _optimize_output(
        raw_output=payload.raw_output,
        schema_name=payload.schema_name,
        instruction=payload.instruction,
        input_text=payload.input_text,
        options=payload.options,
    )
    return result.as_response()


@app.post("/v1/run")
async def run_endpoint(payload: RunRequest) -> dict[str, Any]:
    """实验面：驱动 harness 取回最终回答，再交给同一条 post-hoc 管线。默认关闭。

    时间预算是两个独立值：harness 用 ``HARNESS_TIMEOUT_MS``，优化管线用
    ``options.timeout_ms``；harness 失败或超时时**不进入**优化，直接 5xx。
    """
    harness = getattr(app.state, "harness", None)
    if harness is None:
        raise HTTPException(
            status_code=503,
            detail="harness 驱动未启用（HARNESS_ENABLED=false）。已有输出请直接调用 POST /v1/optimize",
        )
    _check_schema(payload.schema_name)
    timeout_s = app.state.settings.harness_timeout_ms / 1000
    try:
        run = await harness.run(task=payload.task, timeout_s=timeout_s)
    except HarnessBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except HarnessTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except HarnessUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HarnessError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    result = await _optimize_output(
        raw_output=run.text,
        schema_name=payload.schema_name,
        instruction=payload.instruction,
        input_text=payload.input_text,
        options=payload.options,
        audit_extra={"task": payload.task, "harness": run.as_audit(harness.name)},
    )
    return {**result.as_response(), "harness": {**run.as_audit(harness.name), "chars": len(run.text)}}
