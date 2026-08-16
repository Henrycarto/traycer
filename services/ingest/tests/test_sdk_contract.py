"""Cross language contract test between @traycer/sdk and this service.

Everything else in this suite builds envelopes with a Python reimplementation
of the SDK's encryption. That proves the service is self consistent, which is
not the same as proving it can read what a real device sends. A mistake copied
into both halves would pass every one of those tests.

This test runs the published TypeScript through Node, using the SDK's own
built `createWebCryptoAdapter`, and decrypts the result here. It is the only
test that can catch a divergence in the wire format, and the wire format is
the interface with the highest cost of being wrong: the SDK is installed on
patient phones and cannot be rolled back on a schedule we control.

Skipped when Node is unavailable or the SDK has not been built. CI builds the
SDK before running it, so it does not silently skip there.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.crypto import KeyRing, decrypt_envelope, generate_keypair
from app.schemas import EncryptedEnvelope, UploadPayload

REPO_ROOT = Path(__file__).resolve().parents[3]
SDK_CRYPTO = REPO_ROOT / "packages" / "sdk" / "dist" / "internal" / "crypto.js"

pytestmark = [
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
    pytest.mark.skipif(
        not SDK_CRYPTO.exists(),
        reason="the SDK is not built, run npm run build --workspace=@traycer/sdk",
    ),
]

# Mirrors what the SDK's data minimizer emits, so this exercises a realistic
# payload rather than a placeholder string.
PAYLOAD = {
    "v": 1,
    "patientCode": "TRC-2f81a9c4",
    "sdkVersion": "0.1.0",
    "batchId": "b6f8e0f2-3a4c-4d5e-8f90-1a2b3c4d5e6f",
    "createdAt": 1786780800000,
    "windows": [
        {
            "windowStart": 1786777200000,
            "windowMs": 3600000,
            "utcOffsetMinutes": 60,
            "samples": [
                {"feature": "activity_ratio", "value": 0.42, "confidence": 1.0},
                {"feature": "mobility_radius_m", "value": 1834.2, "confidence": 0.75},
                {"feature": "night_usage_minutes", "value": 47.0, "confidence": 1.0},
            ],
        }
    ],
}

NODE_SCRIPT = """
const { createWebCryptoAdapter } = require(process.argv[2]);

async function main() {
  const serverKey = JSON.parse(process.argv[3]);
  const payload = process.argv[4];
  const batchId = process.argv[5];

  const adapter = createWebCryptoAdapter(serverKey);
  const envelope = await adapter.encryptBatch(payload, batchId);

  process.stdout.write(JSON.stringify(envelope));
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
"""


def seal_with_real_sdk(jwk: dict, plaintext: str, batch_id: str) -> dict:
    with tempfile.TemporaryDirectory() as work_dir:
        # .cjs, because the SDK builds to CommonJS and a .mjs extension would
        # force the loader into ES module mode where require does not exist.
        script = Path(work_dir) / "seal.cjs"
        script.write_text(NODE_SCRIPT, encoding="utf-8")

        result = subprocess.run(
            [
                "node",
                str(script),
                str(SDK_CRYPTO),
                json.dumps({"keyId": "traycer-ingest-test", "jwk": jwk}),
                plaintext,
                batch_id,
            ],
            capture_output=True,
            timeout=60,
        )

    if result.returncode != 0:
        pytest.fail(f"the SDK failed to seal the envelope:\n{result.stderr.decode()}")

    return json.loads(result.stdout.decode())


class TestSdkWireFormat:
    def test_the_service_decrypts_what_the_real_sdk_produces(self):
        private_key, jwk = generate_keypair(2048)
        keyring = KeyRing()
        keyring.add("traycer-ingest-test", private_key)

        plaintext = json.dumps(PAYLOAD, separators=(",", ":"))
        raw = seal_with_real_sdk(jwk, plaintext, PAYLOAD["batchId"])

        envelope = EncryptedEnvelope.model_validate(raw)
        recovered = decrypt_envelope(envelope, keyring)

        assert json.loads(recovered) == PAYLOAD

    def test_the_sdk_envelope_matches_the_declared_schema(self):
        private_key, jwk = generate_keypair(2048)
        raw = seal_with_real_sdk(jwk, json.dumps(PAYLOAD), PAYLOAD["batchId"])

        # Fails if the SDK adds, renames or drops a field, because the model
        # forbids extras and requires every declared one.
        envelope = EncryptedEnvelope.model_validate(raw)

        assert envelope.alg == "A256GCM"
        assert envelope.keyAlg == "RSA-OAEP-256"
        assert envelope.keyId == "traycer-ingest-test"
        assert envelope.batchId == PAYLOAD["batchId"]

    def test_the_decrypted_payload_validates_against_the_upload_schema(self):
        private_key, jwk = generate_keypair(2048)
        keyring = KeyRing()
        keyring.add("traycer-ingest-test", private_key)

        raw = seal_with_real_sdk(jwk, json.dumps(PAYLOAD), PAYLOAD["batchId"])
        recovered = decrypt_envelope(EncryptedEnvelope.model_validate(raw), keyring)

        payload = UploadPayload.model_validate_json(recovered)
        assert payload.patientCode == "TRC-2f81a9c4"
        assert len(payload.windows) == 1
        assert len(payload.windows[0].samples) == 3

    def test_a_batch_id_the_sdk_did_not_authenticate_is_rejected(self):
        """The SDK authenticates the batch id as additional data.

        Relabelling a captured envelope has to fail, otherwise an attacker
        could replay one batch under many ids and defeat deduplication.
        """
        private_key, jwk = generate_keypair(2048)
        keyring = KeyRing()
        keyring.add("traycer-ingest-test", private_key)

        raw = seal_with_real_sdk(jwk, json.dumps(PAYLOAD), PAYLOAD["batchId"])
        raw["batchId"] = "ffffffff-3a4c-4d5e-8f90-1a2b3c4d5e6f"

        from app.crypto import DecryptionError

        with pytest.raises(DecryptionError):
            decrypt_envelope(EncryptedEnvelope.model_validate(raw), keyring)
