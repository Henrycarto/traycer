"""Enforces that no transcript is ever produced or stored.

Traycer Voice claims that it operates on acoustic signal alone. That claim is
worth exactly as much as the mechanism that enforces it, so this module is the
mechanism rather than a policy document.

Four checks, applied in order:

1. The openSMILE configuration is inspected before the process starts. A
   config that loads a component capable of producing symbolic output is
   refused, and the process never runs.
2. The argument vector handed to the subprocess is checked against a deny
   list, so a future contributor cannot add a transcription flag quietly.
3. Every field the extractor returns must parse as a finite number. A
   transcript is text. Text cannot survive this check.
4. The audio file is removed from disk before the response is written, and the
   removal is verified rather than assumed.

If any check fails the sample is destroyed and the request errors. There is no
degraded mode that keeps the audio around for inspection, because that mode is
how audio ends up on a disk that someone later subpoenas.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

logger = logging.getLogger("traycer.voice.content_guard")


class ContentGuardViolation(RuntimeError):
    """Raised when something in the pipeline could yield linguistic content."""


# openSMILE components that emit or consume symbolic content rather than
# acoustic descriptors. None of these belong in an acoustic only pipeline.
FORBIDDEN_COMPONENTS = (
    "cTranscriptSink",
    "cAsrSink",
    "cLibsvmLiveSink",
    "cSvmSink",
    "cWaveSink",
    "cWaveSinkCut",
    "cExternalAudioSource",
    "cPortaudioSink",
)

# Command line switches that would cause audio or symbolic output to be
# written somewhere the guard does not control.
FORBIDDEN_ARGUMENTS = (
    "-wavout",
    "-waveout",
    "-audioout",
    "-transcript",
    "-asr",
    "-lexicon",
    "-vocab",
    "-decoder",
)

# Field names that would indicate the extractor produced something other than
# an acoustic measurement.
FORBIDDEN_FIELD_PATTERN = re.compile(
    r"(transcript|token|word|phone(me)?|lexic|text|utterance_text|asr|decode)",
    re.IGNORECASE,
)

# openSMILE writes these bookkeeping columns. They are dropped rather than
# rejected, because they are not measurements and not content.
IGNORED_FIELDS = frozenset({"name", "frameTime", "frameIndex", "class", "numeric_class"})


@dataclass(frozen=True)
class GuardReport:
    accepted: dict[str, float]
    notes: list[str]


def assert_config_is_acoustic_only(config_path: Path) -> None:
    """Read the openSMILE config and refuse anything that is not descriptive.

    Runs before every extraction rather than once at startup. Configs are
    mounted into the container and can change without a restart.
    """
    if not config_path.exists():
        raise ContentGuardViolation(f"openSMILE config not found at {config_path}")

    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ContentGuardViolation(f"openSMILE config unreadable: {exc}") from exc

    for component in FORBIDDEN_COMPONENTS:
        if component in text:
            raise ContentGuardViolation(
                f"openSMILE config {config_path.name} references {component}, which can "
                "write audio or symbolic output. Refusing to run."
            )


AUDIO_SUFFIXES = (".wav", ".flac", ".mp3", ".ogg", ".m4a")


def assert_arguments_are_acoustic_only(argv: Iterable[str]) -> None:
    """Check the subprocess argument vector before it is executed."""
    args = [str(arg) for arg in argv]
    lowered = [arg.lower() for arg in args]

    for forbidden in FORBIDDEN_ARGUMENTS:
        if forbidden in lowered:
            raise ContentGuardViolation(
                f"openSMILE would be invoked with {forbidden}, which produces output "
                "this service is not permitted to create."
            )

    # Exactly one audio path may appear, and only as the input to -I. Any
    # other audio path is an attempt to write a copy of the sample somewhere.
    for index, arg in enumerate(lowered):
        if not arg.endswith(AUDIO_SUFFIXES):
            continue
        preceding = lowered[index - 1] if index > 0 else ""
        if preceding not in {"-i", "-inputfile"}:
            raise ContentGuardViolation(
                f"argument {args[index]!r} names an audio file outside the input flag, "
                "which would copy the patient sample."
            )

    logger.debug("argument vector passed the content guard")


def filter_numeric_output(row: Mapping[str, object]) -> GuardReport:
    """Keep finite numbers, drop bookkeeping, reject anything symbolic.

    A transcript reaching this function would fail on the field name check or
    on the float conversion. Both paths raise rather than dropping the field
    quietly, because a symbolic field appearing here means the pipeline
    changed shape and that is not a condition to paper over.
    """
    accepted: dict[str, float] = {}
    notes: list[str] = []

    for key, raw in row.items():
        name = str(key).strip().strip("'\"")

        if name in IGNORED_FIELDS:
            continue

        if FORBIDDEN_FIELD_PATTERN.search(name):
            raise ContentGuardViolation(
                f"extractor returned a field named {name!r}, which is not an acoustic "
                "measurement. The sample has been destroyed."
            )

        text = str(raw).strip().strip("'\"")
        if text in {"", "?", "nan", "NaN"}:
            notes.append(f"{name} was empty in the extractor output")
            continue

        try:
            value = float(text)
        except (TypeError, ValueError) as exc:
            raise ContentGuardViolation(
                f"field {name!r} did not parse as a number. Only numeric acoustic "
                "descriptors may leave this service."
            ) from exc

        if not math.isfinite(value):
            notes.append(f"{name} was not finite and was dropped")
            continue

        accepted[name] = value

    if not accepted:
        raise ContentGuardViolation("extractor produced no usable numeric fields")

    return GuardReport(accepted=accepted, notes=notes)


def destroy_sample(path: Path) -> None:
    """Remove the audio file and verify it is gone.

    Called from a finally block on every path through the analyzer, including
    the failure paths. Verification matters because a silent unlink failure on
    a read only mount would leave patient audio on disk while the logs claim
    otherwise.
    """
    try:
        if path.exists():
            # Overwrite before unlinking. On a container overlay filesystem
            # this does not guarantee the blocks are gone, but it removes the
            # trivially recoverable case of a file that was only unlinked.
            size = path.stat().st_size
            with path.open("r+b") as handle:
                handle.write(b"\x00" * min(size, 1024 * 1024))
                handle.flush()
                os.fsync(handle.fileno())
            path.unlink()
    except OSError as exc:
        logger.error("failed to destroy audio sample at %s: %s", path, exc)
        raise ContentGuardViolation(
            "audio sample could not be removed from disk"
        ) from exc

    if path.exists():
        raise ContentGuardViolation(f"audio sample still present at {path} after removal")

    logger.info("audio sample destroyed", extra={"sample_removed": True})


def assert_no_transcript_produced(artifacts: Iterable[Path]) -> None:
    """Confirm the extractor left nothing behind except its numeric output.

    openSMILE writes only the output file it is told to write. This checks
    that assumption instead of trusting it, which is the difference between a
    claim and a control.
    """
    for artifact in artifacts:
        if artifact.suffix.lower() in {".txt", ".json", ".srt", ".vtt", ".lab", ".trs"}:
            raise ContentGuardViolation(
                f"unexpected artifact {artifact.name} suggests symbolic output was written"
            )
