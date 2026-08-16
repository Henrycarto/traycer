"""Envelope decryption, the server half of the SDK's upload encryption.

The device generates a fresh AES-256-GCM content key per batch, encrypts the
payload with it, wraps that key under the clinic's RSA public key, and sends
both. This file reverses that.

Matching the SDK exactly matters more than anything else here, so the
parameters are spelled out rather than left to defaults:

  content cipher   AES-256-GCM, 96 bit nonce, 128 bit tag appended to the
                   ciphertext, which is what WebCrypto's encrypt returns
  additional data  the batch id as UTF-8, authenticated but not encrypted
  key wrapping     RSA-OAEP with SHA-256 for both the digest and MGF1, and an
                   empty label, which is what WebCrypto's RSA-OAEP does

Private keys are held in memory only, loaded once at start from a mounted
secret. Several may be loaded at the same time: a key rotation leaves devices
in the field encrypting under the previous key for as long as their queue
takes to drain, and a server that only holds the current key would reject
that data.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .schemas import EncryptedEnvelope

logger = logging.getLogger("traycer.ingest.crypto")

GCM_NONCE_BYTES = 12
GCM_TAG_BYTES = 16
AES_KEY_BYTES = 32
MIN_RSA_KEY_BITS = 2048


class DecryptionError(Exception):
    """The envelope could not be decrypted. Retrying will not help."""


class UnknownKeyError(Exception):
    """The envelope names a key this server does not hold.

    Distinct from DecryptionError on purpose. An unknown key id usually means
    a secret was not mounted rather than a corrupt payload, so the caller
    answers with a retryable status and the device keeps the data.
    """


@dataclass(frozen=True)
class PrivateKeyEntry:
    key_id: str
    key: rsa.RSAPrivateKey


class KeyRing:
    """The private keys this server can decrypt with."""

    def __init__(self, entries: dict[str, rsa.RSAPrivateKey] | None = None) -> None:
        self._keys: dict[str, rsa.RSAPrivateKey] = dict(entries or {})

    @property
    def key_ids(self) -> list[str]:
        return sorted(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, key_id: str, key: rsa.RSAPrivateKey) -> None:
        if key.key_size < MIN_RSA_KEY_BITS:
            raise ValueError(
                f"key {key_id} is {key.key_size} bits, minimum is {MIN_RSA_KEY_BITS}"
            )
        self._keys[key_id] = key

    def get(self, key_id: str) -> rsa.RSAPrivateKey:
        key = self._keys.get(key_id)
        if key is None:
            raise UnknownKeyError(f"no private key loaded for key id {key_id!r}")
        return key

    @classmethod
    def from_environment(cls) -> "KeyRing":
        """Load keys from either of two variables.

        INGEST_PRIVATE_KEYS maps a key id to a PEM file path:

            {"traycer-ingest-2026-01": "/run/secrets/ingest-2026-01.pem"}

        A path rather than the material, so the key reaches the process
        through a mounted file and never appears in an environment listing.
        This is the better option and it is what docker compose uses.

        INGEST_PRIVATE_KEYS_INLINE maps a key id to the PEM text itself. ECS
        injects secrets from Secrets Manager as environment variables rather
        than as files, so a path only deployment would not work there at all.
        The tradeoff is real: an inline key is visible to anything that can
        read the task's environment, which is why it is the second choice and
        why the log says which one was used.

        Both may be set, and the file form wins on a key id collision.
        """
        ring = cls()
        passphrase = os.getenv("INGEST_PRIVATE_KEY_PASSPHRASE")
        password = passphrase.encode("utf-8") if passphrase else None

        inline = _parse_key_mapping("INGEST_PRIVATE_KEYS_INLINE")
        for key_id, pem in inline.items():
            ring.add(key_id, _load_pem(key_id, pem.encode("utf-8"), password, "inline"))
            logger.info("loaded ingest private key %s from the environment", key_id)

        paths = _parse_key_mapping("INGEST_PRIVATE_KEYS")
        for key_id, path in paths.items():
            try:
                with open(path, "rb") as handle:
                    material = handle.read()
            except OSError as exc:
                raise ValueError(f"could not read private key {key_id!r} from {path}: {exc}") from exc

            ring.add(key_id, _load_pem(key_id, material, password, path))
            logger.info("loaded ingest private key %s from %s", key_id, path)

        if len(ring) == 0:
            logger.warning(
                "no ingest private keys configured, the service cannot decrypt any batch"
            )

        return ring


def _parse_key_mapping(variable: str) -> dict[str, str]:
    raw = os.getenv(variable, "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{variable} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"{variable} must be a JSON object of key id to value")

    return {str(key): str(value) for key, value in parsed.items()}


def _load_pem(key_id: str, material: bytes, password: bytes | None, source: str):
    try:
        loaded = serialization.load_pem_private_key(material, password=password)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"could not load private key {key_id!r} from {source}: {exc}") from exc

    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise ValueError(f"key {key_id!r} is not an RSA private key")

    return loaded


def _b64(value: str, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DecryptionError(f"{field} is not valid base64") from exc


def decrypt_envelope(envelope: EncryptedEnvelope, keyring: KeyRing) -> str:
    """Return the plaintext payload, or raise.

    Raises UnknownKeyError when the key id is not held, which the router turns
    into a retryable response, and DecryptionError for anything else, which is
    permanent and drops the batch.
    """
    private_key = keyring.get(envelope.keyId)

    wrapped_key = _b64(envelope.wrappedKey, "wrappedKey")
    nonce = _b64(envelope.iv, "iv")
    ciphertext = _b64(envelope.ciphertext, "ciphertext")

    if len(nonce) != GCM_NONCE_BYTES:
        raise DecryptionError(f"iv must be {GCM_NONCE_BYTES} bytes, got {len(nonce)}")

    # Ciphertext carries the tag appended, so anything at or below tag length
    # contains no actual payload.
    if len(ciphertext) <= GCM_TAG_BYTES:
        raise DecryptionError("ciphertext is too short to contain a payload and a tag")

    try:
        content_key = private_key.decrypt(
            wrapped_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
    except ValueError as exc:
        # Deliberately vague. A distinguishable failure here is the classic
        # padding oracle, and the caller does not need to know which part of
        # the unwrap failed.
        raise DecryptionError("content key could not be unwrapped") from exc

    if len(content_key) != AES_KEY_BYTES:
        raise DecryptionError(
            f"unwrapped content key is {len(content_key)} bytes, expected {AES_KEY_BYTES}"
        )

    # The batch id is the additional authenticated data. Because it is also
    # the field used for deduplication before decryption, authenticating it
    # closes the gap where an attacker relabels a captured batch.
    additional_data = envelope.batchId.encode("utf-8")

    try:
        plaintext = AESGCM(content_key).decrypt(nonce, ciphertext, additional_data)
    except InvalidTag as exc:
        raise DecryptionError(
            "authentication failed, the payload or its batch id was altered"
        ) from exc

    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecryptionError("plaintext is not valid UTF-8") from exc


def generate_keypair(key_size: int = 2048) -> tuple[rsa.RSAPrivateKey, dict]:
    """Create a keypair and the public JWK a device is configured with.

    Used by the tests and by the enrollment tooling. The JWK shape is what
    `serverPublicKey.jwk` in the SDK expects.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    numbers = private_key.public_key().public_numbers()

    def to_b64url(value: int) -> str:
        length = (value.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(value.to_bytes(length, "big")).decode().rstrip("=")

    jwk = {
        "kty": "RSA",
        "n": to_b64url(numbers.n),
        "e": to_b64url(numbers.e),
        "alg": "RSA-OAEP-256",
        "ext": True,
    }

    return private_key, jwk
