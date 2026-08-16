"""Clinician push notification delivery and device registration."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status

from ..engine.fcm_client import FcmClient, FcmConfigurationError
from ..engine.threshold_config import ThresholdManager
from ..schemas import (
    ApiError,
    ApiMeta,
    ApiResponse,
    DeviceRegistration,
    NotifyRequest,
    NotifyResult,
)
from ..security import Principal, current_principal
from ..store import (
    AlertStore,
    ClinicianDeviceStore,
    InMemoryClinicianDeviceStore,
)
from .alerts import get_alert_store, get_threshold_manager

logger = logging.getLogger("traycer.pulse.notify")

router = APIRouter(prefix="/v1", tags=["notify"])

_fcm = FcmClient()
_devices: ClinicianDeviceStore = InMemoryClinicianDeviceStore()


def get_fcm() -> FcmClient:
    return _fcm


def get_devices() -> ClinicianDeviceStore:
    return _devices


def _meta() -> ApiMeta:
    return ApiMeta(request_id=str(uuid.uuid4()))


@router.post("/notify", response_model=ApiResponse[NotifyResult])
async def notify(
    request: NotifyRequest,
    response: Response,
    principal: Principal = Depends(current_principal),
    fcm: FcmClient = Depends(get_fcm),
    alerts: AlertStore = Depends(get_alert_store),
    devices: ClinicianDeviceStore = Depends(get_devices),
    thresholds: ThresholdManager = Depends(get_threshold_manager),
) -> ApiResponse[NotifyResult]:
    principal.require_scope("pulse:notify", "pulse:write")

    alert = await alerts.get(request.alert_id)
    if alert is None:
        response.status_code = status.HTTP_404_NOT_FOUND
        return ApiResponse[NotifyResult](
            data=None,
            error=ApiError(code="alert_not_found", message="no alert with that id"),
            meta=_meta(),
        )

    patient_thresholds = await thresholds.get(alert.patient_code)
    now = datetime.now(timezone.utc)

    if await thresholds.is_quiet_hour(patient_thresholds, now):
        # The alert stays in the feed with its real timestamp. Only the push
        # is held, which is the difference between not waking someone and
        # hiding what happened.
        logger.info("notification held by quiet hours")
        return ApiResponse[NotifyResult](
            data=NotifyResult(
                alert_id=alert.alert_id,
                delivered=0,
                failed=0,
                dry_run=fcm.dry_run,
                suppressed_reason="within the configured quiet hours",
            ),
            error=None,
            meta=_meta(),
        )

    try:
        result = await fcm.send_alert(alert, request.clinician_tokens)
    except FcmConfigurationError as exc:
        logger.error("FCM is misconfigured: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ApiResponse[NotifyResult](
            data=None,
            error=ApiError(
                code="fcm_unavailable", message="push notifications are not configured"
            ),
            meta=_meta(),
        )

    for token in result.invalid_tokens:
        # A token FCM calls unregistered belongs to an app that was
        # uninstalled. Keeping it guarantees a failure on every future alert.
        await devices.remove(token)

    if result.delivered > 0:
        await alerts.mark_notified(alert.alert_id)

    return ApiResponse[NotifyResult](data=result, error=None, meta=_meta())


@router.post("/devices", response_model=ApiResponse[dict])
async def register_device(
    registration: DeviceRegistration,
    principal: Principal = Depends(current_principal),
    devices: ClinicianDeviceStore = Depends(get_devices),
) -> ApiResponse[dict]:
    """Register the calling clinician's device for push."""
    principal.require_scope("pulse:read", "user/Observation.read")

    await devices.register(principal.subject, registration.fcm_token, registration.platform)
    logger.info("clinician device registered", extra={"platform": registration.platform})

    return ApiResponse[dict](
        data={"registered": True, "platform": registration.platform},
        error=None,
        meta=_meta(),
    )


@router.get("/devices", response_model=ApiResponse[list[str]])
async def list_devices(
    principal: Principal = Depends(current_principal),
    devices: ClinicianDeviceStore = Depends(get_devices),
) -> ApiResponse[list[str]]:
    principal.require_scope("pulse:read", "user/Observation.read")

    tokens = await devices.tokens_for(principal.subject)
    # Tokens are truncated in the response. A clinician needs to confirm a
    # device is registered, not to copy the token back out of the API.
    return ApiResponse[list[str]](
        data=[f"{token[:8]}..." for token in tokens], error=None, meta=_meta()
    )
