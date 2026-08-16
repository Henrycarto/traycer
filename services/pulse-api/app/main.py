"""Traycer Pulse API.

Alert classification, alert history and clinician push notification. This is
the service the clinician dashboard talks to.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .engine.threshold_config import ThresholdManager
from .routers import alerts, notify
from .schemas import ApiError, ApiMeta, ApiResponse, HealthStatus
from .store import PostgresAlertStore, PostgresThresholdStore, create_pool

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("traycer.pulse")

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = await create_pool()
    app.state.pool = pool

    if pool is not None:
        alerts.use_stores(
            PostgresAlertStore(pool),
            ThresholdManager(PostgresThresholdStore(pool)),
        )
        logger.info("alert history and thresholds backed by postgres")
    else:
        logger.warning(
            "running with in memory stores. Alerts do not survive a restart."
        )

    fcm = notify.get_fcm()
    if fcm.dry_run:
        logger.warning("FCM is in dry run mode, notifications are built but not sent")
    elif not fcm.configured:
        logger.error("FCM credentials are missing and dry run is off")

    try:
        yield
    finally:
        if pool is not None:
            await pool.close()


app = FastAPI(
    title="Traycer Pulse API",
    version="0.1.0",
    description="Alert classification and clinician notification for behavioral trajectories.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["authorization", "content-type"],
)

app.include_router(alerts.router)
app.include_router(notify.router)


def _meta() -> ApiMeta:
    return ApiMeta(request_id=str(uuid.uuid4()))


@app.get("/health", response_model=ApiResponse[HealthStatus])
async def health() -> ApiResponse[HealthStatus]:
    fcm = notify.get_fcm()
    classifier = alerts.get_classifier()

    return ApiResponse[HealthStatus](
        data=HealthStatus(
            status="ok",
            fcm_configured=fcm.configured,
            fcm_dry_run=fcm.dry_run,
            tracked_patients=classifier.tracked_patients,
        ),
        error=None,
        meta=_meta(),
    )


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
