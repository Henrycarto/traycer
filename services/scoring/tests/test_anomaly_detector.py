from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.engine.anomaly_detector import (
    PLACEHOLDER_COEFFICIENTS,
    AnomalyDetector,
    DirectionWeight,
    load_coefficients,
)
from app.engine.baseline_builder import build_baseline
from app.schemas import BaselineQuality
from app.schemas import BehavioralFeatureName as F
from app.schemas import BehavioralSample, Severity

from .conftest import BASE_START, apply_relapse_signature, generate_samples


@pytest.fixture
def detector() -> AnomalyDetector:
    return AnomalyDetector(PLACEHOLDER_COEFFICIENTS)


@pytest.fixture
def established_baseline(fourteen_day_samples):
    return build_baseline("TRC-2f81a9c4", fourteen_day_samples)


def _later_day(seed: int = 91, noise: float = 0.08):
    return generate_samples(
        days=1, start=BASE_START + timedelta(days=20), seed=seed, noise=noise
    )


class TestStableBehavior:
    def test_a_day_that_matches_the_baseline_scores_low(
        self, detector, established_baseline
    ) -> None:
        result = detector.score(_later_day(), established_baseline)

        assert result.severity is Severity.STABLE
        assert result.risk_score < 40

    def test_every_feature_gets_a_deviation_entry(self, detector, established_baseline) -> None:
        result = detector.score(_later_day(), established_baseline)
        assert len(result.deviations) == len(F)

    def test_deviations_come_back_ranked(self, detector, established_baseline) -> None:
        result = detector.score(_later_day(), established_baseline)
        magnitudes = [d.weighted_deviation for d in result.deviations]

        assert magnitudes == sorted(magnitudes, reverse=True)


class TestRelapseSignature:
    def test_a_prodromal_day_scores_higher_than_a_stable_one(
        self, detector, established_baseline
    ) -> None:
        stable = detector.score(_later_day(), established_baseline)
        prodromal = detector.score(
            apply_relapse_signature(_later_day(), intensity=1.0), established_baseline
        )

        assert prodromal.risk_score > stable.risk_score

    def test_the_score_rises_with_the_intensity_of_the_change(
        self, detector, established_baseline
    ) -> None:
        scores = [
            detector.score(
                apply_relapse_signature(_later_day(), intensity=level), established_baseline
            ).risk_score
            for level in (0.0, 0.35, 0.7, 1.0)
        ]

        assert scores == sorted(scores)

    def test_the_dominant_signal_is_one_of_the_bent_features(
        self, detector, established_baseline
    ) -> None:
        result = detector.score(
            apply_relapse_signature(_later_day(), intensity=1.0), established_baseline
        )

        bent = {
            F.MOBILITY_RADIUS_M,
            F.LOCATION_ENTROPY,
            F.PLACE_COUNT,
            F.PRIMARY_PLACE_RATIO,
            F.ACTIVITY_RATIO,
            F.MOVEMENT_INTENSITY,
            F.LONGEST_SEDENTARY_MINUTES,
            F.NIGHT_USAGE_MINUTES,
            F.COMMUNICATION_SESSION_COUNT,
            F.COMMUNICATION_MINUTES,
        }
        assert result.dominant_signal in bent

    def test_the_sign_of_the_z_score_records_the_direction(
        self, detector, established_baseline
    ) -> None:
        result = detector.score(
            apply_relapse_signature(_later_day(), intensity=1.0), established_baseline
        )
        by_feature = {d.feature: d for d in result.deviations}

        # Life space contracted, so radius is below baseline and the patient
        # spent more of the day at their primary place.
        assert by_feature[F.MOBILITY_RADIUS_M].deviation_z < 0
        assert by_feature[F.PRIMARY_PLACE_RATIO].deviation_z > 0


class TestBaselineQualityHandling:
    def test_an_insufficient_baseline_produces_no_score(self, detector) -> None:
        thin = build_baseline("TRC-2f81a9c4", generate_samples(days=2))
        result = detector.score(_later_day(), thin)

        assert result.risk_score == 0.0
        assert result.severity is Severity.STABLE
        assert any("insufficient" in note for note in result.notes)

    def test_a_provisional_baseline_shrinks_the_score(self, detector) -> None:
        provisional = build_baseline("TRC-2f81a9c4", generate_samples(days=10))
        established = build_baseline("TRC-2f81a9c4", generate_samples(days=14))

        window = apply_relapse_signature(_later_day(), intensity=1.0)

        assert provisional.quality is BaselineQuality.PROVISIONAL
        assert (
            detector.score(window, provisional).risk_score
            < detector.score(window, established).risk_score
        )

    def test_a_provisional_score_says_so(self, detector) -> None:
        provisional = build_baseline("TRC-2f81a9c4", generate_samples(days=10))
        result = detector.score(_later_day(), provisional)

        assert any("provisional" in note for note in result.notes)


class TestConfidenceDiscount:
    def test_a_thinly_sensed_window_scores_lower_than_a_dense_one(
        self, detector, established_baseline
    ) -> None:
        window = apply_relapse_signature(_later_day(), intensity=1.0)
        thin = [
            BehavioralSample(
                observed_at=s.observed_at,
                feature=s.feature,
                value=s.value,
                confidence=0.15,
            )
            for s in window
        ]

        assert (
            detector.score(thin, established_baseline).risk_score
            < detector.score(window, established_baseline).risk_score
        )


class TestSaturation:
    def test_one_broken_sensor_cannot_carry_the_whole_score(
        self, detector, established_baseline
    ) -> None:
        window = _later_day()
        broken = [
            BehavioralSample(
                observed_at=s.observed_at,
                feature=s.feature,
                value=s.value * (5000.0 if s.feature is F.UNLOCK_COUNT else 1.0),
                confidence=s.confidence,
            )
            for s in window
        ]

        result = detector.score(broken, established_baseline)
        unlock = next(d for d in result.deviations if d.feature is F.UNLOCK_COUNT)

        assert unlock.weighted_deviation <= PLACEHOLDER_COEFFICIENTS.saturation
        assert result.risk_score <= 100.0


class TestDirectionAsymmetry:
    def test_an_asymmetric_coefficient_set_weights_the_two_directions_differently(
        self, established_baseline
    ) -> None:
        """The clinical set is private, so this proves the mechanism works.

        With mobility contraction weighted three times as heavily as
        expansion, the same magnitude of change has to produce a larger score
        when the radius falls.
        """
        asymmetric = replace(
            PLACEHOLDER_COEFFICIENTS,
            name="test-asymmetric",
            directions={
                **PLACEHOLDER_COEFFICIENTS.directions,
                F.MOBILITY_RADIUS_M: DirectionWeight(rising=1.0, falling=3.0),
            },
        )
        detector = AnomalyDetector(asymmetric)

        window = _later_day()
        contracted = _scale_feature(window, F.MOBILITY_RADIUS_M, 0.5)
        expanded = _scale_feature(window, F.MOBILITY_RADIUS_M, 1.5)

        falling = next(
            d
            for d in detector.score(contracted, established_baseline).deviations
            if d.feature is F.MOBILITY_RADIUS_M
        )
        rising = next(
            d
            for d in detector.score(expanded, established_baseline).deviations
            if d.feature is F.MOBILITY_RADIUS_M
        )

        assert falling.weighted_deviation > rising.weighted_deviation


class TestCoefficientLoading:
    def test_the_placeholder_set_is_flagged_as_uncalibrated(self) -> None:
        assert PLACEHOLDER_COEFFICIENTS.clinically_calibrated is False
        assert any("not clinically calibrated" in note for note in PLACEHOLDER_COEFFICIENTS.notes)

    def test_loading_without_the_private_package_falls_back(self) -> None:
        coefficients = load_coefficients()

        # The private package is absent from the public repository, so this is
        # the expected path in CI.
        assert coefficients.name == PLACEHOLDER_COEFFICIENTS.name
        assert coefficients.clinically_calibrated is False

    def test_a_detector_reports_its_calibration_state(self, detector) -> None:
        assert detector.clinically_calibrated is False
        assert detector.coefficient_name == "public-placeholder-v1"


class TestSeverityBands:
    def test_bands_follow_the_configured_thresholds(self, detector) -> None:
        assert detector.severity_for(10.0) is Severity.STABLE
        assert detector.severity_for(54.9) is Severity.STABLE
        assert detector.severity_for(55.0) is Severity.ELEVATED
        assert detector.severity_for(74.9) is Severity.ELEVATED
        assert detector.severity_for(75.0) is Severity.CRITICAL
        assert detector.severity_for(100.0) is Severity.CRITICAL


def _scale_feature(samples, feature: F, factor: float):
    return [
        BehavioralSample(
            observed_at=s.observed_at,
            feature=s.feature,
            value=s.value * (factor if s.feature is feature else 1.0),
            confidence=s.confidence,
        )
        for s in samples
    ]
