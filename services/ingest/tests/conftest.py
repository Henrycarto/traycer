"""Test fixtures for ingest.

`seal_envelope` reproduces what the SDK does in
`packages/sdk/src/internal/crypto.ts`, step for step: a fresh AES-256-GCM key
per batch, a 96 bit nonce, the batch id as additional authenticated data, the
tag appended to the ciphertext, and the content key wrapped with RSA-OAEP
using SHA-256 for both the digest and MGF1.

Reproducing the client here is not a substitute for testing against the real
SDK. test_sdk_contract.py does that by running the published TypeScript
through Node. This fixture exists so the rest of the suite can build envelopes
cheaply.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.crypto import KeyRing, generate_keypair
from app.store import InMemoryIngestStore

PATIENT_CODE = "TRC-2f81a9c4"
PATIENT_ID = "6f1c2a70-6d0e-4a1b-9f3c-2b7d4e5a8c91"
KEY_ID = "traycer-ingest-test"
SIGNING_KEY = "test-signing-key-not-a-real-secret"

# 2026-08-16T08:00:00Z, aligned to an hour so windows land on real boundaries.
BASE_MS = 1_786_780_800_000


@pytest.fixture(scope="session", autouse=True)
def jwt_environment():
    os.environ["JWT_SIGNING_KEY"] = SIGNING_KEY
    os.environ["JWT_AUDIENCE"] = "traycer-ingest"
    os.environ["JWT_ISSUER"] = "traycer-enrollment"
    yield


@pytest.fixture(scope="session")
def keypair():
    return generate_keypair(key_size=2048)


@pytest.fixture(scope="session")
def keyring(keypair):
    private_key, _ = keypair
    ring = KeyRing()
    ring.add(KEY_ID, private_key)
    return ring


@pytest.fixture
def store():
    return InMemoryIngestStore(patients={PATIENT_CODE: PATIENT_ID})


def device_token(
    patient_code: str = PATIENT_CODE,
    scope: str = "ingest:write",
    expires_in: int = 3600,
) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    return jwt.encode(
        {
            "sub": "device-abc123",
            "patient_code": patient_code,
            "scope": scope,
            "aud": "traycer-ingest",
            "iss": "traycer-enrollment",
            "iat": now,
            "exp": now + expires_in,
        },
        SIGNING_KEY,
        algorithm="HS256",
    )


def make_payload(
    batch_id: str = "b6f8e0f2-3a4c-4d5e-8f90-1a2b3c4d5e6f",
    patient_code: str = PATIENT_CODE,
    windows: int = 2,
    start_ms: int = BASE_MS,
) -> dict:
    return {
        "v": 1,
        "patientCode": patient_code,
        "sdkVersion": "0.1.0",
        "batchId": batch_id,
        "createdAt": start_ms + windows * 3_600_000,
        "windows": [
            {
                "windowStart": start_ms + index * 3_600_000,
                "windowMs": 3_600_000,
                "utcOffsetMinutes": 60,
                "samples": [
                    {"feature": "activity_ratio", "value": 0.42, "confidence": 1.0},
                    {"feature": "mobility_radius_m", "value": 1834.2, "confidence": 0.75},
                    {"feature": "screen_on_minutes", "value": 22.5, "confidence": 1.0},
                ],
            }
            for index in range(windows)
        ],
    }


def seal_envelope(payload: dict, jwk_public_key, batch_id: str | None = None) -> dict:
    """Encrypt a payload the way the SDK does."""
    resolved_batch_id = batch_id or payload["batchId"]
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    content_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ciphertext = AESGCM(content_key).encrypt(
        nonce, plaintext, resolved_batch_id.encode("utf-8")
    )

    wrapped = jwk_public_key.encrypt(
        content_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    return {
        "v": 1,
        "batchId": resolved_batch_id,
        "alg": "A256GCM",
        "keyAlg": "RSA-OAEP-256",
        "wrappedKey": base64.b64encode(wrapped).decode(),
        "iv": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "keyId": KEY_ID,
    }


@pytest.fixture
def sealed(keypair):
    private_key, _ = keypair
    public_key = private_key.public_key()

    def build(payload: dict | None = None, batch_id: str | None = None) -> dict:
        return seal_envelope(payload or make_payload(), public_key, batch_id)

    return build
