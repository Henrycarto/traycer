"""Request and response models for the behavioral scoring service."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

SERVICE_NAME = "traycer-scoring"
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


class BehavioralFeatureName(str, Enum):
    """Mirrors the closed vocabulary in @traycer/sdk.

    Kept in sync by the contract test in tests/test_feature_parity.py, which
    reads the TypeScript source rather than a copy of it. A feature added on
    one side and forgotten on the other fails CI.
    """

    ACTIVITY_RATIO = "activity_ratio"
    MOVEMENT_INTENSITY = "movement_intensity"
    LONGEST_SEDENTARY_MINUTES = "longest_sedentary_minutes"
    ACTIVITY_FRAGMENTATION = "activity_fragmentation"
    MOBILITY_RADIUS_M = "mobility_radius_m"
    LOCATION_ENTROPY = "location_entropy"
    PLACE_COUNT = "place_count"
    PRIMARY_PLACE_RATIO = "primary_place_ratio"
    SCREEN_ON_MINUTES = "screen_on_minutes"
    UNLOCK_COUNT = "unlock_count"
    MEAN_SESSION_MINUTES = "mean_session_minutes"
    USAGE_LOOP_INDEX = "usage_loop_index"
    NIGHT_USAGE_MINUTES = "night_usage_minutes"
    COMMUNICATION_SESSION_COUNT = "communication_session_count"
    COMMUNICATION_MINUTES = "communication_minutes"


class BaselineQuality(str, Enum):
    INSUFFICIENT = "insufficient"
    PROVISIONAL = "provisional"
    ESTABLISHED = "established"


class Severity(str, Enum):
    STABLE = "stable"
    ELEVATED = "elevated"
    CRITICAL = "critical"


class BehavioralSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    feature: BehavioralFeatureName
    value: float
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class FeatureBaseline(BaseModel):
    """Outlier resistant location and scale for one feature, plus its daily shape."""

    model_config = ConfigDict(extra="forbid")

    feature: BehavioralFeatureName
    median: float
    mad: float
    iqr: float
    p05: float
    p95: float
    observation_count: int
    effective_days: float
    # 24 hourly medians in the patient's local time. Circadian shape carries
    # signal that a whole day aggregate flattens away.
    hourly_median: list[float] = Field(default_factory=list)
    weekend_median: Optional[float] = None
    weekday_median: Optional[float] = None


class PatientBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_code: str
    built_at: datetime
    window_start: datetime
    window_end: datetime
    sample_days: int
    quality: BaselineQuality
    features: list[FeatureBaseline]
    notes: list[str] = Field(default_factory=list)


class FeatureDeviation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature: BehavioralFeatureName
    observed: float
    baseline_median: float
    # Deviation in units of the baseline scale. Sign is preserved because
    # mobility radius collapse and a mobility radius spike are different
    # clinical pictures.
    deviation_z: float
    # Deviation after the direction weighting is applied, always positive.
    weighted_deviation: float
    confidence: float


class TrajectoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: datetime
    risk_score: float = Field(ge=0.0, le=100.0)
    baseline_score: float
    deviation: float


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_code: str = Field(min_length=6, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    window_start: datetime
    window_end: datetime
    samples: list[BehavioralSample]
    baseline: Optional[PatientBaseline] = None


class ScoreResult(BaseModel):
    # protected_namespaces is cleared because model_version is a field name
    # the API contract needs, and pydantic reserves the model_ prefix.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    patient_code: str
    scored_at: datetime
    risk_score: float = Field(ge=0.0, le=100.0)
    severity: Severity
    baseline_quality: BaselineQuality
    dominant_signal: Optional[BehavioralFeatureName] = None
    deviations: list[FeatureDeviation]
    coefficient_set: str
    model_version: str
    # Populated only when the private trajectory model is installed.
    predicted_risk_7d: Optional[float] = None
    prediction_available: bool = False
    notes: list[str] = Field(default_factory=list)


class BaselineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_code: str = Field(min_length=6, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    samples: list[BehavioralSample]
    window_days: int = Field(default=14, ge=3, le=60)


class HealthStatus(BaseModel):
    status: str
    coefficient_set: str
    predictor_available: bool
    predictor_version: Optional[str] = None
