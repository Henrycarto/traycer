"""Builds a patient's personal behavioral baseline.

Traycer scores every patient against themselves, not against a population.
That choice is the whole reason the platform works on a panel of forty rather
than a cohort of forty thousand.

A shift worker who sleeps at 09:00, a retiree who leaves the house twice a
week, and a graduate student who walks eleven kilometres a day have nothing in
common on a population distribution. All three are unremarkable against their
own fourteen day baseline, and all three produce a clear signal when their own
pattern breaks. A population model spends its capacity learning that people
differ. A personal model spends it learning that this person changed.

The cost is the fourteen day cold start, during which the platform can observe
but cannot alert. That is stated plainly to clinicians rather than hidden
behind a provisional score.

Every statistic here resists outliers. Median and median absolute deviation
instead of mean and standard deviation, because a single week away from home
would otherwise inflate the scale parameter enough to mask a genuine change
for the following month.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

import numpy as np

from ..schemas import (
    BaselineQuality,
    BehavioralFeatureName,
    BehavioralSample,
    FeatureBaseline,
    PatientBaseline,
)

logger = logging.getLogger("traycer.scoring.baseline")

# Scale factor that makes the median absolute deviation a consistent estimator
# of the standard deviation for normally distributed data.
MAD_TO_SIGMA = 1.4826

# A feature needs this many observations before it gets a baseline entry.
MIN_OBSERVATIONS_PER_FEATURE = 24

# Days of coverage required for each quality level.
PROVISIONAL_DAYS = 7
ESTABLISHED_DAYS = 14

# Samples below this confidence contribute nothing. A window where the sensor
# was readable for under a fifth of the period is closer to a gap than to a
# measurement.
MIN_USABLE_CONFIDENCE = 0.2

# Floor on the scale estimate, expressed as a fraction of the median. Without
# it a feature that was perfectly constant during the baseline window produces
# a MAD of zero, and every later observation divides by zero into an infinite
# deviation.
MIN_SCALE_FRACTION = 0.05

# Absolute floor for features whose baseline median is itself zero, such as
# night usage for a patient who slept through the whole baseline window.
ABSOLUTE_SCALE_FLOOR = 1e-3


def build_baseline(
    patient_code: str,
    samples: Sequence[BehavioralSample],
    window_days: int = ESTABLISHED_DAYS,
    now: datetime | None = None,
) -> PatientBaseline:
    """Construct a personal baseline from the earliest window of SDK data."""
    now = now or datetime.now(timezone.utc)
    notes: list[str] = []

    usable = [s for s in samples if s.confidence >= MIN_USABLE_CONFIDENCE]
    dropped = len(samples) - len(usable)
    if dropped:
        notes.append(f"{dropped} low confidence samples excluded")

    if not usable:
        return PatientBaseline(
            patient_code=patient_code,
            built_at=now,
            window_start=now,
            window_end=now,
            sample_days=0,
            quality=BaselineQuality.INSUFFICIENT,
            features=[],
            notes=notes + ["no usable samples"],
        )

    ordered = sorted(usable, key=lambda s: s.observed_at)
    window_start = ordered[0].observed_at

    # The cutoff is exclusive. An inclusive bound at start plus fourteen days
    # pulls in the first observation of day fifteen and reports coverage the
    # patient does not have.
    cutoff = window_start + timedelta(days=window_days)
    in_window = [s for s in ordered if s.observed_at < cutoff]
    window_end = in_window[-1].observed_at if in_window else window_start

    by_feature: dict[BehavioralFeatureName, list[BehavioralSample]] = defaultdict(list)
    for sample in in_window:
        by_feature[sample.feature].append(sample)

    features: list[FeatureBaseline] = []
    for feature, feature_samples in sorted(by_feature.items(), key=lambda item: item[0].value):
        baseline = _build_feature_baseline(feature, feature_samples)
        if baseline is None:
            notes.append(
                f"{feature.value} had {len(feature_samples)} observations, "
                f"below the {MIN_OBSERVATIONS_PER_FEATURE} needed"
            )
            continue
        features.append(baseline)

    covered_days = _distinct_days(in_window)
    quality = _quality_for(covered_days, features)

    if quality is not BaselineQuality.ESTABLISHED:
        notes.append(
            f"{covered_days} days of coverage, {ESTABLISHED_DAYS} needed for an "
            "established baseline"
        )

    return PatientBaseline(
        patient_code=patient_code,
        built_at=now,
        window_start=window_start,
        window_end=window_end,
        sample_days=covered_days,
        quality=quality,
        features=features,
        notes=notes,
    )


def _build_feature_baseline(
    feature: BehavioralFeatureName, samples: Sequence[BehavioralSample]
) -> FeatureBaseline | None:
    if len(samples) < MIN_OBSERVATIONS_PER_FEATURE:
        return None

    values = np.asarray([s.value for s in samples], dtype=float)
    weights = np.asarray([s.confidence for s in samples], dtype=float)

    median = float(_weighted_median(values, weights))
    mad = float(np.median(np.abs(values - median)))
    scale = _floor_scale(mad * MAD_TO_SIGMA, median)

    q1, q3 = (float(x) for x in np.percentile(values, [25, 75]))
    p05, p95 = (float(x) for x in np.percentile(values, [5, 95]))

    return FeatureBaseline(
        feature=feature,
        median=round(median, 6),
        mad=round(scale, 6),
        iqr=round(q3 - q1, 6),
        p05=round(p05, 6),
        p95=round(p95, 6),
        observation_count=len(samples),
        effective_days=round(float(np.sum(weights)) / 24.0, 3),
        hourly_median=_hourly_profile(samples),
        weekday_median=_subset_median(samples, weekend=False),
        weekend_median=_subset_median(samples, weekend=True),
    )


def _floor_scale(scale: float, median: float) -> float:
    """Keep the scale estimate away from zero.

    A patient whose screen time was identical every day during the baseline
    window is not infinitely sensitive to a one minute change, which is what a
    zero scale would imply.
    """
    relative_floor = abs(median) * MIN_SCALE_FRACTION
    return max(scale, relative_floor, ABSOLUTE_SCALE_FLOOR)


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """Median with each observation weighted by its sensor confidence."""
    if values.size == 0:
        return 0.0

    total = float(np.sum(weights))
    if total <= 0:
        return float(np.median(values))

    order = np.argsort(values)
    sorted_values = values[order]
    cumulative = np.cumsum(weights[order]) / total

    index = int(np.searchsorted(cumulative, 0.5, side="left"))
    index = min(index, sorted_values.size - 1)
    return float(sorted_values[index])


def _hourly_profile(samples: Sequence[BehavioralSample]) -> list[float]:
    """Median per hour of day.

    Returns 24 values. Hours with no observation inherit the overall median so
    the profile stays usable rather than carrying holes that downstream code
    has to special case.
    """
    buckets: dict[int, list[float]] = defaultdict(list)
    for sample in samples:
        buckets[sample.observed_at.hour].append(sample.value)

    overall = float(np.median([s.value for s in samples])) if samples else 0.0

    profile: list[float] = []
    for hour in range(24):
        values = buckets.get(hour)
        profile.append(round(float(np.median(values)) if values else overall, 6))
    return profile


def _subset_median(samples: Sequence[BehavioralSample], weekend: bool) -> float | None:
    """Weekday and weekend medians, kept separate.

    Most people's mobility and usage patterns differ enough between the two
    that pooling them widens the scale estimate and blunts the detector.
    """
    subset = [s.value for s in samples if (s.observed_at.weekday() >= 5) == weekend]
    if len(subset) < MIN_OBSERVATIONS_PER_FEATURE // 3:
        return None
    return round(float(np.median(subset)), 6)


def _distinct_days(samples: Iterable[BehavioralSample]) -> int:
    return len({s.observed_at.date() for s in samples})


def _quality_for(covered_days: int, features: Sequence[FeatureBaseline]) -> BaselineQuality:
    if not features or covered_days < PROVISIONAL_DAYS:
        return BaselineQuality.INSUFFICIENT
    if covered_days < ESTABLISHED_DAYS:
        return BaselineQuality.PROVISIONAL
    return BaselineQuality.ESTABLISHED


def expected_for_hour(baseline: FeatureBaseline, hour: int) -> float:
    """Circadian aware expected value.

    Falls back to the overall median when the hourly profile is missing, which
    happens for a provisional baseline built from partial coverage.
    """
    if len(baseline.hourly_median) == 24 and 0 <= hour < 24:
        value = baseline.hourly_median[hour]
        if math.isfinite(value):
            return value
    return baseline.median
