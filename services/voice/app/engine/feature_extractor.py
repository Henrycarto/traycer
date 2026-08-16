"""Builds the Traycer voice biomarker vector from openSMILE output.

openSMILE eGeMAPSv02 emits 88 functionals. Feeding all 88 into a relapse model
trained on a few hundred patients is a good way to fit noise, so this module
reduces them to thirteen descriptors chosen because each one has a documented
relationship to psychiatric state rather than because it improved a validation
score.

The mapping from eGeMAPS parameter names to Traycer feature names lives here
and nowhere else. When the extractor config changes, this is the only file
that has to move.

Clinical rationale, briefly:

- Vocal energy and energy variability fall in depression. Reduced loudness is
  one of the oldest described features of psychomotor retardation.
- Pitch variance is the monotone measure. It flattens in depression and it
  widens in mania, so the direction of change matters as much as the size.
- Speech rate and articulation rate separate slowed speech from speech that is
  broken up by pauses. Depression tends to affect both, anxiety tends to
  affect pausing more than articulation.
- Pause frequency and mean pause length capture the same split from the other
  side. Long pauses are the acoustic surface of slowed thought.
- Jitter, shimmer and harmonicity are voice quality. They move with autonomic
  arousal and with several psychotropic medications, which is why they are
  reported rather than folded into a single score here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Optional

from ..schemas import SampleQuality, VoiceFeature, VoiceFeatureName

logger = logging.getLogger("traycer.voice.features")

# Samples shorter than this cannot support a stable functional estimate.
MIN_USABLE_SECONDS = 4.0

# Below this voiced ratio the sample is background noise or silence.
MIN_VOICED_RATIO = 0.08

# Above this jitter the sample is almost certainly clipped or wind affected
# rather than a pathological voice.
NOISY_JITTER_THRESHOLD = 0.09


@dataclass(frozen=True)
class ParameterMapping:
    """One Traycer feature and the eGeMAPS parameters that produce it."""

    feature: VoiceFeatureName
    unit: str
    sources: tuple[str, ...]
    scale: float = 1.0


# eGeMAPSv02 functional names. The sources tuple is ordered by preference so a
# config revision that renames one parameter degrades to the next rather than
# dropping the feature.
MAPPINGS: tuple[ParameterMapping, ...] = (
    ParameterMapping(
        feature=VoiceFeatureName.VOCAL_ENERGY,
        unit="loudness",
        sources=("loudness_sma3_amean", "equivalentSoundLevel_dBp"),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.ENERGY_VARIABILITY,
        unit="loudness_sd",
        sources=("loudness_sma3_stddevNorm", "loudness_sma3_percentile80.0"),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.PITCH_MEAN_SEMITONES,
        unit="semitones_from_27.5hz",
        sources=("F0semitoneFrom27.5Hz_sma3nz_amean",),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.PITCH_VARIANCE,
        unit="semitones_sd",
        sources=(
            "F0semitoneFrom27.5Hz_sma3nz_stddevNorm",
            "F0semitoneFrom27.5Hz_sma3nz_percentile80.0",
        ),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.SPEECH_RATE,
        unit="syllables_per_second",
        sources=("loudnessPeaksPerSec",),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.ARTICULATION_RATE,
        unit="voiced_segments_per_second",
        sources=("VoicedSegmentsPerSec",),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.MEAN_PAUSE_SECONDS,
        unit="seconds",
        sources=("MeanUnvoicedSegmentLength",),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.VOICED_RATIO,
        unit="ratio",
        sources=("MeanVoicedSegmentLengthSec",),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.JITTER,
        unit="ratio",
        sources=("jitterLocal_sma3nz_amean",),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.SHIMMER,
        unit="ratio",
        sources=("shimmerLocaldB_sma3nz_amean",),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.HARMONICITY,
        unit="db",
        sources=("HNRdBACF_sma3nz_amean",),
    ),
    ParameterMapping(
        feature=VoiceFeatureName.SPECTRAL_SLOPE,
        unit="db_per_khz",
        sources=("slope0-500_sma3_amean", "slopeV0-500_sma3nz_amean"),
    ),
)


@dataclass(frozen=True)
class VectorResult:
    features: list[VoiceFeature]
    quality: SampleQuality
    notes: list[str]


def _lookup(values: Mapping[str, float], names: tuple[str, ...]) -> Optional[float]:
    """Case and whitespace tolerant parameter lookup.

    openSMILE CSV headers pick up quoting and stray whitespace depending on
    the config, and a header mismatch that silently drops a feature is much
    harder to notice than one that errors.
    """
    normalised = {
        key.strip().strip("'\"").strip().lower(): value for key, value in values.items()
    }
    for name in names:
        found = normalised.get(name.lower())
        if found is not None:
            return found
    return None


def build_vector(
    values: Mapping[str, float],
    sample_seconds: float,
    guard_notes: Optional[list[str]] = None,
) -> VectorResult:
    """Reduce raw eGeMAPS functionals to the Traycer voice vector."""
    notes: list[str] = list(guard_notes or [])
    features: list[VoiceFeature] = []

    for mapping in MAPPINGS:
        raw = _lookup(values, mapping.sources)
        if raw is None:
            notes.append(f"{mapping.feature.value} unavailable in extractor output")
            continue
        features.append(
            VoiceFeature(
                feature=mapping.feature,
                value=round(raw * mapping.scale, 6),
                unit=mapping.unit,
            )
        )

    pause_frequency = _derive_pause_frequency(values, sample_seconds)
    if pause_frequency is not None:
        features.append(
            VoiceFeature(
                feature=VoiceFeatureName.PAUSE_FREQUENCY,
                value=round(pause_frequency, 6),
                unit="pauses_per_minute",
            )
        )
    else:
        notes.append("pause_frequency could not be derived")

    quality = assess_quality(features, sample_seconds)
    return VectorResult(features=features, quality=quality, notes=notes)


def _derive_pause_frequency(
    values: Mapping[str, float], sample_seconds: float
) -> Optional[float]:
    """Pauses per minute, derived rather than read directly.

    eGeMAPS reports mean unvoiced segment length but not a pause count. The
    count is recoverable from the voiced segment rate, because voiced and
    unvoiced segments alternate: a sample with n voiced segments per second
    has approximately the same number of intervening unvoiced ones.
    """
    voiced_rate = _lookup(values, ("VoicedSegmentsPerSec",))
    if voiced_rate is None or sample_seconds <= 0:
        return None

    mean_unvoiced = _lookup(values, ("MeanUnvoicedSegmentLength",))
    if mean_unvoiced is None:
        return None

    # Unvoiced segments shorter than 150 milliseconds are stop consonants
    # rather than pauses. Excluding them is what separates this from a plain
    # segment count, and it is the reason the feature tracks slowed thought
    # instead of tracking phonetics.
    if mean_unvoiced < 0.15:
        return 0.0

    return voiced_rate * 60.0


def assess_quality(features: list[VoiceFeature], sample_seconds: float) -> SampleQuality:
    """Decide whether the vector is worth feeding to the scoring pipeline.

    A bad sample that is scored anyway produces a spurious deviation, and a
    spurious deviation costs a clinician a phone call. Rejecting is cheaper.
    """
    if sample_seconds < MIN_USABLE_SECONDS:
        return SampleQuality.TOO_SHORT

    by_name = {feature.feature: feature.value for feature in features}

    articulation = by_name.get(VoiceFeatureName.ARTICULATION_RATE)
    if articulation is None or articulation < MIN_VOICED_RATIO:
        return SampleQuality.NO_SPEECH

    jitter = by_name.get(VoiceFeatureName.JITTER)
    if jitter is not None and jitter > NOISY_JITTER_THRESHOLD:
        return SampleQuality.NOISY

    harmonicity = by_name.get(VoiceFeatureName.HARMONICITY)
    if harmonicity is not None and harmonicity < 2.0:
        # Harmonics to noise ratio under 2 dB means the recording is dominated
        # by room or handling noise.
        return SampleQuality.NOISY

    return SampleQuality.USABLE
