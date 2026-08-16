"""Traycer ingest service.

Terminates the SDK upload contract. Verifies the device token, deduplicates on
batch id, decrypts the envelope, validates the payload against the closed
feature vocabulary, and writes the samples to the Timescale hypertable.

This is the only service that ever holds a decrypted behavioral payload in
memory, and it holds it for the length of one request.
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

from .crypto import KeyRing
from .routers import batches
from .schemas import ApiError, ApiMeta, ApiResponse, HealthStatus
from .store import InMemoryIngestStore, PostgresIngestStore, create_pools

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("traycer.ingest")

# A batch of 24 windows across 15 features serialises to roughly 40 kB, and
# the envelope adds base64 overhead. A megabyte is generous and still bounds
# what a single request can ask this process to buffer.
MAX_BODY_BYTES = int(os.getenv("INGEST_MAX_BODY_BYTES", str(1024 * 1024)))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    keyring = KeyRing.from_environment()
    if len(keyring) == 0:
        logger.error(
            "no private keys loaded, every batch will be refused with unknown_key"
        )

    relational, timeseries = await create_pools()
    app.state.relational = relational
    app.state.timeseries = timeseries

    if relational is not None and timeseries is not None:
        store = PostgresIngestStore(relational, timeseries)
        logger.info("ingest backed by postgres and timescale")
    else:
        store = InMemoryIngestStore()
        logger.warning("ingest using the in memory store, nothing survives a restart")

    app.state.keyring = keyring
    app.state.store = store
    batches.configure(keyring, store)

    try:
        yield
    finally:
        if relational is not None:
            await relational.close()
        if timeseries is not None:
            await timeseries.close()


app = FastAPI(
    title="Traycer Ingest",
    version="0.1.0",
    description="Terminates the @traycer/sdk encrypted upload contract.",
    lifespan=lifespan,
)

app.include_router(batches.router)


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Reject oversized uploads on the declared length before reading them.

    A device sending a megabyte batch is already an anomaly. Checking the
    header first means a hostile content length never decides how much this
    process buffers.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content=ApiResponse[HealthStatus](
                        data=None,
                        error=ApiError(
                            code="payload_too_large",
                            message=f"body exceeds {MAX_BODY_BYTES} bytes",
                        ),
                        meta=ApiMeta(request_id=str(uuid.uuid4())),
                    ).model_dump(mode="json"),
                )
        except ValueError:
            pass

    return await call_next(request)


def _meta() -> ApiMeta:
    return ApiMeta(request_id=str(uuid.uuid4()))


@app.get("/health", response_model=ApiResponse[HealthStatus])
async def health() -> ApiResponse[HealthStatus]:
    keyring: KeyRing = app.state.keyring

    return ApiResponse[HealthStatus](
        data=HealthStatus(
            status="ok" if len(keyring) > 0 else "degraded",
            key_ids=keyring.key_ids,
            relational_connected=app.state.relational is not None,
            timeseries_connected=app.state.timeseries is not None,
        ),
        error=None,
        meta=_meta(),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
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
    # 422 tells the device to drop the batch, which is right: a payload that
    # fails schema validation will fail identically on every retry.
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
    # 500 keeps the batch on the device. An unhandled error here is by
    # definition something we do not understand, and losing patient data over
    # it is the worse outcome.
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
        409: "conflict",
        413: "payload_too_large",
        422: "invalid_request",
        429: "rate_limited",
        503: "service_unavailable",
    }.get(status_code, "error")
