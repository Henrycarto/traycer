"""Traycer behavioral scoring service.

Two responsibilities: build a personal baseline from a patient's own first
window of data, and score later windows against it. The trajectory model that
turns a score series into a forecast is a private package and is absent from
this repository. Its interface is in engine/lstm_predictor.py.
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

from .engine.lstm_predictor import describe_stub
from .routers import baseline, score
from .schemas import ApiError, ApiMeta, ApiResponse, HealthStatus

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("traycer.scoring")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    detector = score.get_detector()
    predictor = score.get_predictor()

    if not detector.clinically_calibrated:
        logger.warning(
            "running on placeholder coefficients. Scores are structurally valid and "
            "clinically meaningless."
        )
    if not predictor.available:
        logger.info("trajectory prediction disabled: %s", describe_stub()["reason"])

    yield


app = FastAPI(
    title="Traycer Scoring",
    version="0.1.0",
    description=(
        "Personal baseline construction and deviation scoring for passive "
        "behavioral data."
    ),
    lifespan=lifespan,
)

app.include_router(baseline.router)
app.include_router(score.router)


def _meta() -> ApiMeta:
    return ApiMeta(request_id=str(uuid.uuid4()))


@app.get("/health", response_model=ApiResponse[HealthStatus])
async def health() -> ApiResponse[HealthStatus]:
    detector = score.get_detector()
    predictor = score.get_predictor()

    return ApiResponse[HealthStatus](
        data=HealthStatus(
            status="ok",
            coefficient_set=detector.coefficient_name,
            predictor_available=predictor.available,
            predictor_version=predictor.version,
        ),
        error=None,
        meta=_meta(),
    )


@app.get("/v1/model-status")
async def model_status() -> ApiResponse[dict]:
    """Explains exactly what is stubbed and why.

    Exists so an operator can distinguish a model that failed to load from a
    model that was never installed, without reading the logs.
    """
    detector = score.get_detector()
    payload = dict(describe_stub())
    payload["coefficient_set"] = detector.coefficient_name
    payload["clinically_calibrated"] = detector.clinically_calibrated

    return ApiResponse[dict](data=payload, error=None, meta=_meta())


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    body = ApiResponse[dict](
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
    body = ApiResponse[dict](
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
    logger.exception("unhandled error on %s", request.url.path)
    body = ApiResponse[dict](
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
        409: "conflict",
        422: "invalid_request",
        429: "rate_limited",
        503: "service_unavailable",
    }.get(status_code, "error")
