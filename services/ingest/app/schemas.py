"""Wire models for the ingest endpoint.

These mirror `packages/sdk/src/types/index.ts` exactly. The SDK is the client
half of this contract and it is already published, so this file follows the
SDK rather than the other way round. A field renamed here without a matching
SDK release breaks every device in the field.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

SERVICE_NAME = "traycer-ingest"
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
    """The closed vocabulary, third definition.

    It exists in the SDK as a TypeScript union, in the scoring service as an
    enum, and here. All three are compared by the contract test in
    services/scoring/tests/test_feature_parity.py, which reads the TypeScript
    source rather than a copy of it.
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


class EncryptedEnvelope(BaseModel):
    """What the device actually posts.

    Only the batch id travels in the clear, and it does so because the server
    has to deduplicate a retried upload before it can decrypt anything. It is
    authenticated as additional data, so it cannot be swapped without the
    decryption failing.
    """

    model_config = ConfigDict(extra="forbid")

    v: Literal[1]
    batchId: str = Field(min_length=8, max_length=64)
    alg: Literal["A256GCM"]
    keyAlg: Literal["RSA-OAEP-256"]
    wrappedKey: str = Field(min_length=1, max_length=2048)
    iv: str = Field(min_length=1, max_length=64)
    ciphertext: str = Field(min_length=1)
    keyId: str = Field(min_length=1, max_length=128)


class FeatureSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature: BehavioralFeatureName
    value: float
    confidence: float = Field(ge=0.0, le=1.0)


class BehavioralWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    windowStart: int = Field(ge=0)
    windowMs: int = Field(gt=0)
    utcOffsetMinutes: int = Field(ge=-840, le=840)
    samples: list[FeatureSample]


class UploadPayload(BaseModel):
    """The decrypted plaintext, built by the SDK's data minimizer."""

    model_config = ConfigDict(extra="forbid")

    v: Literal[1]
    patientCode: str = Field(min_length=6, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    sdkVersion: str = Field(min_length=1, max_length=32)
    batchId: str = Field(min_length=8, max_length=64)
    createdAt: int = Field(ge=0)
    windows: list[BehavioralWindow]


class IngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    accepted_windows: int
    accepted_samples: int
    rejected_samples: int
    duplicate: bool = False
    notes: list[str] = Field(default_factory=list)


class HealthStatus(BaseModel):
    status: str
    key_ids: list[str]
    relational_connected: bool
    timeseries_connected: bool
