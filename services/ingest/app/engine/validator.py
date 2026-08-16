"""Server side validation of a decrypted payload.

The SDK already runs a data minimizer before anything is encrypted. This is
the same check from the other side of the wire, and it exists because the SDK
is open source and installable by anyone. A client that has been modified, or
simply an old version with a bug, must not be able to write values the scoring
engine will later treat as real.

The posture is different from the SDK's. The minimizer throws, because a
failure there means a bug in code we control and it should be loud. Here a
single bad sample is dropped and counted while the rest of the batch is kept,
because refusing an entire day of a patient's data over one out of range
number is the wrong trade for a monitoring system.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..schemas import BehavioralFeatureName as F
from ..schemas import BehavioralWindow, UploadPayload

# Upper bounds per feature, matching FEATURE_CEILINGS in the SDK's
# data_minimizer.ts. A value above these is a sensor fault or a tampered
# client, never a real reading.
FEATURE_CEILINGS: dict[F, float] = {
    F.ACTIVITY_RATIO: 1.0,
    F.MOVEMENT_INTENSITY: 16.0,
    F.LONGEST_SEDENTARY_MINUTES: 1440.0,
    F.ACTIVITY_FRAGMENTATION: 600.0,
    F.MOBILITY_RADIUS_M: 500000.0,
    F.LOCATION_ENTROPY: 8.0,
    F.PLACE_COUNT: 64.0,
    F.PRIMARY_PLACE_RATIO: 1.0,
    F.SCREEN_ON_MINUTES: 1440.0,
    F.UNLOCK_COUNT: 2000.0,
    F.MEAN_SESSION_MINUTES: 1440.0,
    F.USAGE_LOOP_INDEX: 100.0,
    F.NIGHT_USAGE_MINUTES: 1440.0,
    F.COMMUNICATION_SESSION_COUNT: 500.0,
    F.COMMUNICATION_MINUTES: 1440.0,
}

# A window shorter than a minute or longer than a day is not something the
# collector produces.
MIN_WINDOW_MS = 60_000
MAX_WINDOW_MS = 24 * 60 * 60 * 1000

# Clock skew allowance for a device timestamp in the future. Phones drift, and
# a device an hour ahead is common enough that rejecting it would lose real
# data. Beyond this the timestamp is not trustworthy.
MAX_FUTURE_SKEW_MS = 6 * 60 * 60 * 1000

# Windows older than this are past the point of clinical usefulness and are
# usually a device that was offline for months.
MAX_BACKFILL_MS = 90 * 24 * 60 * 60 * 1000


@dataclass
class ValidatedSample:
    patient_code: str
    observed_at_ms: int
    feature: str
    value: float
    confidence: float
    sdk_version: str


@dataclass
class ValidationOutcome:
    samples: list[ValidatedSample] = field(default_factory=list)
    accepted_windows: int = 0
    rejected_samples: int = 0
    notes: list[str] = field(default_factory=list)


def validate_payload(payload: UploadPayload, now_ms: int) -> ValidationOutcome:
    """Reduce a decrypted payload to the rows that may be stored."""
    outcome = ValidationOutcome()
    seen_windows: set[int] = set()

    for window in payload.windows:
        problem = _window_problem(window, now_ms)
        if problem is not None:
            outcome.rejected_samples += len(window.samples)
            outcome.notes.append(problem)
            continue

        if window.windowStart in seen_windows:
            # A batch carrying the same window twice means the device queue
            # double appended. Keeping the first is consistent with the
            # minimizer, which keeps the first of a duplicated feature.
            outcome.rejected_samples += len(window.samples)
            outcome.notes.append(f"duplicate window at {window.windowStart}")
            continue

        seen_windows.add(window.windowStart)
        accepted_here = _collect_samples(payload, window, outcome)

        if accepted_here:
            outcome.accepted_windows += 1

    return outcome


def _window_problem(window: BehavioralWindow, now_ms: int) -> str | None:
    if window.windowMs < MIN_WINDOW_MS or window.windowMs > MAX_WINDOW_MS:
        return f"window length {window.windowMs}ms is out of range"

    if window.windowStart > now_ms + MAX_FUTURE_SKEW_MS:
        return f"window at {window.windowStart} is too far in the future"

    if window.windowStart < now_ms - MAX_BACKFILL_MS:
        return f"window at {window.windowStart} is older than the backfill limit"

    if not window.samples:
        return f"window at {window.windowStart} carried no samples"

    return None


def _collect_samples(
    payload: UploadPayload, window: BehavioralWindow, outcome: ValidationOutcome
) -> bool:
    seen_features: set[str] = set()
    accepted = False

    for sample in window.samples:
        feature = sample.feature

        if feature.value in seen_features:
            outcome.rejected_samples += 1
            continue
        seen_features.add(feature.value)

        if not math.isfinite(sample.value) or not math.isfinite(sample.confidence):
            outcome.rejected_samples += 1
            outcome.notes.append(f"{feature.value} was not finite")
            continue

        ceiling = FEATURE_CEILINGS[feature]
        if sample.value < 0 or sample.value > ceiling:
            outcome.rejected_samples += 1
            outcome.notes.append(
                f"{feature.value} value {sample.value} outside 0 to {ceiling}"
            )
            continue

        outcome.samples.append(
            ValidatedSample(
                patient_code=payload.patientCode,
                observed_at_ms=window.windowStart,
                feature=feature.value,
                value=sample.value,
                confidence=sample.confidence,
                sdk_version=payload.sdkVersion,
            )
        )
        accepted = True

    return accepted
