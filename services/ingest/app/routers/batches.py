"""POST /v1/batches, the endpoint the SDK uploads to.

Every status code returned here is chosen against how the SDK's upload queue
reacts to it, in `packages/sdk/src/collectors/upload_queue.ts`:

  2xx            batch removed from the device queue
  409            also removed, because a duplicate means the server already
                 has it and a response was lost
  401, 403       kept and retried after the host app refreshes the token
  4xx otherwise  dropped permanently, because retrying reproduces the failure
  408, 429, 5xx  kept and retried with backoff

So the mapping is not cosmetic. Returning 422 for a transient database fault
would tell every device in the field to delete data that was never stored, and
returning 503 for a malformed payload would make a device retry the same bad
batch until it exhausts its attempts.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Response, status

from ..crypto import DecryptionError, KeyRing, UnknownKeyError, decrypt_envelope
from ..engine.validator import validate_payload
from ..schemas import (
    ApiError,
    ApiMeta,
    ApiResponse,
    EncryptedEnvelope,
    IngestResult,
    UploadPayload,
)
from ..security import DevicePrincipal, current_device
from ..store import BatchReceipt, IngestStore, PatientNotEnrolled

logger = logging.getLogger("traycer.ingest.batches")

router = APIRouter(prefix="/v1", tags=["ingest"])

_keyring = KeyRing()
_store: IngestStore | None = None


def configure(keyring: KeyRing, store: IngestStore) -> None:
    global _keyring, _store
    _keyring = keyring
    _store = store


def get_keyring() -> KeyRing:
    return _keyring


def get_store() -> IngestStore:
    if _store is None:
        raise RuntimeError("ingest store was never configured")
    return _store


def _meta() -> ApiMeta:
    return ApiMeta(request_id=str(uuid.uuid4()))


def _fail(response: Response, code: str, message: str, http_status: int, **details: object):
    response.status_code = http_status
    return ApiResponse[IngestResult](
        data=None,
        error=ApiError(code=code, message=message, details=dict(details) or None),
        meta=_meta(),
    )


@router.post("/batches", response_model=ApiResponse[IngestResult])
async def receive_batch(
    envelope: EncryptedEnvelope,
    response: Response,
    device: DevicePrincipal = Depends(current_device),
    keyring: KeyRing = Depends(get_keyring),
    store: IngestStore = Depends(get_store),
    x_traycer_batch_id: str | None = Header(default=None),
) -> ApiResponse[IngestResult]:
    device.require_scope("ingest:write")

    # The header is advisory. The body is authoritative, because the body's
    # batch id is authenticated as additional data and the header is not.
    if x_traycer_batch_id and x_traycer_batch_id != envelope.batchId:
        return _fail(
            response,
            "batch_id_mismatch",
            "the batch id header does not match the envelope",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    # Deduplicate before decrypting. A retried upload after a lost response is
    # the common case, and it should cost a single indexed lookup rather than
    # an RSA operation.
    try:
        if await store.already_received(envelope.batchId):
            logger.info("duplicate batch received", extra={"batch_id": envelope.batchId})
            response.status_code = status.HTTP_409_CONFLICT
            return ApiResponse[IngestResult](
                data=IngestResult(
                    batch_id=envelope.batchId,
                    accepted_windows=0,
                    accepted_samples=0,
                    rejected_samples=0,
                    duplicate=True,
                    notes=["batch was already stored"],
                ),
                error=None,
                meta=_meta(),
            )
    except Exception:
        logger.exception("deduplication lookup failed")
        return _fail(
            response,
            "store_unavailable",
            "the deduplication store is unavailable",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        plaintext = decrypt_envelope(envelope, keyring)
    except UnknownKeyError:
        # Almost always a secret that was not mounted rather than a bad
        # payload, so the device keeps the batch and retries.
        logger.error("no private key for key id %s", envelope.keyId)
        return _fail(
            response,
            "unknown_key",
            "this server holds no key for the given key id",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except DecryptionError as exc:
        # Retrying reproduces the same ciphertext, so the batch is dropped.
        logger.warning("decryption failed: %s", exc)
        return _fail(
            response,
            "decryption_failed",
            "the envelope could not be decrypted",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    try:
        payload = UploadPayload.model_validate_json(plaintext)
    except ValueError as exc:
        logger.warning("payload failed schema validation")
        return _fail(
            response,
            "invalid_payload",
            "the decrypted payload does not match the expected schema",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            errors=str(exc)[:500],
        )

    if payload.batchId != envelope.batchId:
        return _fail(
            response,
            "batch_id_mismatch",
            "the payload batch id does not match the envelope",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    # A device may only submit for the patient its token was issued to.
    if payload.patientCode != device.patient_code:
        logger.warning("device attempted a cross patient upload")
        return _fail(
            response,
            "patient_scope_mismatch",
            "the device token does not cover this patient code",
            status.HTTP_403_FORBIDDEN,
        )

    now = datetime.now(timezone.utc)
    outcome = validate_payload(payload, now_ms=int(now.timestamp() * 1000))

    if not outcome.samples:
        # Nothing storable. The batch is acknowledged rather than retried,
        # because the same payload will fail the same way forever. 202 like
        # every other accepted response, so a client never has to distinguish
        # between two success codes.
        logger.info("batch contained no storable samples")
        response.status_code = status.HTTP_202_ACCEPTED
        return ApiResponse[IngestResult](
            data=IngestResult(
                batch_id=envelope.batchId,
                accepted_windows=0,
                accepted_samples=0,
                rejected_samples=outcome.rejected_samples,
                notes=outcome.notes[:20],
            ),
            error=None,
            meta=_meta(),
        )

    try:
        patient_id = await store.resolve_patient(payload.patientCode)
    except PatientNotEnrolled:
        logger.error("no patient row for a validly scoped token")
        return _fail(
            response,
            "patient_not_provisioned",
            "the patient is not provisioned yet, retry later",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except Exception:
        logger.exception("patient lookup failed")
        return _fail(
            response,
            "store_unavailable",
            "the patient store is unavailable",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # Samples first, receipt second. See the note at the top of store.py: the
    # reverse order can acknowledge a batch whose samples were never written.
    try:
        written = await store.write_samples(patient_id, outcome.samples)
    except Exception:
        logger.exception("sample write failed")
        return _fail(
            response,
            "store_unavailable",
            "the metric store is unavailable",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        await store.record_batch(
            BatchReceipt(
                batch_id=envelope.batchId,
                patient_code=payload.patientCode,
                received_at=now,
                window_count=outcome.accepted_windows,
                sample_count=written,
            ),
            patient_id,
        )
        await store.touch_device(patient_id, payload.sdkVersion, now)
    except Exception:
        # The samples are already stored. Asking for a retry is safe because
        # the insert is idempotent, and it is better than acknowledging a
        # batch whose receipt is missing.
        logger.exception("batch receipt write failed after samples were stored")
        return _fail(
            response,
            "store_unavailable",
            "the batch receipt could not be recorded",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    logger.info(
        "batch stored",
        extra={
            "windows": outcome.accepted_windows,
            "samples": written,
            "rejected": outcome.rejected_samples,
        },
    )

    response.status_code = status.HTTP_202_ACCEPTED
    return ApiResponse[IngestResult](
        data=IngestResult(
            batch_id=envelope.batchId,
            accepted_windows=outcome.accepted_windows,
            accepted_samples=written,
            rejected_samples=outcome.rejected_samples,
            notes=outcome.notes[:20],
        ),
        error=None,
        meta=_meta(),
    )
