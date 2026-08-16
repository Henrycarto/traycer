"""POST /v1/analyze.

Takes a short audio sample, returns an acoustic vector, destroys the audio.

The audio never touches persistent storage. It is written to a temporary file
because openSMILE reads from a path rather than a pipe, and that file is
overwritten and unlinked in a finally block before the response is built.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import uuid
import wave
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status

from ..engine.content_guard import ContentGuardViolation, destroy_sample
from ..engine.feature_extractor import build_vector
from ..engine.opensmile_client import (
    OpenSmileClient,
    OpenSmileError,
    OpenSmileTimeout,
    OpenSmileUnavailable,
)
from ..schemas import (
    AnalyzeRequest,
    ApiError,
    ApiMeta,
    ApiResponse,
    VoiceBiomarkerVector,
)
from ..security import DevicePrincipal, current_device

logger = logging.getLogger("traycer.voice.analyze")

router = APIRouter(prefix="/v1", tags=["analyze"])

MAX_SAMPLE_SECONDS = float(os.getenv("VOICE_MAX_SAMPLE_SECONDS", "30"))
MAX_UPLOAD_BYTES = int(os.getenv("VOICE_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))
READ_CHUNK_BYTES = 256 * 1024


def get_client() -> OpenSmileClient:
    return OpenSmileClient()


def _meta() -> ApiMeta:
    return ApiMeta(request_id=str(uuid.uuid4()))


def _fail(response: Response, code: str, message: str, http_status: int, **details: object):
    response.status_code = http_status
    return ApiResponse[VoiceBiomarkerVector](
        data=None,
        error=ApiError(code=code, message=message, details=dict(details) or None),
        meta=_meta(),
    )


@router.post("/analyze", response_model=ApiResponse[VoiceBiomarkerVector])
async def analyze(
    response: Response,
    audio: UploadFile = File(..., description="16 bit PCM WAV, mono, 16 kHz or higher"),
    patient_code: str = Form(...),
    recorded_at: datetime = Form(...),
    prompt_id: str = Form(...),
    device: DevicePrincipal = Depends(current_device),
    client: OpenSmileClient = Depends(get_client),
) -> ApiResponse[VoiceBiomarkerVector]:
    device.require_scope("voice:submit")

    try:
        request = AnalyzeRequest(
            patient_code=patient_code, recorded_at=recorded_at, prompt_id=prompt_id
        )
    except ValueError as exc:
        return _fail(response, "invalid_request", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)

    # A device may only submit for the patient its token was issued to.
    if request.patient_code != device.patient_code:
        logger.warning("device attempted a cross patient submission")
        return _fail(
            response,
            "patient_scope_mismatch",
            "the device token does not cover this patient code",
            status.HTTP_403_FORBIDDEN,
        )

    sample_path = await _persist_upload(audio)
    if sample_path is None:
        return _fail(
            response,
            "upload_too_large",
            f"audio sample exceeds {MAX_UPLOAD_BYTES} bytes",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    try:
        sample_seconds = _wav_duration_seconds(sample_path)
    except ValueError as exc:
        destroy_sample(sample_path)
        return _fail(
            response,
            "unsupported_audio",
            str(exc),
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    if sample_seconds > MAX_SAMPLE_SECONDS:
        destroy_sample(sample_path)
        return _fail(
            response,
            "sample_too_long",
            f"sample is {sample_seconds:.1f} seconds, limit is {MAX_SAMPLE_SECONDS:.0f}",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            sample_seconds=round(sample_seconds, 2),
        )

    try:
        extraction = await client.extract(sample_path)
    except OpenSmileUnavailable as exc:
        logger.error("extractor unavailable: %s", exc)
        return _fail(
            response,
            "extractor_unavailable",
            "the acoustic extractor is not available",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except OpenSmileTimeout:
        return _fail(
            response,
            "extractor_timeout",
            "acoustic extraction exceeded its time budget",
            status.HTTP_504_GATEWAY_TIMEOUT,
        )
    except ContentGuardViolation as exc:
        # The guard fired. Log the reason, never the payload that triggered it.
        logger.error("content guard rejected an extraction: %s", exc)
        return _fail(
            response,
            "content_guard_violation",
            "extraction was rejected by the content guard and the sample was destroyed",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except OpenSmileError as exc:
        logger.error("extraction failed: %s", exc)
        return _fail(
            response,
            "extraction_failed",
            "acoustic extraction failed",
            status.HTTP_502_BAD_GATEWAY,
        )

    vector = build_vector(
        extraction.values, sample_seconds=sample_seconds, guard_notes=extraction.notes
    )

    payload = VoiceBiomarkerVector(
        patient_code=request.patient_code,
        recorded_at=request.recorded_at,
        prompt_id=request.prompt_id,
        sample_seconds=round(sample_seconds, 3),
        quality=vector.quality,
        extractor=f"opensmile/{extraction.config_name}",
        features=vector.features,
        guard_notes=vector.notes,
    )

    logger.info(
        "voice sample analysed",
        extra={"quality": payload.quality.value, "features": len(payload.features)},
    )

    return ApiResponse[VoiceBiomarkerVector](data=payload, error=None, meta=_meta())


async def _persist_upload(audio: UploadFile) -> Path | None:
    """Stream the upload to a temporary file with a hard size cap.

    Streaming rather than reading into memory, because an eight megabyte cap
    per request multiplied by the worker count is enough to matter, and
    because a malformed content length header should not decide how much
    memory this process allocates.
    """
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed below
        prefix="traycer-sample-", suffix=".wav", delete=False
    )
    path = Path(handle.name)
    written = 0

    try:
        while True:
            chunk = await audio.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                handle.close()
                destroy_sample(path)
                return None
            handle.write(chunk)
    finally:
        with contextlib.suppress(OSError):
            handle.close()
        await audio.close()

    return path


def _wav_duration_seconds(path: Path) -> float:
    """Validate the container and return its duration.

    Only PCM WAV is accepted. Accepting compressed formats would mean running
    a decoder over patient supplied bytes, and a decoder is a much larger
    attack surface than a header parser.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
    except (wave.Error, EOFError, OSError) as exc:
        raise ValueError("audio must be a PCM WAV file") from exc

    if rate < 8000:
        raise ValueError(f"sample rate {rate} Hz is too low for acoustic analysis")
    if channels > 2:
        raise ValueError(f"{channels} channels is not supported, use mono or stereo")
    if width not in (1, 2, 3, 4):
        raise ValueError("unsupported PCM sample width")
    if frames == 0:
        raise ValueError("audio file contains no frames")

    return frames / float(rate)
