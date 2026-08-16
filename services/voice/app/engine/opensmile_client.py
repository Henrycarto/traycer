"""Subprocess wrapper around the openSMILE SMILExtract binary.

openSMILE runs as a child process rather than through a Python binding for two
reasons. The binary is the reference implementation that the acoustic
literature is built on, so results stay comparable to published work. And a
child process is a containment boundary: it gets a read only config, one input
file, one output file, a wall clock limit and a memory limit, and it cannot
reach the database or the network.

The wrapper is async because the rest of the service is. Blocking a worker for
the two to five seconds an extraction takes would cap throughput at one
concurrent sample per worker.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .content_guard import (
    ContentGuardViolation,
    assert_arguments_are_acoustic_only,
    assert_config_is_acoustic_only,
    assert_no_transcript_produced,
    destroy_sample,
    filter_numeric_output,
)

try:  # Linux and macOS only. The service ships in a Linux container.
    import resource
except ImportError:  # pragma: no cover - developer machines on Windows
    resource = None  # type: ignore[assignment]

logger = logging.getLogger("traycer.voice.opensmile")

DEFAULT_BINARY = os.getenv("OPENSMILE_BINARY", "SMILExtract")
DEFAULT_CONFIG = os.getenv(
    "OPENSMILE_CONFIG", "/opt/opensmile/config/egemaps/v02/eGeMAPSv02.conf"
)

# An eGeMAPS extraction over a 30 second sample finishes well inside a second
# on modern hardware. Anything past this is a hung process, not slow work.
EXTRACTION_TIMEOUT_SECONDS = float(os.getenv("OPENSMILE_TIMEOUT_SECONDS", "20"))

# Address space cap for the child. eGeMAPS functionals over a short sample use
# a few tens of megabytes, so this is generous and still bounds a runaway.
CHILD_MEMORY_LIMIT_BYTES = int(os.getenv("OPENSMILE_MEMORY_LIMIT_MB", "512")) * 1024 * 1024


class OpenSmileError(RuntimeError):
    """The extractor failed in a way the caller cannot recover from."""


class OpenSmileUnavailable(OpenSmileError):
    """The binary is missing or not executable."""


class OpenSmileTimeout(OpenSmileError):
    """The extractor exceeded its wall clock budget and was killed."""


@dataclass(frozen=True)
class ExtractionResult:
    """Raw numeric output from one extraction, already guard filtered."""

    values: dict[str, float]
    notes: list[str]
    config_name: str


class OpenSmileClient:
    def __init__(
        self,
        binary: str = DEFAULT_BINARY,
        config_path: str = DEFAULT_CONFIG,
        timeout_seconds: float = EXTRACTION_TIMEOUT_SECONDS,
    ) -> None:
        self._binary = binary
        self._config_path = Path(config_path)
        self._timeout = timeout_seconds

    @property
    def config_name(self) -> str:
        return self._config_path.name

    def is_available(self) -> bool:
        return shutil.which(self._binary) is not None or Path(self._binary).is_file()

    async def version(self) -> Optional[str]:
        """Read the binary's own version string, or None when it is absent."""
        if not self.is_available():
            return None

        try:
            process = await asyncio.create_subprocess_exec(
                self._binary,
                "-h",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, asyncio.TimeoutError):
            return None

        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if "openSMILE version" in line:
                return line.strip()
        return None

    async def extract(self, audio_path: Path) -> ExtractionResult:
        """Run one extraction and return guard filtered numeric features.

        The audio file is destroyed before this method returns on every path,
        including the failure paths. That ordering is the reason the finally
        block is here rather than in the router.
        """
        if not self.is_available():
            destroy_sample(audio_path)
            raise OpenSmileUnavailable(
                f"SMILExtract not found at {self._binary}. The voice service cannot "
                "extract acoustic features without it."
            )

        assert_config_is_acoustic_only(self._config_path)

        work_dir = Path(tempfile.mkdtemp(prefix="traycer-voice-"))
        output_path = work_dir / "features.csv"

        argv = [
            self._binary,
            "-C",
            str(self._config_path),
            "-I",
            str(audio_path),
            "-csvoutput",
            str(output_path),
            # Functionals over the whole sample, one row out. Frame level
            # descriptors are not requested because a frame series is closer
            # to the signal than this service is permitted to retain.
            "-instname",
            "sample",
            "-loglevel",
            "1",
            "-nologfile",
            "1",
            "-appendcsv",
            "0",
        ]

        assert_arguments_are_acoustic_only(argv)

        try:
            await self._run(argv)
            assert_no_transcript_produced(work_dir.iterdir())
            row = self._read_single_row(output_path)
            report = filter_numeric_output(row)
            return ExtractionResult(
                values=report.accepted,
                notes=report.notes,
                config_name=self.config_name,
            )
        finally:
            destroy_sample(audio_path)
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _run(self, argv: list[str]) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=_limit_child if resource is not None else None,
            )
        except OSError as exc:
            raise OpenSmileUnavailable(f"could not start SMILExtract: {exc}") from exc

        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise OpenSmileTimeout(
                f"SMILExtract exceeded {self._timeout} seconds and was killed"
            ) from None

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
            raise OpenSmileError(
                f"SMILExtract exited with code {process.returncode}: {detail}"
            )

    @staticmethod
    def _read_single_row(output_path: Path) -> dict[str, str]:
        if not output_path.exists():
            raise OpenSmileError("SMILExtract produced no output file")

        with output_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            # openSMILE writes semicolon separated CSV by default.
            reader = csv.DictReader(handle, delimiter=";")
            rows = list(reader)

        if not rows:
            raise OpenSmileError("SMILExtract output contained no rows")
        if len(rows) > 1:
            # More than one row means frame level output slipped through the
            # config, which is a larger retention surface than intended.
            raise ContentGuardViolation(
                "extractor returned frame level output where functionals were expected"
            )

        return {str(k): str(v) for k, v in rows[0].items() if k is not None}


def _limit_child() -> None:
    """Applied in the child between fork and exec.

    Not available on Windows, which is why the service targets Linux
    containers only. The limits bound a malformed audio file that sends the
    extractor into a large allocation.
    """
    assert resource is not None
    resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY_LIMIT_BYTES, CHILD_MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
