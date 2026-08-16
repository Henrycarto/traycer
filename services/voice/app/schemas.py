"""Request and response models for Traycer Voice.

The service accepts a short audio sample and returns an acoustic vector. It
never returns, logs, or stores anything derived from what was said. The models
in this file are the contract that makes that checkable: the response type has
no free text field anywhere in it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

SERVICE_NAME = "traycer-voice"
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
    """Every endpoint returns this shape, including failures."""

    data: Optional[T] = None
    error: Optional[ApiError] = None
    meta: ApiMeta


class VoiceFeatureName(str, Enum):
    """The complete acoustic vocabulary.

    Closed on purpose. A feature name that is not in this enum cannot be
    returned, which means a reviewer reads one list to know everything the
    service emits.
    """

    VOCAL_ENERGY = "vocal_energy"
    ENERGY_VARIABILITY = "energy_variability"
    PITCH_MEAN_SEMITONES = "pitch_mean_semitones"
    PITCH_VARIANCE = "pitch_variance"
    SPEECH_RATE = "speech_rate"
    ARTICULATION_RATE = "articulation_rate"
    PAUSE_FREQUENCY = "pause_frequency"
    MEAN_PAUSE_SECONDS = "mean_pause_seconds"
    VOICED_RATIO = "voiced_ratio"
    JITTER = "jitter"
    SHIMMER = "shimmer"
    HARMONICITY = "harmonicity"
    SPECTRAL_SLOPE = "spectral_slope"


class VoiceFeature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature: VoiceFeatureName
    value: float
    unit: str


class SampleQuality(str, Enum):
    USABLE = "usable"
    NOISY = "noisy"
    TOO_SHORT = "too_short"
    NO_SPEECH = "no_speech"


class AnalyzeRequest(BaseModel):
    """Multipart uploads carry the audio. This carries the metadata."""

    model_config = ConfigDict(extra="forbid")

    patient_code: str = Field(min_length=6, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    recorded_at: datetime
    prompt_id: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Identifier of the fixed elicitation prompt the patient read. The "
            "prompt text stays on the client. Sending an id rather than the "
            "spoken text keeps content out of this service entirely."
        ),
    )


class VoiceBiomarkerVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_code: str
    recorded_at: datetime
    prompt_id: str
    sample_seconds: float
    quality: SampleQuality
    extractor: str
    features: list[VoiceFeature]
    # Set when the guard rejected part of the extractor output. The vector is
    # still returned, minus whatever failed, so one bad field does not lose a
    # sample the patient took the trouble to record.
    guard_notes: list[str] = Field(default_factory=list)


class HealthStatus(BaseModel):
    status: str
    opensmile_available: bool
    opensmile_version: Optional[str] = None
    config: str
