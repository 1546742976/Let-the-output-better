"""FastAPI 入口：``POST /v1/optimize`` 与 ``GET /healthz``。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.optimizer import optimize
from app.providers import build_provider
from app.schemas import SCHEMA_REGISTRY
from app.settings import get_pipeline_config, get_settings


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.pipeline = get_pipeline_config()
    # 启动期即构造 provider：配置缺失或密钥为空在这里失败，不留到运行期。
    app.state.provider = build_provider(settings)
    try:
        yield
    finally:
        await app.state.provider.aclose()


app = FastAPI(title="Let-the-output-better", version="0.2.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "provider": app.state.settings.provider}


@app.post("/v1/optimize")
async def optimize_endpoint(payload: OptimizeRequest) -> dict[str, Any]:
    if payload.schema_name is not None and payload.schema_name.lower() not in SCHEMA_REGISTRY:
        known = ", ".join(SCHEMA_REGISTRY)
        raise HTTPException(status_code=422, detail=f"未知 schema={payload.schema_name!r}，内置可选：{known}")
    options = payload.options
    result = await optimize(
        payload.raw_output,
        schema=payload.schema_name,
        instruction=payload.instruction,
        input_text=payload.input_text,
        provider=app.state.provider,
        settings=app.state.settings,
        config=app.state.pipeline,
        repair=options.repair,
        humanize=options.humanize,
        fmt=options.format,
        score=options.score,
        strict=options.strict,
        timeout_ms=options.timeout_ms,
    )
    return result.as_response()
