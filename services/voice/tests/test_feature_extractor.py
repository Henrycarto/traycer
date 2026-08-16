from __future__ import annotations

from app.engine.feature_extractor import build_vector
from app.schemas import SampleQuality, VoiceFeatureName

# A realistic eGeMAPSv02 functional row for a neutral read passage.
EGEMAPS_ROW = {
    "loudness_sma3_amean": 0.61,
    "loudness_sma3_stddevNorm": 0.74,
    "F0semitoneFrom27.5Hz_sma3nz_amean": 28.4,
    "F0semitoneFrom27.5Hz_sma3nz_stddevNorm": 0.18,
    "loudnessPeaksPerSec": 3.9,
    "VoicedSegmentsPerSec": 2.7,
    "MeanUnvoicedSegmentLength": 0.31,
    "MeanVoicedSegmentLengthSec": 0.22,
    "jitterLocal_sma3nz_amean": 0.021,
    "shimmerLocaldB_sma3nz_amean": 1.14,
    "HNRdBACF_sma3nz_amean": 9.8,
    "slope0-500_sma3_amean": -0.032,
}


def values(result) -> dict[str, float]:
    return {feature.feature: feature.value for feature in result.features}


class TestVectorConstruction:
    def test_builds_the_full_vector_from_a_complete_row(self) -> None:
        result = build_vector(EGEMAPS_ROW, sample_seconds=18.0)

        by_name = values(result)
        assert by_name[VoiceFeatureName.VOCAL_ENERGY] == 0.61
        assert by_name[VoiceFeatureName.PITCH_VARIANCE] == 0.18
        assert by_name[VoiceFeatureName.SPEECH_RATE] == 3.9
        assert by_name[VoiceFeatureName.ARTICULATION_RATE] == 2.7
        assert result.quality is SampleQuality.USABLE
        assert result.notes == []

    def test_every_returned_value_is_numeric(self) -> None:
        result = build_vector(EGEMAPS_ROW, sample_seconds=18.0)
        assert all(isinstance(feature.value, float) for feature in result.features)

    def test_tolerates_quoted_and_padded_headers(self) -> None:
        noisy_headers = {f"'{key} '": value for key, value in EGEMAPS_ROW.items()}
        result = build_vector(noisy_headers, sample_seconds=18.0)

        assert values(result)[VoiceFeatureName.VOCAL_ENERGY] == 0.61

    def test_notes_a_missing_parameter_instead_of_failing(self) -> None:
        partial = {k: v for k, v in EGEMAPS_ROW.items() if k != "HNRdBACF_sma3nz_amean"}
        result = build_vector(partial, sample_seconds=18.0)

        assert any("harmonicity" in note for note in result.notes)
        assert VoiceFeatureName.HARMONICITY not in values(result)

    def test_falls_back_to_an_alternate_parameter_name(self) -> None:
        renamed = {k: v for k, v in EGEMAPS_ROW.items() if k != "slope0-500_sma3_amean"}
        renamed["slopeV0-500_sma3nz_amean"] = -0.05

        result = build_vector(renamed, sample_seconds=18.0)
        assert values(result)[VoiceFeatureName.SPECTRAL_SLOPE] == -0.05

    def test_carries_guard_notes_through(self) -> None:
        result = build_vector(EGEMAPS_ROW, sample_seconds=18.0, guard_notes=["field x was empty"])
        assert "field x was empty" in result.notes


class TestPauseFrequency:
    def test_derives_pauses_per_minute(self) -> None:
        result = build_vector(EGEMAPS_ROW, sample_seconds=18.0)
        assert values(result)[VoiceFeatureName.PAUSE_FREQUENCY] == 162.0

    def test_reports_zero_when_unvoiced_gaps_are_only_consonants(self) -> None:
        row = dict(EGEMAPS_ROW, MeanUnvoicedSegmentLength=0.06)
        result = build_vector(row, sample_seconds=18.0)

        assert values(result)[VoiceFeatureName.PAUSE_FREQUENCY] == 0.0


class TestQualityGating:
    def test_rejects_a_sample_that_is_too_short(self) -> None:
        result = build_vector(EGEMAPS_ROW, sample_seconds=2.0)
        assert result.quality is SampleQuality.TOO_SHORT

    def test_flags_silence_as_no_speech(self) -> None:
        row = dict(EGEMAPS_ROW, VoicedSegmentsPerSec=0.01)
        result = build_vector(row, sample_seconds=18.0)

        assert result.quality is SampleQuality.NO_SPEECH

    def test_flags_a_clipped_recording_as_noisy(self) -> None:
        row = dict(EGEMAPS_ROW, jitterLocal_sma3nz_amean=0.2)
        result = build_vector(row, sample_seconds=18.0)

        assert result.quality is SampleQuality.NOISY

    def test_flags_a_room_noise_recording_as_noisy(self) -> None:
        row = dict(EGEMAPS_ROW, HNRdBACF_sma3nz_amean=0.6)
        result = build_vector(row, sample_seconds=18.0)

        assert result.quality is SampleQuality.NOISY


class TestDepressionSignature:
    """The vector has to move in the documented direction, not just exist."""

    def test_a_flattened_retarded_sample_reads_lower_across_the_expected_features(self) -> None:
        depressed = dict(EGEMAPS_ROW)
        depressed["loudness_sma3_amean"] = 0.28
        depressed["F0semitoneFrom27.5Hz_sma3nz_stddevNorm"] = 0.07
        depressed["loudnessPeaksPerSec"] = 2.1
        depressed["VoicedSegmentsPerSec"] = 1.4
        depressed["MeanUnvoicedSegmentLength"] = 0.68

        baseline = build_vector(EGEMAPS_ROW, sample_seconds=18.0)
        current = build_vector(depressed, sample_seconds=18.0)

        base = values(baseline)
        now = values(current)

        assert now[VoiceFeatureName.VOCAL_ENERGY] < base[VoiceFeatureName.VOCAL_ENERGY]
        assert now[VoiceFeatureName.PITCH_VARIANCE] < base[VoiceFeatureName.PITCH_VARIANCE]
        assert now[VoiceFeatureName.SPEECH_RATE] < base[VoiceFeatureName.SPEECH_RATE]
        assert now[VoiceFeatureName.MEAN_PAUSE_SECONDS] > base[VoiceFeatureName.MEAN_PAUSE_SECONDS]
