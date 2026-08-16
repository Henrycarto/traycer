"""Decides which score changes become alerts, and how urgent they are.

The hard problem in a monitoring system is not detection. It is deciding what
deserves a clinician's attention. A psychiatrist with a panel of forty will
stop reading a feed that cries wolf twice a week, and a feed nobody reads has
negative value because it creates the impression of coverage.

Four filters stand between a raised score and a notification:

1. Confirmation. A score has to stay above threshold for a configured number
   of consecutive windows. One bad night is not a trajectory.
2. Cooldown. The same patient does not fire again at the same severity inside
   the cooldown window. An escalation from elevated to critical bypasses it,
   because that is new information.
3. Muted signals. A clinician who knows a patient is housebound can mute
   mobility for that patient without muting them entirely.
4. Calibration. A score produced with placeholder coefficients cannot fire a
   clinical alert at all. It is recorded, and it is visibly marked.

Ranking is separate from firing. Every open alert gets a rank so the feed
sorts by what to look at next rather than by what happened last.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque, Iterable, Sequence

from ..schemas import (
    Alert,
    AlertFeedItem,
    AlertThresholds,
    ClassificationOutcome,
    ScoreObservation,
    Severity,
)

logger = logging.getLogger("traycer.pulse.classifier")

# Windows kept per patient for the confirmation check. Enough to satisfy the
# largest permitted min_consecutive with room to see the run break.
CONFIRMATION_HISTORY = 16

# Rank weights. Severity dominates, score breaks ties inside a severity band,
# and age lifts an alert that has sat unacknowledged.
SEVERITY_RANK = {Severity.CRITICAL: 1000.0, Severity.ELEVATED: 500.0, Severity.STABLE: 0.0}
AGE_RANK_PER_HOUR = 2.0
MAX_AGE_RANK = 240.0

# Human readable phrasing per behavioral signal. Written for a clinician
# glancing at a lock screen, not for an engineer reading a log.
SIGNAL_PHRASING: dict[str, str] = {
    "mobility_radius_m": "movement radius well below their usual range",
    "location_entropy": "fewer places visited than usual",
    "place_count": "a narrower set of destinations than usual",
    "primary_place_ratio": "more time at home than their baseline",
    "activity_ratio": "a drop in daily physical activity",
    "movement_intensity": "lower movement intensity than usual",
    "longest_sedentary_minutes": "longer unbroken periods without movement",
    "activity_fragmentation": "more fragmented, restless movement than usual",
    "screen_on_minutes": "a change in daily phone use",
    "unlock_count": "many more phone checks than usual",
    "mean_session_minutes": "shorter, more frequent phone sessions",
    "usage_loop_index": "a repetitive phone checking pattern",
    "night_usage_minutes": "phone use during the night, which is new for them",
    "communication_session_count": "fewer conversations than their baseline",
    "communication_minutes": "less time in communication apps than usual",
}


@dataclass
class PatientState:
    """Rolling state the classifier needs between windows."""

    recent: Deque[ScoreObservation] = field(
        default_factory=lambda: deque(maxlen=CONFIRMATION_HISTORY)
    )
    last_fired_at: dict[Severity, datetime] = field(default_factory=dict)


class AlertClassifier:
    def __init__(self) -> None:
        self._state: dict[str, PatientState] = defaultdict(PatientState)

    @property
    def tracked_patients(self) -> int:
        return len(self._state)

    def classify(
        self,
        observation: ScoreObservation,
        thresholds: AlertThresholds,
        now: datetime | None = None,
    ) -> ClassificationOutcome:
        now = now or datetime.now(timezone.utc)
        state = self._state[observation.patient_code]
        state.recent.append(observation)

        severity = self.severity_for(observation.risk_score, thresholds)
        consecutive = self._consecutive_at_or_above(state, severity, thresholds)

        if severity is Severity.STABLE:
            return ClassificationOutcome(
                fired=False,
                severity=Severity.STABLE,
                suppressed_reason=None,
                consecutive_windows=consecutive,
            )

        if not observation.clinically_calibrated:
            # A demo score must never reach a clinician as a clinical finding.
            return ClassificationOutcome(
                fired=False,
                severity=severity,
                suppressed_reason="score was produced with uncalibrated coefficients",
                consecutive_windows=consecutive,
            )

        if observation.baseline_quality == "insufficient":
            return ClassificationOutcome(
                fired=False,
                severity=severity,
                suppressed_reason="patient baseline is not established yet",
                consecutive_windows=consecutive,
            )

        signal = observation.dominant_signal or "unspecified"
        if signal in thresholds.muted_signals:
            return ClassificationOutcome(
                fired=False,
                severity=severity,
                suppressed_reason=f"{signal} is muted for this patient",
                consecutive_windows=consecutive,
            )

        if consecutive < thresholds.min_consecutive:
            return ClassificationOutcome(
                fired=False,
                severity=severity,
                suppressed_reason=(
                    f"{consecutive} of {thresholds.min_consecutive} consecutive windows "
                    "above threshold"
                ),
                consecutive_windows=consecutive,
            )

        if self._in_cooldown(state, severity, thresholds, now):
            return ClassificationOutcome(
                fired=False,
                severity=severity,
                suppressed_reason=f"within the {thresholds.cooldown_hours} hour cooldown",
                consecutive_windows=consecutive,
            )

        alert = Alert(
            alert_id=str(uuid.uuid4()),
            patient_code=observation.patient_code,
            fired_at=observation.scored_at,
            severity=severity,
            risk_score=observation.risk_score,
            trigger_signal=signal,
            summary=self.summarise(severity, signal, consecutive),
            consecutive_windows=consecutive,
        )

        state.last_fired_at[severity] = now
        logger.info(
            "alert fired",
            extra={"severity": severity.value, "signal": signal, "windows": consecutive},
        )

        return ClassificationOutcome(
            fired=True,
            alert=alert,
            severity=severity,
            suppressed_reason=None,
            consecutive_windows=consecutive,
        )

    @staticmethod
    def severity_for(score: float, thresholds: AlertThresholds) -> Severity:
        if score >= thresholds.critical_score:
            return Severity.CRITICAL
        if score >= thresholds.elevated_score:
            return Severity.ELEVATED
        return Severity.STABLE

    @staticmethod
    def summarise(severity: Severity, signal: str, consecutive: int) -> str:
        """One line a clinician can act on without opening the dashboard."""
        phrase = SIGNAL_PHRASING.get(signal, "a behavioral pattern outside their baseline")
        window_word = "window" if consecutive == 1 else "windows"
        lead = "Sustained deviation" if severity is Severity.CRITICAL else "Deviation"
        return f"{lead} across {consecutive} {window_word}: {phrase}."

    def reset(self, patient_code: str) -> None:
        """Clear rolling state, for example after a baseline rebuild."""
        self._state.pop(patient_code, None)

    # -- internals --------------------------------------------------------

    def _consecutive_at_or_above(
        self, state: PatientState, severity: Severity, thresholds: AlertThresholds
    ) -> int:
        """Count the current unbroken run at or above the severity floor."""
        if severity is Severity.STABLE:
            return 0

        floor = (
            thresholds.critical_score
            if severity is Severity.CRITICAL
            else thresholds.elevated_score
        )

        count = 0
        for observation in reversed(state.recent):
            if observation.risk_score >= floor:
                count += 1
            else:
                break
        return count

    def _in_cooldown(
        self,
        state: PatientState,
        severity: Severity,
        thresholds: AlertThresholds,
        now: datetime,
    ) -> bool:
        last = state.last_fired_at.get(severity)
        if last is None:
            return False

        # An escalation is new information and is never held back. Only a
        # repeat at the same severity is suppressed.
        return now - last < timedelta(hours=thresholds.cooldown_hours)


def rank_alerts(alerts: Iterable[Alert], now: datetime | None = None) -> list[AlertFeedItem]:
    """Order the feed by what to look at next.

    Not chronological. A critical alert from this morning outranks an elevated
    alert from ten minutes ago, and an unacknowledged alert climbs as it ages
    so nothing sits in the feed unread for a week.
    """
    now = now or datetime.now(timezone.utc)
    items: list[AlertFeedItem] = []

    for alert in alerts:
        age_hours = max(0.0, (now - alert.fired_at).total_seconds() / 3600.0)
        age_component = 0.0 if alert.acknowledged_at else min(
            age_hours * AGE_RANK_PER_HOUR, MAX_AGE_RANK
        )

        rank = SEVERITY_RANK[alert.severity] + alert.risk_score + age_component
        if alert.acknowledged_at:
            # Acknowledged alerts stay visible for context and sink below
            # everything still needing a decision.
            rank -= 2000.0

        items.append(
            AlertFeedItem(
                alert_id=alert.alert_id,
                patient_code=alert.patient_code,
                fired_at=alert.fired_at,
                severity=alert.severity,
                risk_score=alert.risk_score,
                trigger_signal=alert.trigger_signal,
                summary=alert.summary,
                acknowledged=alert.acknowledged_at is not None,
                rank=round(rank, 3),
            )
        )

    return sorted(items, key=lambda item: (-item.rank, item.fired_at))


def panel_summary(alerts: Sequence[Alert]) -> dict[str, int]:
    """Counts by severity for the dashboard header."""
    counts = {severity.value: 0 for severity in Severity}
    for alert in alerts:
        counts[alert.severity.value] += 1
    counts["unacknowledged"] = sum(1 for a in alerts if a.acknowledged_at is None)
    return counts
