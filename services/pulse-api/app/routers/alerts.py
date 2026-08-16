"""Alert classification, feed and acknowledgement."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Response, status

from ..engine.alert_classifier import AlertClassifier, panel_summary, rank_alerts
from ..engine.threshold_config import (
    InMemoryThresholdStore,
    ThresholdManager,
    ThresholdValidationError,
)
from ..schemas import (
    AcknowledgeRequest,
    Alert,
    AlertFeedItem,
    AlertThresholds,
    ApiError,
    ApiMeta,
    ApiResponse,
    ClassificationOutcome,
    ClassifyRequest,
    ThresholdUpdate,
)
from ..security import Principal, current_principal
from ..store import AlertStore, InMemoryAlertStore

logger = logging.getLogger("traycer.pulse.alerts")

router = APIRouter(prefix="/v1", tags=["alerts"])

# Swapped for the Postgres implementations in the lifespan handler when a DSN
# is configured. Held at module scope so the classifier keeps its rolling
# per patient state across requests within a worker.
_classifier = AlertClassifier()
_alert_store: AlertStore = InMemoryAlertStore()
_threshold_manager = ThresholdManager(InMemoryThresholdStore())


def get_classifier() -> AlertClassifier:
    return _classifier


def get_alert_store() -> AlertStore:
    return _alert_store


def get_threshold_manager() -> ThresholdManager:
    return _threshold_manager


def use_stores(alert_store: AlertStore, threshold_manager: ThresholdManager) -> None:
    """Called from the lifespan handler once the database pool is open."""
    global _alert_store, _threshold_manager
    _alert_store = alert_store
    _threshold_manager = threshold_manager


def _meta() -> ApiMeta:
    return ApiMeta(request_id=str(uuid.uuid4()))


@router.post("/alerts/classify", response_model=ApiResponse[list[ClassificationOutcome]])
async def classify(
    request: ClassifyRequest,
    principal: Principal = Depends(current_principal),
    classifier: AlertClassifier = Depends(get_classifier),
    store: AlertStore = Depends(get_alert_store),
    thresholds: ThresholdManager = Depends(get_threshold_manager),
) -> ApiResponse[list[ClassificationOutcome]]:
    """Run scoring output through the alert policy.

    Called by the scoring pipeline after each batch of windows is scored.
    Observations are processed in timestamp order because the confirmation
    counter depends on sequence, and a caller batching a backfill will not
    always send them sorted.
    """
    principal.require_scope("pulse:write", "system/Observation.write")

    outcomes: list[ClassificationOutcome] = []
    ordered = sorted(request.observations, key=lambda o: o.scored_at)

    for observation in ordered:
        patient_thresholds = await thresholds.get(observation.patient_code)
        outcome = classifier.classify(observation, patient_thresholds)

        if outcome.fired and outcome.alert is not None:
            await store.add(outcome.alert)

        outcomes.append(outcome)

    fired = sum(1 for outcome in outcomes if outcome.fired)
    logger.info("classified %d observations, %d alerts fired", len(outcomes), fired)

    return ApiResponse[list[ClassificationOutcome]](data=outcomes, error=None, meta=_meta())


@router.get("/alerts", response_model=ApiResponse[list[AlertFeedItem]])
async def feed(
    limit: int = Query(default=100, ge=1, le=500),
    patient_code: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
    store: AlertStore = Depends(get_alert_store),
) -> ApiResponse[list[AlertFeedItem]]:
    """The alert feed, ranked by what to look at next rather than by time."""
    principal.require_scope("pulse:read", "user/Observation.read", "patient/Observation.read")

    alerts = await store.recent(limit=limit, patient_code=patient_code)
    items = rank_alerts(alerts)

    meta = _meta()
    return ApiResponse[list[AlertFeedItem]](
        data=items,
        error=None,
        meta=meta.model_copy(update={}),
    )


@router.get("/alerts/summary", response_model=ApiResponse[dict])
async def summary(
    principal: Principal = Depends(current_principal),
    store: AlertStore = Depends(get_alert_store),
) -> ApiResponse[dict]:
    principal.require_scope("pulse:read", "user/Observation.read", "patient/Observation.read")

    alerts = await store.recent(limit=500)
    return ApiResponse[dict](data=panel_summary(alerts), error=None, meta=_meta())


@router.post("/alerts/{alert_id}/acknowledge", response_model=ApiResponse[Alert])
async def acknowledge(
    alert_id: str,
    request: AcknowledgeRequest,
    response: Response,
    principal: Principal = Depends(current_principal),
    store: AlertStore = Depends(get_alert_store),
) -> ApiResponse[Alert]:
    principal.require_scope("pulse:write", "user/Observation.write")

    updated = await store.acknowledge(alert_id, request.acknowledged_by, datetime.now(timezone.utc))
    if updated is None:
        response.status_code = status.HTTP_404_NOT_FOUND
        return ApiResponse[Alert](
            data=None,
            error=ApiError(code="alert_not_found", message="no alert with that id"),
            meta=_meta(),
        )

    return ApiResponse[Alert](data=updated, error=None, meta=_meta())


@router.get("/patients/{patient_code}/thresholds", response_model=ApiResponse[AlertThresholds])
async def read_thresholds(
    patient_code: str,
    principal: Principal = Depends(current_principal),
    thresholds: ThresholdManager = Depends(get_threshold_manager),
) -> ApiResponse[AlertThresholds]:
    principal.require_scope("pulse:read", "user/Observation.read")

    current = await thresholds.get(patient_code)
    return ApiResponse[AlertThresholds](data=current, error=None, meta=_meta())


@router.patch("/patients/{patient_code}/thresholds", response_model=ApiResponse[AlertThresholds])
async def update_thresholds(
    patient_code: str,
    update: ThresholdUpdate,
    response: Response,
    principal: Principal = Depends(current_principal),
    thresholds: ThresholdManager = Depends(get_threshold_manager),
    classifier: AlertClassifier = Depends(get_classifier),
) -> ApiResponse[AlertThresholds]:
    principal.require_scope("pulse:write", "user/Observation.write")

    try:
        updated = await thresholds.update(patient_code, update)
    except ThresholdValidationError as exc:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return ApiResponse[AlertThresholds](
            data=None,
            error=ApiError(code="invalid_thresholds", message=str(exc)),
            meta=_meta(),
        )

    # The confirmation counter was accumulated against the old threshold, so
    # it no longer means what it did. Clearing it costs at most one window of
    # delay and avoids firing on a run that was never above the new bar.
    classifier.reset(patient_code)

    return ApiResponse[AlertThresholds](data=updated, error=None, meta=_meta())
