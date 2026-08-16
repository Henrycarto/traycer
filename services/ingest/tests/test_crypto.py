from __future__ import annotations

import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import serialization

from app.crypto import (
    DecryptionError,
    KeyRing,
    UnknownKeyError,
    decrypt_envelope,
    generate_keypair,
)
from app.schemas import EncryptedEnvelope

from .conftest import KEY_ID, make_payload, seal_envelope


def pem_bytes(private_key) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class TestRoundTrip:
    def test_a_sealed_payload_comes_back_intact(self, keypair, keyring):
        private_key, _ = keypair
        payload = make_payload()

        envelope = EncryptedEnvelope.model_validate(
            seal_envelope(payload, private_key.public_key())
        )

        assert json.loads(decrypt_envelope(envelope, keyring)) == payload

    def test_each_batch_uses_a_different_content_key(self, keypair):
        private_key, _ = keypair
        first = seal_envelope(make_payload(), private_key.public_key())
        second = seal_envelope(make_payload(), private_key.public_key())

        # A reused content key would show as an identical wrapped key. Fresh
        # keys per batch are what keep one compromised batch from exposing
        # the rest.
        assert first["wrappedKey"] != second["wrappedKey"]
        assert first["iv"] != second["iv"]


class TestFailureModes:
    def test_an_unknown_key_id_raises_its_own_error(self, keypair, keyring):
        private_key, _ = keypair
        envelope = seal_envelope(make_payload(), private_key.public_key())
        envelope["keyId"] = "some-other-key"

        # A distinct exception type, because the router answers 503 for this
        # and 422 for a genuine decryption failure.
        with pytest.raises(UnknownKeyError):
            decrypt_envelope(EncryptedEnvelope.model_validate(envelope), keyring)

    def test_a_wrong_private_key_cannot_unwrap(self, keypair):
        private_key, _ = keypair
        other_key, _ = generate_keypair(2048)

        ring = KeyRing()
        ring.add(KEY_ID, other_key)

        envelope = EncryptedEnvelope.model_validate(
            seal_envelope(make_payload(), private_key.public_key())
        )

        with pytest.raises(DecryptionError):
            decrypt_envelope(envelope, ring)

    def test_a_truncated_ciphertext_is_rejected(self, keypair, keyring):
        private_key, _ = keypair
        envelope = seal_envelope(make_payload(), private_key.public_key())
        envelope["ciphertext"] = base64.b64encode(b"short").decode()

        with pytest.raises(DecryptionError, match="too short"):
            decrypt_envelope(EncryptedEnvelope.model_validate(envelope), keyring)

    def test_a_wrong_length_nonce_is_rejected(self, keypair, keyring):
        private_key, _ = keypair
        envelope = seal_envelope(make_payload(), private_key.public_key())
        envelope["iv"] = base64.b64encode(b"\x00" * 8).decode()

        with pytest.raises(DecryptionError, match="iv must be"):
            decrypt_envelope(EncryptedEnvelope.model_validate(envelope), keyring)

    def test_invalid_base64_is_rejected(self, keypair, keyring):
        private_key, _ = keypair
        envelope = seal_envelope(make_payload(), private_key.public_key())
        envelope["wrappedKey"] = "not base64 at all!!"

        with pytest.raises(DecryptionError, match="base64"):
            decrypt_envelope(EncryptedEnvelope.model_validate(envelope), keyring)


class TestKeyRing:
    def test_a_weak_key_is_refused(self):
        weak, _ = generate_keypair(1024)
        ring = KeyRing()

        with pytest.raises(ValueError, match="minimum is 2048"):
            ring.add("weak", weak)

    def test_key_ids_are_reported_for_health(self, keyring):
        assert keyring.key_ids == [KEY_ID]


class TestEnvironmentLoading:
    def test_keys_load_from_a_file_path(self, tmp_path, monkeypatch, keypair):
        private_key, _ = keypair
        path = tmp_path / "ingest.pem"
        path.write_bytes(pem_bytes(private_key))

        monkeypatch.setenv("INGEST_PRIVATE_KEYS", json.dumps({"from-file": str(path)}))
        monkeypatch.delenv("INGEST_PRIVATE_KEYS_INLINE", raising=False)

        ring = KeyRing.from_environment()
        assert ring.key_ids == ["from-file"]

    def test_keys_load_inline_for_ecs(self, monkeypatch, keypair):
        """ECS injects secrets as environment variables, not files."""
        private_key, _ = keypair

        monkeypatch.delenv("INGEST_PRIVATE_KEYS", raising=False)
        monkeypatch.setenv(
            "INGEST_PRIVATE_KEYS_INLINE",
            json.dumps({"from-env": pem_bytes(private_key).decode()}),
        )

        ring = KeyRing.from_environment()
        assert ring.key_ids == ["from-env"]

    def test_both_sources_can_be_loaded_together(self, tmp_path, monkeypatch, keypair):
        private_key, _ = keypair
        other, _ = generate_keypair(2048)

        path = tmp_path / "rotated.pem"
        path.write_bytes(pem_bytes(other))

        monkeypatch.setenv("INGEST_PRIVATE_KEYS", json.dumps({"current": str(path)}))
        monkeypatch.setenv(
            "INGEST_PRIVATE_KEYS_INLINE",
            json.dumps({"previous": pem_bytes(private_key).decode()}),
        )

        # Holding both keys is what lets a rotation happen without rejecting
        # batches queued on devices under the previous key.
        ring = KeyRing.from_environment()
        assert ring.key_ids == ["current", "previous"]

    def test_no_configuration_yields_an_empty_ring(self, monkeypatch):
        monkeypatch.delenv("INGEST_PRIVATE_KEYS", raising=False)
        monkeypatch.delenv("INGEST_PRIVATE_KEYS_INLINE", raising=False)

        assert len(KeyRing.from_environment()) == 0

    def test_malformed_configuration_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("INGEST_PRIVATE_KEYS", "{not json")
        monkeypatch.delenv("INGEST_PRIVATE_KEYS_INLINE", raising=False)

        with pytest.raises(ValueError, match="not valid JSON"):
            KeyRing.from_environment()

    def test_a_missing_key_file_fails_loudly(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "INGEST_PRIVATE_KEYS", json.dumps({"absent": str(tmp_path / "nope.pem")})
        )
        monkeypatch.delenv("INGEST_PRIVATE_KEYS_INLINE", raising=False)

        with pytest.raises(ValueError, match="could not read"):
            KeyRing.from_environment()
