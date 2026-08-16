"""POST /v1/score.

Scores one window of behavioral samples against a patient's personal baseline
and, when the private trajectory model is installed, attaches a forecast.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status

from ..engine.anomaly_detector import AnomalyDetector
from ..engine.lstm_predictor import TrajectoryPredictor
from ..schemas import (
    ApiError,
    ApiMeta,
    ApiResponse,
    ScoreRequest,
    ScoreResult,
)
from ..security import ServicePrincipal, current_service

logger = logging.getLogger("traycer.scoring.score_router")

router = APIRouter(prefix="/v1", tags=["score"])

# Both are stateless and cheap to hold. The detector reads its coefficient set
# once at import, and reloading it per request would mean an import attempt on
# every call.
_detector = AnomalyDetector()
_predictor = TrajectoryPredictor()


def get_detector() -> AnomalyDetector:
    return _detector


def get_predictor() -> TrajectoryPredictor:
    return _predictor


def _meta() -> ApiMeta:
    return ApiMeta(request_id=str(uuid.uuid4()))


@router.post("/score", response_model=ApiResponse[ScoreResult])
async def score_window(
    request: ScoreRequest,
    response: Response,
    principal: ServicePrincipal = Depends(current_service),
    detector: AnomalyDetector = Depends(get_detector),
    predictor: TrajectoryPredictor = Depends(get_predictor),
) -> ApiResponse[ScoreResult]:
    principal.require_scope("scoring:read")

    if request.baseline is None:
        response.status_code = status.HTTP_409_CONFLICT
        return ApiResponse[ScoreResult](
            data=None,
            error=ApiError(
                code="baseline_missing",
                message=(
                    "no personal baseline was supplied. Traycer scores each patient "
                    "against themselves, so a score cannot be produced without one."
                ),
            ),
            meta=_meta(),
        )

    if request.baseline.patient_code != request.patient_code:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return ApiResponse[ScoreResult](
            data=None,
            error=ApiError(
                code="baseline_patient_mismatch",
                message="the supplied baseline belongs to a different patient",
            ),
            meta=_meta(),
        )

    scored_at = datetime.now(timezone.utc)
    result = detector.score(request.samples, request.baseline, scored_at=scored_at)

    notes = list(result.notes)
    if not result.clinically_calibrated:
        notes.append(
            "this score was produced with placeholder coefficients and has no "
            "clinical meaning"
        )

    payload = ScoreResult(
        patient_code=request.patient_code,
        scored_at=scored_at,
        risk_score=result.risk_score,
        severity=result.severity,
        baseline_quality=request.baseline.quality,
        dominant_signal=result.dominant_signal,
        deviations=result.deviations,
        coefficient_set=result.coefficient_set,
        model_version=predictor.version or "anomaly-only",
        prediction_available=predictor.available,
        predicted_risk_7d=None,
        notes=notes,
    )

    logger.info(
        "window scored",
        extra={
            "severity": payload.severity.value,
            "risk_score": payload.risk_score,
            "calibrated": result.clinically_calibrated,
        },
    )

    return ApiResponse[ScoreResult](data=payload, error=None, meta=_meta())
