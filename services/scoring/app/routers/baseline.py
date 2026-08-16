"""POST /v1/baseline.

Builds a personal baseline from a patient's first window of SDK data. Called
by the ingest pipeline once a patient crosses the coverage threshold, and
again whenever a clinician requests a rebuild after a documented life change
such as a move or a new job.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Response, status

from ..engine.baseline_builder import build_baseline
from ..schemas import (
    ApiError,
    ApiMeta,
    ApiResponse,
    BaselineQuality,
    BaselineRequest,
    PatientBaseline,
)
from ..security import ServicePrincipal, current_service

logger = logging.getLogger("traycer.scoring.baseline_router")

router = APIRouter(prefix="/v1", tags=["baseline"])


def _meta() -> ApiMeta:
    return ApiMeta(request_id=str(uuid.uuid4()))


@router.post("/baseline", response_model=ApiResponse[PatientBaseline])
async def create_baseline(
    request: BaselineRequest,
    response: Response,
    principal: ServicePrincipal = Depends(current_service),
) -> ApiResponse[PatientBaseline]:
    principal.require_scope("scoring:write")

    if not request.samples:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return ApiResponse[PatientBaseline](
            data=None,
            error=ApiError(
                code="no_samples",
                message="a baseline cannot be built from an empty sample set",
            ),
            meta=_meta(),
        )

    baseline = build_baseline(
        patient_code=request.patient_code,
        samples=request.samples,
        window_days=request.window_days,
    )

    logger.info(
        "baseline built",
        extra={
            "quality": baseline.quality.value,
            "features": len(baseline.features),
            "days": baseline.sample_days,
        },
    )

    # An insufficient baseline is a valid and expected outcome during the cold
    # start window, so it returns 200 with the quality stated rather than an
    # error the caller has to special case.
    if baseline.quality is BaselineQuality.INSUFFICIENT:
        response.status_code = status.HTTP_200_OK

    return ApiResponse[PatientBaseline](data=baseline, error=None, meta=_meta())
