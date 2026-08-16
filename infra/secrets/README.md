Private keys for local development live here and are gitignored.

Generate one before starting the ingest service:

    python scripts/generate-ingest-keys.py --key-id traycer-ingest-local

That writes traycer-ingest-local.pem into this directory and prints the public
JWK to configure the SDK with. The compose file mounts this directory read
only at /run/secrets.

Nothing in here is committed. In production the private key is generated in
KMS and never touches a filesystem.