from __future__ import annotations

from datetime import timedelta

from app.engine.baseline_builder import build_baseline, expected_for_hour
from app.schemas import BaselineQuality
from app.schemas import BehavioralFeatureName as F
from app.schemas import BehavioralSample

from .conftest import BASE_START, generate_samples


class TestQualityGating:
    def test_fourteen_days_produces_an_established_baseline(self, fourteen_day_samples) -> None:
        baseline = build_baseline("TRC-2f81a9c4", fourteen_day_samples)

        assert baseline.quality is BaselineQuality.ESTABLISHED
        assert baseline.sample_days == 14
        assert len(baseline.features) == len(F)

    def test_ten_days_is_provisional(self) -> None:
        baseline = build_baseline("TRC-2f81a9c4", generate_samples(days=10))
        assert baseline.quality is BaselineQuality.PROVISIONAL

    def test_three_days_is_insufficient(self) -> None:
        baseline = build_baseline("TRC-2f81a9c4", generate_samples(days=3))
        assert baseline.quality is BaselineQuality.INSUFFICIENT

    def test_an_empty_sample_set_is_insufficient_rather_than_an_error(self) -> None:
        baseline = build_baseline("TRC-2f81a9c4", [])

        assert baseline.quality is BaselineQuality.INSUFFICIENT
        assert baseline.features == []
        assert "no usable samples" in baseline.notes


class TestWindowing:
    def test_only_the_first_window_is_used(self) -> None:
        samples = generate_samples(days=40)
        baseline = build_baseline("TRC-2f81a9c4", samples, window_days=14)

        assert baseline.window_end - baseline.window_start <= timedelta(days=14)
        assert baseline.sample_days == 14

    def test_the_window_honours_a_custom_length(self) -> None:
        baseline = build_baseline("TRC-2f81a9c4", generate_samples(days=40), window_days=21)
        assert baseline.sample_days == 21


class TestRobustStatistics:
    def test_the_median_lands_near_the_generated_shape(self, fourteen_day_samples) -> None:
        baseline = build_baseline("TRC-2f81a9c4", fourteen_day_samples)
        radius = next(f for f in baseline.features if f.feature is F.MOBILITY_RADIUS_M)

        # Overnight, daytime and evening values are 12, 2400 and 480. The
        # hourly median over a full day sits in the lower band because most
        # hours are overnight or evening.
        assert 10 < radius.median < 2400
        assert radius.mad > 0

    def test_one_extreme_week_does_not_blow_up_the_scale(self) -> None:
        samples = generate_samples(days=14)
        holiday = [
            BehavioralSample(
                observed_at=s.observed_at,
                feature=s.feature,
                value=s.value * (40.0 if s.feature is F.MOBILITY_RADIUS_M else 1.0),
                confidence=s.confidence,
            )
            if s.observed_at.day in (10, 11)
            else s
            for s in samples
        ]

        clean = build_baseline("TRC-2f81a9c4", samples)
        with_holiday = build_baseline("TRC-2f81a9c4", holiday)

        clean_scale = next(
            f for f in clean.features if f.feature is F.MOBILITY_RADIUS_M
        ).mad
        holiday_scale = next(
            f for f in with_holiday.features if f.feature is F.MOBILITY_RADIUS_M
        ).mad

        # A mean and standard deviation would roughly triple here. The median
        # absolute deviation absorbs two days out of fourteen.
        assert holiday_scale < clean_scale * 1.6

    def test_a_constant_feature_gets_a_scale_floor_rather_than_zero(self) -> None:
        constant = [
            BehavioralSample(
                observed_at=BASE_START + timedelta(hours=h),
                feature=F.SCREEN_ON_MINUTES,
                value=20.0,
                confidence=1.0,
            )
            for h in range(24 * 14)
        ]

        baseline = build_baseline("TRC-2f81a9c4", constant)
        screen = next(f for f in baseline.features if f.feature is F.SCREEN_ON_MINUTES)

        assert screen.mad > 0
        assert screen.mad == 1.0  # five percent of a median of twenty

    def test_a_zero_median_feature_still_gets_a_positive_scale(self) -> None:
        zeros = [
            BehavioralSample(
                observed_at=BASE_START + timedelta(hours=h),
                feature=F.NIGHT_USAGE_MINUTES,
                value=0.0,
                confidence=1.0,
            )
            for h in range(24 * 14)
        ]

        baseline = build_baseline("TRC-2f81a9c4", zeros)
        night = next(f for f in baseline.features if f.feature is F.NIGHT_USAGE_MINUTES)

        assert night.mad > 0


class TestConfidenceHandling:
    def test_low_confidence_samples_are_excluded(self) -> None:
        samples = generate_samples(days=14, confidence=0.05)
        baseline = build_baseline("TRC-2f81a9c4", samples)

        assert baseline.quality is BaselineQuality.INSUFFICIENT
        assert any("low confidence" in note for note in baseline.notes)

    def test_partial_confidence_still_builds_a_baseline(self) -> None:
        samples = generate_samples(days=14, confidence=0.6)
        baseline = build_baseline("TRC-2f81a9c4", samples)

        assert baseline.quality is BaselineQuality.ESTABLISHED


class TestCircadianProfile:
    def test_every_feature_carries_a_full_hourly_profile(self, fourteen_day_samples) -> None:
        baseline = build_baseline("TRC-2f81a9c4", fourteen_day_samples)

        for feature in baseline.features:
            assert len(feature.hourly_median) == 24

    def test_the_profile_separates_night_from_day(self, fourteen_day_samples) -> None:
        baseline = build_baseline("TRC-2f81a9c4", fourteen_day_samples)
        activity = next(f for f in baseline.features if f.feature is F.ACTIVITY_RATIO)

        assert expected_for_hour(activity, 3) < expected_for_hour(activity, 12)

    def test_expected_for_hour_falls_back_to_the_overall_median(self, fourteen_day_samples) -> None:
        baseline = build_baseline("TRC-2f81a9c4", fourteen_day_samples)
        activity = next(f for f in baseline.features if f.feature is F.ACTIVITY_RATIO)
        stripped = activity.model_copy(update={"hourly_median": []})

        assert expected_for_hour(stripped, 12) == stripped.median

    def test_weekday_and_weekend_medians_are_reported_separately(
        self, fourteen_day_samples
    ) -> None:
        baseline = build_baseline("TRC-2f81a9c4", fourteen_day_samples)
        radius = next(f for f in baseline.features if f.feature is F.MOBILITY_RADIUS_M)

        assert radius.weekday_median is not None
        assert radius.weekend_median is not None


class TestSparseFeatures:
    def test_a_feature_with_too_few_observations_is_skipped_and_noted(self) -> None:
        samples = generate_samples(days=14, features=(F.ACTIVITY_RATIO,))
        samples += [
            BehavioralSample(
                observed_at=BASE_START + timedelta(hours=h),
                feature=F.PLACE_COUNT,
                value=2.0,
                confidence=1.0,
            )
            for h in range(5)
        ]

        baseline = build_baseline("TRC-2f81a9c4", samples)
        present = {f.feature for f in baseline.features}

        assert F.ACTIVITY_RATIO in present
        assert F.PLACE_COUNT not in present
        assert any("place_count" in note for note in baseline.notes)
