"""Traycer Voice service.

Acoustic feature extraction over short optional voice samples. The service is
stateless. It holds a sample for as long as one extraction takes and then
destroys it. Nothing about a sample is written to a database from here.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .engine.opensmile_client import OpenSmileClient
from .routers import analyze
from .schemas import ApiError, ApiMeta, ApiResponse, HealthStatus

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("traycer.voice")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    client = OpenSmileClient()
    app.state.opensmile = client
    app.state.opensmile_version = await client.version()

    if not client.is_available():
        # Starting without the binary is deliberate. The health endpoint
        # reports the gap and the orchestrator can hold traffic, which beats a
        # crash loop that hides the reason in a restart counter.
        logger.error("SMILExtract is not available, analysis requests will fail")
    else:
        logger.info("openSMILE ready: %s", app.state.opensmile_version or "version unknown")

    yield


app = FastAPI(
    title="Traycer Voice",
    version="0.1.0",
    description=(
        "Acoustic feature extraction for behavioral biomarkers. Operates on "
        "acoustic signal only. Never transcribes."
    ),
    lifespan=lifespan,
)

app.include_router(analyze.router)


def _meta() -> ApiMeta:
    return ApiMeta(request_id=str(uuid.uuid4()))


@app.get("/health", response_model=ApiResponse[HealthStatus])
async def health() -> ApiResponse[HealthStatus]:
    client: OpenSmileClient = app.state.opensmile
    available = client.is_available()

    return ApiResponse[HealthStatus](
        data=HealthStatus(
            status="ok" if available else "degraded",
            opensmile_available=available,
            opensmile_version=getattr(app.state, "opensmile_version", None),
            config=client.config_name,
        ),
        error=None,
        meta=_meta(),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Keep the envelope shape on error paths too.

    A client that has to branch between two response shapes will get the
    branch wrong eventually, so failures carry the same three keys.
    """
    body = ApiResponse[HealthStatus](
        data=None,
        error=ApiError(code=_code_for(exc.status_code), message=str(exc.detail)),
        meta=_meta(),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(mode="json"),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    body = ApiResponse[HealthStatus](
        data=None,
        error=ApiError(
            code="invalid_request",
            message="request failed validation",
            details={"errors": exc.errors()},
        ),
        meta=_meta(),
    )
    return JSONResponse(status_code=422, content=body.model_dump(mode="json"))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # The message is deliberately generic. An unhandled exception in this
    # service may carry a filesystem path to a patient sample, and that path
    # is not something to hand back over the wire.
    logger.exception("unhandled error on %s", request.url.path)
    body = ApiResponse[HealthStatus](
        data=None,
        error=ApiError(code="internal_error", message="internal error"),
        meta=_meta(),
    )
    return JSONResponse(status_code=500, content=body.model_dump(mode="json"))


def _code_for(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        413: "payload_too_large",
        415: "unsupported_media_type",
        422: "invalid_request",
        429: "rate_limited",
        503: "service_unavailable",
    }.get(status_code, "error")
