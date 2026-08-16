"""Request and response models for the Pulse alert and notification API."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

SERVICE_NAME = "traycer-pulse-api"
SERVICE_VERSION = "0.1.0"


class ApiMeta(BaseModel):
    request_id: str
    service: str = SERVICE_NAME
    version: str = SERVICE_VERSION
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApiError(BaseModel):
    code: str
    message: str
    details: Optional[dict] = None


class ApiResponse(BaseModel, Generic[T]):
    data: Optional[T] = None
    error: Optional[ApiError] = None
    meta: ApiMeta


class Severity(str, Enum):
    STABLE = "stable"
    ELEVATED = "elevated"
    CRITICAL = "critical"


class SdkStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    INACTIVE = "inactive"
    ENROLLING = "enrolling"


class AlertThresholds(BaseModel):
    """Per patient alert configuration.

    Defaults are deliberately conservative. A clinician who is woken at 03:00
    by a false positive turns notifications off, and a system nobody looks at
    detects nothing.
    """

    model_config = ConfigDict(extra="forbid")

    patient_code: str
    elevated_score: float = Field(default=55.0, ge=1.0, le=99.0)
    critical_score: float = Field(default=75.0, ge=2.0, le=100.0)
    # Consecutive scoring windows above the threshold before an alert fires.
    # One window above threshold is usually a bad night, not a trajectory.
    min_consecutive: int = Field(default=2, ge=1, le=14)
    # Hours before the same patient can fire again at the same severity.
    cooldown_hours: int = Field(default=24, ge=1, le=336)
    # Signals a clinician has chosen to ignore for this patient, for example
    # mobility for a patient who is housebound for physical reasons.
    muted_signals: list[str] = Field(default_factory=list)
    # Suppress push between these local hours. The alert is still recorded and
    # still visible in the dashboard, it just does not ring.
    quiet_hours_start: Optional[int] = Field(default=None, ge=0, le=23)
    quiet_hours_end: Optional[int] = Field(default=None, ge=0, le=23)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ThresholdUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elevated_score: Optional[float] = Field(default=None, ge=1.0, le=99.0)
    critical_score: Optional[float] = Field(default=None, ge=2.0, le=100.0)
    min_consecutive: Optional[int] = Field(default=None, ge=1, le=14)
    cooldown_hours: Optional[int] = Field(default=None, ge=1, le=336)
    muted_signals: Optional[list[str]] = None
    quiet_hours_start: Optional[int] = Field(default=None, ge=0, le=23)
    quiet_hours_end: Optional[int] = Field(default=None, ge=0, le=23)


class ScoreObservation(BaseModel):
    """One scoring window handed to the classifier."""

    model_config = ConfigDict(extra="forbid")

    patient_code: str
    scored_at: datetime
    risk_score: float = Field(ge=0.0, le=100.0)
    baseline_quality: str
    dominant_signal: Optional[str] = None
    clinically_calibrated: bool = False


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_id: str
    patient_code: str
    fired_at: datetime
    severity: Severity
    risk_score: float
    trigger_signal: str
    # Plain language, one line, no jargon. This is what the clinician reads
    # first on a phone lock screen.
    summary: str
    consecutive_windows: int
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    notified: bool = False


class ClassificationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fired: bool
    alert: Optional[Alert] = None
    severity: Severity
    suppressed_reason: Optional[str] = None
    consecutive_windows: int


class AlertFeedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_id: str
    patient_code: str
    fired_at: datetime
    severity: Severity
    risk_score: float
    trigger_signal: str
    summary: str
    acknowledged: bool
    # Ranking key. Higher is more urgent. Combines severity, score and age so
    # a clinician scanning the feed sees the most actionable row first.
    rank: float


class ClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[ScoreObservation]


class AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acknowledged_by: str = Field(min_length=1, max_length=128)


class NotifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_id: str
    clinician_tokens: list[str] = Field(min_length=1)


class NotifyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_id: str
    delivered: int
    failed: int
    dry_run: bool
    invalid_tokens: list[str] = Field(default_factory=list)
    suppressed_reason: Optional[str] = None


class DeviceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fcm_token: str = Field(min_length=16, max_length=4096)
    platform: str = Field(pattern=r"^(ios|android|web)$")


class HealthStatus(BaseModel):
    status: str
    fcm_configured: bool
    fcm_dry_run: bool
    tracked_patients: int
