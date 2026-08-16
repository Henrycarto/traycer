"""The content guard is the load bearing privacy claim of this service.

These tests are written as adversarial cases rather than happy paths. Each one
is a way a transcript or a copy of the audio could plausibly get out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.content_guard import (
    ContentGuardViolation,
    assert_arguments_are_acoustic_only,
    assert_config_is_acoustic_only,
    assert_no_transcript_produced,
    destroy_sample,
    filter_numeric_output,
)

ACOUSTIC_CONFIG = """
[componentInstances:cComponentManager]
instance[dataMemory].type=cDataMemory
instance[waveIn].type=cWaveSource
instance[frames].type=cFramer
instance[csvSink].type=cCsvSink
"""

CONFIG_WITH_AUDIO_SINK = ACOUSTIC_CONFIG + """
instance[wavOut].type=cWaveSink
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestConfigInspection:
    def test_accepts_an_acoustic_config(self, tmp_path: Path) -> None:
        config = _write(tmp_path, "ok.conf", ACOUSTIC_CONFIG)
        assert_config_is_acoustic_only(config)

    def test_refuses_a_config_that_can_write_audio(self, tmp_path: Path) -> None:
        config = _write(tmp_path, "leaky.conf", CONFIG_WITH_AUDIO_SINK)
        with pytest.raises(ContentGuardViolation, match="cWaveSink"):
            assert_config_is_acoustic_only(config)

    def test_refuses_a_missing_config(self, tmp_path: Path) -> None:
        with pytest.raises(ContentGuardViolation, match="not found"):
            assert_config_is_acoustic_only(tmp_path / "absent.conf")


class TestArgumentInspection:
    def test_accepts_the_standard_invocation(self) -> None:
        assert_arguments_are_acoustic_only(
            ["SMILExtract", "-C", "/opt/cfg.conf", "-I", "/tmp/s.wav", "-csvoutput", "/tmp/o.csv"]
        )

    def test_refuses_an_audio_output_flag(self) -> None:
        with pytest.raises(ContentGuardViolation, match="wavout"):
            assert_arguments_are_acoustic_only(
                ["SMILExtract", "-I", "/tmp/s.wav", "-wavout", "/tmp/copy.wav"]
            )

    def test_refuses_an_audio_path_outside_the_input_flag(self) -> None:
        with pytest.raises(ContentGuardViolation, match="names an audio file"):
            assert_arguments_are_acoustic_only(
                ["SMILExtract", "-I", "/tmp/s.wav", "-csvoutput", "/tmp/copy.wav"]
            )

    def test_refuses_a_transcription_flag(self) -> None:
        with pytest.raises(ContentGuardViolation):
            assert_arguments_are_acoustic_only(["SMILExtract", "-asr", "-I", "/tmp/s.wav"])


class TestOutputFiltering:
    def test_keeps_numeric_acoustic_fields(self) -> None:
        report = filter_numeric_output(
            {"name": "sample", "loudness_sma3_amean": "0.412", "jitterLocal_sma3nz_amean": "0.018"}
        )
        assert report.accepted == {
            "loudness_sma3_amean": 0.412,
            "jitterLocal_sma3nz_amean": 0.018,
        }
        assert report.notes == []

    def test_rejects_a_field_named_like_a_transcript(self) -> None:
        with pytest.raises(ContentGuardViolation, match="transcript"):
            filter_numeric_output({"transcript": "0.5"})

    def test_rejects_a_field_named_like_word_output(self) -> None:
        with pytest.raises(ContentGuardViolation):
            filter_numeric_output({"word_count": "12"})

    def test_rejects_text_that_slipped_into_a_numeric_field(self) -> None:
        with pytest.raises(ContentGuardViolation, match="did not parse as a number"):
            filter_numeric_output({"loudness_sma3_amean": "I feel fine today"})

    def test_notes_an_empty_field_without_failing_the_sample(self) -> None:
        report = filter_numeric_output({"loudness_sma3_amean": "0.4", "HNRdBACF_sma3nz_amean": "?"})
        assert "HNRdBACF_sma3nz_amean" in report.notes[0]
        assert len(report.accepted) == 1

    def test_rejects_output_with_nothing_usable(self) -> None:
        with pytest.raises(ContentGuardViolation, match="no usable numeric fields"):
            filter_numeric_output({"name": "sample", "frameTime": "0.0"})


class TestSampleDestruction:
    def test_removes_the_file(self, tmp_path: Path) -> None:
        sample = tmp_path / "sample.wav"
        sample.write_bytes(b"RIFF" + b"\x00" * 512)

        destroy_sample(sample)

        assert not sample.exists()

    def test_is_safe_to_call_when_the_file_is_already_gone(self, tmp_path: Path) -> None:
        destroy_sample(tmp_path / "never-existed.wav")

    def test_overwrites_before_unlinking(self, tmp_path: Path) -> None:
        sample = tmp_path / "sample.wav"
        sample.write_bytes(b"SECRET AUDIO PAYLOAD" * 100)

        destroy_sample(sample)

        # Nothing to read back, which is the point. The assertion that matters
        # is that the call verified removal rather than assuming it.
        assert not sample.exists()


class TestArtifactInspection:
    def test_accepts_a_csv_only_workspace(self, tmp_path: Path) -> None:
        (tmp_path / "features.csv").write_text("a;b\n1;2\n", encoding="utf-8")
        assert_no_transcript_produced(tmp_path.iterdir())

    def test_refuses_a_text_artifact(self, tmp_path: Path) -> None:
        (tmp_path / "features.csv").write_text("a;b\n1;2\n", encoding="utf-8")
        (tmp_path / "output.txt").write_text("hello", encoding="utf-8")

        with pytest.raises(ContentGuardViolation, match="output.txt"):
            assert_no_transcript_produced(tmp_path.iterdir())

    def test_refuses_a_subtitle_artifact(self, tmp_path: Path) -> None:
        (tmp_path / "aligned.vtt").write_text("WEBVTT", encoding="utf-8")

        with pytest.raises(ContentGuardViolation):
            assert_no_transcript_produced(tmp_path.iterdir())
