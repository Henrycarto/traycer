from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.engine.alert_classifier import AlertClassifier, panel_summary, rank_alerts
from app.engine.threshold_config import default_thresholds
from app.schemas import Alert, ScoreObservation, Severity

START = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


def observation(
    score: float,
    minutes: int = 0,
    signal: str = "mobility_radius_m",
    calibrated: bool = True,
    baseline_quality: str = "established",
) -> ScoreObservation:
    return ScoreObservation(
        patient_code="TRC-2f81a9c4",
        scored_at=START + timedelta(minutes=minutes),
        risk_score=score,
        baseline_quality=baseline_quality,
        dominant_signal=signal,
        clinically_calibrated=calibrated,
    )


@pytest.fixture
def classifier() -> AlertClassifier:
    return AlertClassifier()


@pytest.fixture
def thresholds():
    return default_thresholds("TRC-2f81a9c4")


class TestSeverityBands:
    def test_a_stable_score_produces_nothing(self, classifier, thresholds) -> None:
        outcome = classifier.classify(observation(20.0), thresholds)

        assert outcome.fired is False
        assert outcome.severity is Severity.STABLE

    def test_bands_match_the_configured_thresholds(self, thresholds) -> None:
        assert AlertClassifier.severity_for(54.9, thresholds) is Severity.STABLE
        assert AlertClassifier.severity_for(55.0, thresholds) is Severity.ELEVATED
        assert AlertClassifier.severity_for(75.0, thresholds) is Severity.CRITICAL


class TestConfirmation:
    def test_one_window_above_threshold_does_not_fire(self, classifier, thresholds) -> None:
        outcome = classifier.classify(observation(60.0), thresholds)

        assert outcome.fired is False
        assert "1 of 2 consecutive" in (outcome.suppressed_reason or "")

    def test_the_second_consecutive_window_fires(self, classifier, thresholds) -> None:
        classifier.classify(observation(60.0, minutes=0), thresholds)
        outcome = classifier.classify(observation(62.0, minutes=60), thresholds)

        assert outcome.fired is True
        assert outcome.alert is not None
        assert outcome.alert.severity is Severity.ELEVATED
        assert outcome.alert.consecutive_windows == 2

    def test_a_dip_below_threshold_resets_the_run(self, classifier, thresholds) -> None:
        classifier.classify(observation(60.0, minutes=0), thresholds)
        classifier.classify(observation(30.0, minutes=60), thresholds)
        outcome = classifier.classify(observation(61.0, minutes=120), thresholds)

        assert outcome.fired is False
        assert outcome.consecutive_windows == 1

    def test_a_higher_min_consecutive_delays_the_alert(self, classifier, thresholds) -> None:
        strict = thresholds.model_copy(update={"min_consecutive": 4})

        for minute in range(0, 180, 60):
            assert classifier.classify(observation(60.0, minutes=minute), strict).fired is False

        assert classifier.classify(observation(60.0, minutes=180), strict).fired is True


class TestCooldown:
    def test_a_repeat_at_the_same_severity_is_held(self, classifier, thresholds) -> None:
        now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

        classifier.classify(observation(60.0, minutes=0), thresholds, now=now)
        fired = classifier.classify(observation(61.0, minutes=60), thresholds, now=now)
        assert fired.fired is True

        repeat = classifier.classify(
            observation(62.0, minutes=120), thresholds, now=now + timedelta(hours=2)
        )
        assert repeat.fired is False
        assert "cooldown" in (repeat.suppressed_reason or "")

    def test_the_cooldown_expires(self, classifier, thresholds) -> None:
        now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

        classifier.classify(observation(60.0, minutes=0), thresholds, now=now)
        classifier.classify(observation(61.0, minutes=60), thresholds, now=now)

        later = classifier.classify(
            observation(62.0, minutes=120), thresholds, now=now + timedelta(hours=25)
        )
        assert later.fired is True

    def test_an_escalation_to_critical_bypasses_the_cooldown(self, classifier, thresholds) -> None:
        now = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

        classifier.classify(observation(60.0, minutes=0), thresholds, now=now)
        assert classifier.classify(observation(61.0, minutes=60), thresholds, now=now).fired

        # Deterioration to critical is new information and must not be held by
        # the elevated cooldown.
        classifier.classify(observation(80.0, minutes=120), thresholds, now=now)
        escalated = classifier.classify(observation(82.0, minutes=180), thresholds, now=now)

        assert escalated.fired is True
        assert escalated.severity is Severity.CRITICAL


class TestSuppressionRules:
    def test_an_uncalibrated_score_never_fires(self, classifier, thresholds) -> None:
        classifier.classify(observation(90.0, minutes=0, calibrated=False), thresholds)
        outcome = classifier.classify(observation(92.0, minutes=60, calibrated=False), thresholds)

        assert outcome.fired is False
        assert "uncalibrated" in (outcome.suppressed_reason or "")

    def test_an_unbuilt_baseline_never_fires(self, classifier, thresholds) -> None:
        classifier.classify(observation(90.0, minutes=0, baseline_quality="insufficient"), thresholds)
        outcome = classifier.classify(
            observation(92.0, minutes=60, baseline_quality="insufficient"), thresholds
        )

        assert outcome.fired is False
        assert "baseline" in (outcome.suppressed_reason or "")

    def test_a_muted_signal_is_suppressed_for_that_patient_only(
        self, classifier, thresholds
    ) -> None:
        muted = thresholds.model_copy(update={"muted_signals": ["mobility_radius_m"]})

        classifier.classify(observation(80.0, minutes=0, signal="mobility_radius_m"), muted)
        blocked = classifier.classify(
            observation(82.0, minutes=60, signal="mobility_radius_m"), muted
        )
        assert blocked.fired is False
        assert "muted" in (blocked.suppressed_reason or "")

        # A different signal on the same patient still fires.
        classifier.reset("TRC-2f81a9c4")
        classifier.classify(observation(80.0, minutes=120, signal="night_usage_minutes"), muted)
        allowed = classifier.classify(
            observation(82.0, minutes=180, signal="night_usage_minutes"), muted
        )
        assert allowed.fired is True


class TestSummaryText:
    def test_the_summary_is_plain_language_about_the_signal(self, classifier, thresholds) -> None:
        classifier.classify(observation(60.0, minutes=0), thresholds)
        outcome = classifier.classify(observation(61.0, minutes=60), thresholds)

        assert outcome.alert is not None
        assert "movement radius" in outcome.alert.summary
        assert "mobility_radius_m" not in outcome.alert.summary

    def test_an_unknown_signal_still_reads_as_a_sentence(self) -> None:
        text = AlertClassifier.summarise(Severity.ELEVATED, "something_new", 2)
        assert text.endswith(".")
        assert "outside their baseline" in text

    def test_a_critical_summary_reads_differently_from_an_elevated_one(self) -> None:
        elevated = AlertClassifier.summarise(Severity.ELEVATED, "activity_ratio", 2)
        critical = AlertClassifier.summarise(Severity.CRITICAL, "activity_ratio", 4)

        assert elevated != critical
        assert critical.startswith("Sustained")


class TestRanking:
    def _alert(self, severity: Severity, score: float, hours_ago: float, acked=False) -> Alert:
        now = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
        return Alert(
            alert_id=f"{severity.value}-{score}-{hours_ago}",
            patient_code="TRC-2f81a9c4",
            fired_at=now - timedelta(hours=hours_ago),
            severity=severity,
            risk_score=score,
            trigger_signal="activity_ratio",
            summary="test",
            consecutive_windows=2,
            acknowledged_at=now if acked else None,
        )

    def test_critical_outranks_a_more_recent_elevated(self) -> None:
        now = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
        items = rank_alerts(
            [self._alert(Severity.ELEVATED, 60.0, 0.1), self._alert(Severity.CRITICAL, 80.0, 8)],
            now=now,
        )

        assert items[0].severity is Severity.CRITICAL

    def test_an_unacknowledged_alert_climbs_as_it_ages(self) -> None:
        now = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
        items = rank_alerts(
            [self._alert(Severity.ELEVATED, 60.0, 0.1), self._alert(Severity.ELEVATED, 60.0, 20)],
            now=now,
        )

        assert items[0].fired_at < items[1].fired_at

    def test_acknowledged_alerts_sink_below_everything_open(self) -> None:
        now = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
        items = rank_alerts(
            [
                self._alert(Severity.CRITICAL, 90.0, 1, acked=True),
                self._alert(Severity.ELEVATED, 56.0, 1),
            ],
            now=now,
        )

        assert items[0].severity is Severity.ELEVATED
        assert items[1].acknowledged is True

    def test_an_empty_feed_ranks_to_an_empty_list(self) -> None:
        assert rank_alerts([]) == []


class TestPanelSummary:
    def test_counts_by_severity_and_open_state(self) -> None:
        now = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
        alerts = [
            Alert(
                alert_id="a",
                patient_code="TRC-1",
                fired_at=now,
                severity=Severity.CRITICAL,
                risk_score=88.0,
                trigger_signal="activity_ratio",
                summary="s",
                consecutive_windows=3,
            ),
            Alert(
                alert_id="b",
                patient_code="TRC-2",
                fired_at=now,
                severity=Severity.ELEVATED,
                risk_score=60.0,
                trigger_signal="night_usage_minutes",
                summary="s",
                consecutive_windows=2,
                acknowledged_at=now,
            ),
        ]

        counts = panel_summary(alerts)
        assert counts["critical"] == 1
        assert counts["elevated"] == 1
        assert counts["unacknowledged"] == 1


class TestStateIsolation:
    def test_two_patients_do_not_share_a_confirmation_counter(self, classifier, thresholds) -> None:
        first = ScoreObservation(
            patient_code="TRC-aaaaaa",
            scored_at=START,
            risk_score=60.0,
            baseline_quality="established",
            dominant_signal="activity_ratio",
            clinically_calibrated=True,
        )
        second = first.model_copy(update={"patient_code": "TRC-bbbbbb"})

        classifier.classify(first, thresholds.model_copy(update={"patient_code": "TRC-aaaaaa"}))
        outcome = classifier.classify(
            second, thresholds.model_copy(update={"patient_code": "TRC-bbbbbb"})
        )

        assert outcome.fired is False
        assert outcome.consecutive_windows == 1

    def test_reset_clears_the_run(self, classifier, thresholds) -> None:
        classifier.classify(observation(60.0, minutes=0), thresholds)
        classifier.reset("TRC-2f81a9c4")
        outcome = classifier.classify(observation(61.0, minutes=60), thresholds)

        assert outcome.fired is False
        assert outcome.consecutive_windows == 1
