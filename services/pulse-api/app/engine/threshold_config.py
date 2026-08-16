"""Per patient alert threshold management.

One global threshold does not survive contact with a real panel. A patient six
weeks out of an inpatient stay needs a hair trigger. A patient stable on
clozapine for four years needs a threshold that does not fire every time they
visit family. The clinician who knows the difference is the one who should set
it, so thresholds are per patient and clinician editable.

Storage is behind a protocol. The deployed service uses Postgres through the
repository in `store.py`; tests and local runs use the in memory
implementation here. The classifier depends on the protocol, not on either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from ..schemas import AlertThresholds, ThresholdUpdate

logger = logging.getLogger("traycer.pulse.thresholds")

# Applied to a patient with no explicit configuration. These match the column
# defaults in the relational schema, and the two are checked against each
# other by a test so they cannot drift.
DEFAULT_ELEVATED = 55.0
DEFAULT_CRITICAL = 75.0
DEFAULT_MIN_CONSECUTIVE = 2
DEFAULT_COOLDOWN_HOURS = 24


class ThresholdValidationError(ValueError):
    """A requested configuration would produce an unusable alert policy."""


class ThresholdStore(Protocol):
    async def get(self, patient_code: str) -> AlertThresholds | None: ...

    async def put(self, thresholds: AlertThresholds) -> None: ...

    async def all_patients(self) -> list[str]: ...


@dataclass
class InMemoryThresholdStore:
    """Process local store. Used by tests and by local development."""

    _rows: dict[str, AlertThresholds] | None = None

    def __post_init__(self) -> None:
        if self._rows is None:
            self._rows = {}

    async def get(self, patient_code: str) -> AlertThresholds | None:
        assert self._rows is not None
        return self._rows.get(patient_code)

    async def put(self, thresholds: AlertThresholds) -> None:
        assert self._rows is not None
        self._rows[thresholds.patient_code] = thresholds

    async def all_patients(self) -> list[str]:
        assert self._rows is not None
        return sorted(self._rows)


def default_thresholds(patient_code: str) -> AlertThresholds:
    return AlertThresholds(
        patient_code=patient_code,
        elevated_score=DEFAULT_ELEVATED,
        critical_score=DEFAULT_CRITICAL,
        min_consecutive=DEFAULT_MIN_CONSECUTIVE,
        cooldown_hours=DEFAULT_COOLDOWN_HOURS,
    )


def validate(thresholds: AlertThresholds) -> None:
    """Reject configurations that cannot do their job.

    Each rule here corresponds to a way a well meaning clinician can turn the
    alerting off without realising it.
    """
    if thresholds.critical_score <= thresholds.elevated_score:
        raise ThresholdValidationError(
            "the critical threshold must sit above the elevated threshold, otherwise "
            "no alert can ever be classified as elevated"
        )

    if thresholds.elevated_score >= 95.0:
        raise ThresholdValidationError(
            "an elevated threshold of 95 or higher will effectively never fire"
        )

    if thresholds.min_consecutive > 7:
        raise ThresholdValidationError(
            "requiring more than seven consecutive windows delays the alert past the "
            "point where early detection is useful"
        )

    quiet_start = thresholds.quiet_hours_start
    quiet_end = thresholds.quiet_hours_end
    if (quiet_start is None) != (quiet_end is None):
        raise ThresholdValidationError("quiet hours need both a start and an end")

    if quiet_start is not None and quiet_end is not None:
        span = (quiet_end - quiet_start) % 24
        if span == 0:
            raise ThresholdValidationError(
                "quiet hours covering the whole day would suppress every notification"
            )
        if span > 14:
            raise ThresholdValidationError(
                f"quiet hours span {span} hours, which suppresses most of the day"
            )


class ThresholdManager:
    def __init__(self, store: ThresholdStore) -> None:
        self._store = store

    async def get(self, patient_code: str) -> AlertThresholds:
        """Return the patient's configuration, or the defaults.

        Never returns None. A missing row means a patient nobody has tuned
        yet, and the correct behavior is to monitor them with the defaults
        rather than to skip them.
        """
        existing = await self._store.get(patient_code)
        return existing or default_thresholds(patient_code)

    async def update(self, patient_code: str, update: ThresholdUpdate) -> AlertThresholds:
        current = await self.get(patient_code)
        changes = update.model_dump(exclude_none=True)

        if not changes:
            return current

        proposed = current.model_copy(
            update={**changes, "updated_at": datetime.now(timezone.utc)}
        )
        validate(proposed)

        await self._store.put(proposed)
        logger.info("thresholds updated", extra={"changed": sorted(changes)})
        return proposed

    async def reset(self, patient_code: str) -> AlertThresholds:
        fresh = default_thresholds(patient_code)
        await self._store.put(fresh)
        return fresh

    async def is_quiet_hour(self, thresholds: AlertThresholds, at: datetime) -> bool:
        """Whether push should be held back at this local hour.

        Quiet hours suppress the notification, never the alert. The row still
        appears in the feed with its real timestamp, so a clinician opening the
        dashboard in the morning sees exactly what happened overnight.
        """
        start = thresholds.quiet_hours_start
        end = thresholds.quiet_hours_end
        if start is None or end is None:
            return False

        hour = at.hour
        if start <= end:
            return start <= hour < end
        # The window wraps past midnight, which is the common case.
        return hour >= start or hour < end
