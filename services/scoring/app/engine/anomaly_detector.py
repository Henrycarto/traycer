"""Scores a scoring window against a patient's personal baseline.

The architecture in this file is public and complete. Read it, run it, change
it. What is not public is the coefficient set: the per feature weights, the
direction asymmetries, the saturation caps and the logistic parameters that
decide what counts as a clinically significant deviation.

Those numbers came out of labelled relapse episodes. They are the part that
took clinical data to produce and they are the part a competitor cannot
reconstruct from the code. Everything else here is arithmetic that any
competent engineer would write the same way.

When the private coefficient package is not installed, the detector runs on a
neutral placeholder set. It produces a mathematically valid score with no
clinical meaning, and it says so in the response. That is deliberate: the
pipeline stays runnable end to end for anyone evaluating the repository, and
nobody can mistake a demo score for a clinical one.

Scoring pipeline, in order:

1. Reduce the scoring window to one value per feature, weighted by sensor
   confidence.
2. Compare against the circadian aware expected value from the baseline.
3. Divide by the baseline scale to get a scaled deviation, sign preserved.
4. Apply the direction weight, because a mobility collapse and a mobility
   spike are not the same finding.
5. Saturate, so one broken sensor cannot dominate the whole score.
6. Combine with a power mean, which lets a single severe deviation carry the
   score while still letting several moderate ones accumulate.
7. Map to 0 through 100 with a logistic, discount for baseline quality and
   sensor coverage, and assign a severity band.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Sequence

import numpy as np

from ..schemas import (
    BaselineQuality,
    BehavioralFeatureName,
    BehavioralSample,
    FeatureBaseline,
    FeatureDeviation,
    PatientBaseline,
    Severity,
)
from .baseline_builder import expected_for_hour

logger = logging.getLogger("traycer.scoring.anomaly")

F = BehavioralFeatureName


@dataclass(frozen=True)
class DirectionWeight:
    """Asymmetric weighting for one feature.

    `falling` applies when the observed value is below baseline, `rising` when
    it is above. They differ for most features. Mobility radius falling is the
    life space contraction that precedes depressive relapse; mobility radius
    rising is usually a holiday.
    """

    rising: float
    falling: float


@dataclass(frozen=True)
class CoefficientSet:
    name: str
    clinically_calibrated: bool
    # Relative contribution of each feature to the combined score.
    weights: Mapping[BehavioralFeatureName, float]
    # Direction asymmetry per feature.
    directions: Mapping[BehavioralFeatureName, DirectionWeight]
    # Maximum weighted deviation any single feature may contribute.
    saturation: float
    # Exponent of the power mean used to combine features.
    power: float
    # Logistic midpoint and steepness mapping combined deviation to 0 to 100.
    logistic_midpoint: float
    logistic_steepness: float
    # Score bands.
    elevated_threshold: float
    critical_threshold: float
    # Multiplier applied while the baseline is still provisional.
    provisional_shrinkage: float
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public placeholder coefficients
#
# NOT CLINICALLY CALIBRATED. Every weight is 1.0, every direction is
# symmetric, and the logistic parameters are round numbers. The detector runs
# and the numbers are internally consistent, but a score produced with this
# set carries no clinical meaning and must never drive an alert to a
# clinician. The response marks it, the dashboard marks it, and the health
# endpoint reports it.
# ---------------------------------------------------------------------------

_SYMMETRIC = DirectionWeight(rising=1.0, falling=1.0)

PLACEHOLDER_COEFFICIENTS = CoefficientSet(
    name="public-placeholder-v1",
    clinically_calibrated=False,
    weights={feature: 1.0 for feature in BehavioralFeatureName},
    directions={feature: _SYMMETRIC for feature in BehavioralFeatureName},
    saturation=6.0,
    power=2.0,
    logistic_midpoint=2.0,
    logistic_steepness=1.0,
    elevated_threshold=55.0,
    critical_threshold=75.0,
    provisional_shrinkage=0.6,
    notes=[
        "placeholder coefficients, not clinically calibrated",
        "scores produced with this set must not drive clinical decisions",
    ],
)


def load_coefficients() -> CoefficientSet:
    """Load the clinical coefficient set, or fall back to the placeholder.

    The private package `traycer_coefficients` is installed from a private
    index in the deployed image and is absent from the public repository. It
    exposes one function, `clinical_coefficients()`, returning the same
    CoefficientSet shape defined above. The shape is public so the interface
    is auditable; only the numbers are withheld.
    """
    try:
        from traycer_coefficients import clinical_coefficients  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "traycer_coefficients is not installed, scoring on placeholder coefficients"
        )
        return PLACEHOLDER_COEFFICIENTS

    coefficients = clinical_coefficients()
    logger.info("loaded clinical coefficient set %s", coefficients.name)
    return coefficients


@dataclass(frozen=True)
class AnomalyResult:
    risk_score: float
    severity: Severity
    deviations: list[FeatureDeviation]
    dominant_signal: BehavioralFeatureName | None
    coefficient_set: str
    clinically_calibrated: bool
    notes: list[str]


class AnomalyDetector:
    def __init__(self, coefficients: CoefficientSet | None = None) -> None:
        self._coefficients = coefficients or load_coefficients()

    @property
    def coefficient_name(self) -> str:
        return self._coefficients.name

    @property
    def clinically_calibrated(self) -> bool:
        return self._coefficients.clinically_calibrated

    def score(
        self,
        samples: Sequence[BehavioralSample],
        baseline: PatientBaseline,
        scored_at: datetime | None = None,
    ) -> AnomalyResult:
        scored_at = scored_at or datetime.now(timezone.utc)
        notes: list[str] = list(self._coefficients.notes)

        if baseline.quality is BaselineQuality.INSUFFICIENT:
            # Refusing to score is the correct answer here. A number produced
            # against an unbuilt baseline looks exactly like a real one on a
            # dashboard, and a clinician has no way to tell them apart.
            return AnomalyResult(
                risk_score=0.0,
                severity=Severity.STABLE,
                deviations=[],
                dominant_signal=None,
                coefficient_set=self._coefficients.name,
                clinically_calibrated=self._coefficients.clinically_calibrated,
                notes=notes + ["baseline is insufficient, no score produced"],
            )

        baseline_by_feature = {fb.feature: fb for fb in baseline.features}
        deviations = self._compute_deviations(samples, baseline_by_feature)

        if not deviations:
            return AnomalyResult(
                risk_score=0.0,
                severity=Severity.STABLE,
                deviations=[],
                dominant_signal=None,
                coefficient_set=self._coefficients.name,
                clinically_calibrated=self._coefficients.clinically_calibrated,
                notes=notes + ["no features in this window had a baseline to compare against"],
            )

        combined = self._combine(deviations)
        raw_score = self._to_score(combined)

        coverage = float(np.mean([d.confidence for d in deviations]))
        score = raw_score * _coverage_discount(coverage)

        if baseline.quality is BaselineQuality.PROVISIONAL:
            score *= self._coefficients.provisional_shrinkage
            notes.append("score reduced because the baseline is still provisional")

        score = float(np.clip(score, 0.0, 100.0))
        dominant = max(deviations, key=lambda d: d.weighted_deviation)

        return AnomalyResult(
            risk_score=round(score, 2),
            severity=self.severity_for(score),
            deviations=sorted(deviations, key=lambda d: d.weighted_deviation, reverse=True),
            dominant_signal=dominant.feature if dominant.weighted_deviation > 0 else None,
            coefficient_set=self._coefficients.name,
            clinically_calibrated=self._coefficients.clinically_calibrated,
            notes=notes,
        )

    def severity_for(self, score: float) -> Severity:
        if score >= self._coefficients.critical_threshold:
            return Severity.CRITICAL
        if score >= self._coefficients.elevated_threshold:
            return Severity.ELEVATED
        return Severity.STABLE

    # -- steps ------------------------------------------------------------

    def _compute_deviations(
        self,
        samples: Sequence[BehavioralSample],
        baselines: Mapping[BehavioralFeatureName, FeatureBaseline],
    ) -> list[FeatureDeviation]:
        grouped: dict[BehavioralFeatureName, list[BehavioralSample]] = defaultdict(list)
        for sample in samples:
            grouped[sample.feature].append(sample)

        deviations: list[FeatureDeviation] = []

        for feature, feature_samples in grouped.items():
            baseline = baselines.get(feature)
            if baseline is None:
                continue

            usable = [s for s in feature_samples if s.confidence > 0]
            if not usable:
                continue

            values = np.asarray([s.value for s in usable], dtype=float)
            weights = np.asarray([s.confidence for s in usable], dtype=float)
            observed = float(np.average(values, weights=weights))

            # Compare each observation against the expected value for its own
            # hour, then take the weighted mean of those residuals. Comparing
            # a daily aggregate against a daily median instead would lose the
            # circadian structure, which is where sleep disruption shows up.
            residuals = np.asarray(
                [s.value - expected_for_hour(baseline, s.observed_at.hour) for s in usable],
                dtype=float,
            )
            mean_residual = float(np.average(residuals, weights=weights))

            deviation_z = mean_residual / baseline.mad if baseline.mad > 0 else 0.0

            direction = self._coefficients.directions.get(feature, _SYMMETRIC)
            direction_weight = direction.rising if deviation_z >= 0 else direction.falling
            feature_weight = self._coefficients.weights.get(feature, 1.0)

            weighted = min(
                abs(deviation_z) * direction_weight * feature_weight,
                self._coefficients.saturation,
            )

            deviations.append(
                FeatureDeviation(
                    feature=feature,
                    observed=round(observed, 6),
                    baseline_median=baseline.median,
                    deviation_z=round(deviation_z, 4),
                    weighted_deviation=round(weighted, 4),
                    confidence=round(float(np.mean(weights)), 4),
                )
            )

        return deviations

    def _combine(self, deviations: Sequence[FeatureDeviation]) -> float:
        """Power mean over the weighted deviations.

        The exponent is what makes this different from an average. At p equal
        to 1 the combination is a mean, and a single severe signal gets diluted
        by every stable feature around it. As p rises the largest deviations
        dominate. Relapse usually shows up in two or three features before it
        shows up in ten, so the combination has to be sensitive to a narrow
        signal without ignoring a broad one entirely.
        """
        magnitudes = np.asarray([d.weighted_deviation for d in deviations], dtype=float)
        if magnitudes.size == 0:
            return 0.0

        power = self._coefficients.power
        if power <= 1.0:
            return float(np.mean(magnitudes))

        return float(np.power(np.mean(np.power(magnitudes, power)), 1.0 / power))

    def _to_score(self, combined: float) -> float:
        """Logistic map from combined deviation to a 0 to 100 score.

        Linear scaling was the first thing tried and it was wrong in both
        tails. Small deviations are noise and should stay near zero rather
        than crawling upward, and past a point more deviation does not mean
        more risk, it means the sensor broke.
        """
        midpoint = self._coefficients.logistic_midpoint
        steepness = self._coefficients.logistic_steepness
        exponent = -steepness * (combined - midpoint)
        # Guard against overflow for large negative deviations.
        exponent = float(np.clip(exponent, -50.0, 50.0))
        return 100.0 / (1.0 + float(np.exp(exponent)))


def _coverage_discount(coverage: float) -> float:
    """Shrink the score when the window was thinly sensed.

    A patient whose phone was off for most of the day produces a small number
    of low confidence samples. Those samples can still be extreme, and without
    this discount an empty day would fire an alert.
    """
    return float(np.clip(0.4 + 0.6 * coverage, 0.0, 1.0))
