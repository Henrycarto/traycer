"""Synthetic patients.

Every test in this service runs against generated data with a known shape.
That is the only honest option: real behavioral traces from psychiatric
patients do not belong in a public repository, and a fixture that came from
one patient would encode that patient's routine into the test suite.

The generator produces a stable daily rhythm with controllable noise, and
`apply_relapse_signature` bends it in the directions the literature describes
for a depressive prodrome. Tests assert that the detector notices the bend.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas import BehavioralFeatureName as F
from app.schemas import BehavioralSample

BASE_START = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)

# Daily shape per feature: (overnight value, daytime value, evening value).
DAILY_SHAPE: dict[F, tuple[float, float, float]] = {
    F.ACTIVITY_RATIO: (0.02, 0.34, 0.18),
    F.MOVEMENT_INTENSITY: (0.05, 0.42, 0.25),
    F.LONGEST_SEDENTARY_MINUTES: (55.0, 12.0, 26.0),
    F.ACTIVITY_FRAGMENTATION: (0.4, 6.2, 3.5),
    F.MOBILITY_RADIUS_M: (12.0, 2400.0, 480.0),
    F.LOCATION_ENTROPY: (0.05, 1.35, 0.6),
    F.PLACE_COUNT: (1.0, 3.0, 2.0),
    F.PRIMARY_PLACE_RATIO: (0.98, 0.24, 0.65),
    F.SCREEN_ON_MINUTES: (0.6, 9.4, 18.2),
    F.UNLOCK_COUNT: (0.3, 6.0, 9.0),
    F.MEAN_SESSION_MINUTES: (0.5, 2.4, 3.6),
    F.USAGE_LOOP_INDEX: (0.1, 0.9, 1.3),
    F.NIGHT_USAGE_MINUTES: (2.1, 0.0, 0.0),
    F.COMMUNICATION_SESSION_COUNT: (0.1, 1.6, 2.4),
    F.COMMUNICATION_MINUTES: (0.2, 4.1, 7.8),
}


def _shape_value(feature: F, hour: int) -> float:
    overnight, daytime, evening = DAILY_SHAPE[feature]
    if hour < 7:
        return overnight
    if hour < 18:
        return daytime
    return evening


def generate_samples(
    days: int,
    start: datetime = BASE_START,
    noise: float = 0.08,
    seed: int = 20260105,
    features: tuple[F, ...] | None = None,
    confidence: float = 1.0,
) -> list[BehavioralSample]:
    """Hourly samples for every feature over the requested number of days."""
    rng = random.Random(seed)
    chosen = features or tuple(DAILY_SHAPE)
    samples: list[BehavioralSample] = []

    for day in range(days):
        for hour in range(24):
            observed_at = start + timedelta(days=day, hours=hour)
            # A gentle weekly cycle so weekday and weekend medians differ, the
            # way they do for almost everyone.
            weekly = 1.0 + 0.12 * math.sin(2 * math.pi * day / 7)

            for feature in chosen:
                base = _shape_value(feature, hour) * weekly
                jitter = 1.0 + rng.uniform(-noise, noise)
                samples.append(
                    BehavioralSample(
                        observed_at=observed_at,
                        feature=feature,
                        value=max(0.0, base * jitter),
                        confidence=confidence,
                    )
                )

    return samples


def apply_relapse_signature(
    samples: list[BehavioralSample], intensity: float = 1.0
) -> list[BehavioralSample]:
    """Bend a window toward a depressive prodrome.

    The directions are the ones described in the digital phenotyping
    literature: life space contracts, routine variety drops, the patient stays
    at their primary place, activity falls, and phone use moves into the night.
    """
    multipliers: dict[F, float] = {
        F.MOBILITY_RADIUS_M: 1.0 - 0.72 * intensity,
        F.LOCATION_ENTROPY: 1.0 - 0.65 * intensity,
        F.PLACE_COUNT: 1.0 - 0.5 * intensity,
        F.PRIMARY_PLACE_RATIO: 1.0 + 0.38 * intensity,
        F.ACTIVITY_RATIO: 1.0 - 0.55 * intensity,
        F.MOVEMENT_INTENSITY: 1.0 - 0.45 * intensity,
        F.LONGEST_SEDENTARY_MINUTES: 1.0 + 0.9 * intensity,
        F.NIGHT_USAGE_MINUTES: 1.0 + 3.2 * intensity,
        F.COMMUNICATION_SESSION_COUNT: 1.0 - 0.7 * intensity,
        F.COMMUNICATION_MINUTES: 1.0 - 0.75 * intensity,
    }

    return [
        BehavioralSample(
            observed_at=sample.observed_at,
            feature=sample.feature,
            value=max(0.0, sample.value * multipliers.get(sample.feature, 1.0)),
            confidence=sample.confidence,
        )
        for sample in samples
    ]


@pytest.fixture
def fourteen_day_samples() -> list[BehavioralSample]:
    return generate_samples(days=14)


@pytest.fixture
def one_day_samples() -> list[BehavioralSample]:
    return generate_samples(days=1, start=BASE_START + timedelta(days=20), seed=7)
