"""Generate an ingest keypair.

Writes the private key as PEM for the ingest service and prints the public JWK
that goes into the SDK's `serverPublicKey` configuration. The two halves of
the upload encryption are produced together here so they cannot drift.

    python scripts/generate-ingest-keys.py --key-id traycer-ingest-2026-01

In production the private key is generated in KMS and never touches a disk.
This script is for local development and for a first integration, and it says
so in the output rather than leaving that to be assumed.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def to_b64url(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).decode().rstrip("=")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Traycer ingest keypair.")
    parser.add_argument("--key-id", required=True, help="Key id echoed in every envelope.")
    parser.add_argument(
        "--out-dir",
        default="infra/secrets",
        help="Directory for the private key PEM. Gitignored by default.",
    )
    parser.add_argument("--key-size", type=int, default=3072)
    args = parser.parse_args()

    if args.key_size < 2048:
        print("key size must be at least 2048 bits", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pem_path = out_dir / f"{args.key_id}.pem"

    if pem_path.exists():
        print(f"refusing to overwrite {pem_path}", file=sys.stderr)
        return 1

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=args.key_size)

    pem_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    try:
        pem_path.chmod(0o600)
    except OSError:
        # Windows does not honour POSIX modes. The file is still inside a
        # gitignored directory, which is the control that matters here.
        pass

    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "n": to_b64url(numbers.n),
        "e": to_b64url(numbers.e),
        "alg": "RSA-OAEP-256",
        "ext": True,
    }

    print(f"private key written to {pem_path}")
    print("\nSet on the ingest service:\n")
    print(f'  INGEST_PRIVATE_KEYS={json.dumps({args.key_id: str(pem_path)})}')
    print("\nPass to the SDK as serverPublicKey:\n")
    print(json.dumps({"keyId": args.key_id, "jwk": jwk}, indent=2))
    print(
        "\nThis key was generated on disk, which is fine for development. "
        "For production, generate it in KMS so the private half never leaves it."
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
