"""End to end tests for the ingest endpoint.

The status codes are asserted deliberately and in detail, because the SDK
queue branches on them. A test that only checked "not an error" would let a
change from 503 to 422 through, and that change would tell every device in the
field to delete data the server never stored.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import batches
from app.store import InMemoryIngestStore

from .conftest import PATIENT_CODE, PATIENT_ID, device_token, make_payload


@pytest.fixture
def client(keyring, store):
    batches.configure(keyring, store)
    app.state.keyring = keyring
    app.state.store = store
    app.state.relational = None
    app.state.timeseries = None

    with TestClient(app, raise_server_exceptions=False) as test_client:
        # The lifespan handler reconfigures from the environment, so the test
        # keyring and store are reapplied after startup.
        batches.configure(keyring, store)
        app.state.keyring = keyring
        yield test_client


def post(client, envelope, token=None, headers=None):
    merged = {"authorization": f"Bearer {token or device_token()}"}
    merged.update(headers or {})
    return client.post("/v1/batches", json=envelope, headers=merged)


class TestHappyPath:
    def test_a_valid_batch_is_stored(self, client, sealed, store):
        response = post(client, sealed())

        assert response.status_code == 202
        body = response.json()
        assert body["error"] is None
        assert body["data"]["accepted_windows"] == 2
        assert body["data"]["accepted_samples"] == 6
        assert body["data"]["rejected_samples"] == 0

    def test_samples_reach_the_store_with_the_right_shape(self, client, sealed, store):
        post(client, sealed())

        assert len(store.samples) == 6
        patient_id, sample = store.samples[0]
        assert patient_id == PATIENT_ID
        assert sample.patient_code == PATIENT_CODE
        assert sample.feature in {"activity_ratio", "mobility_radius_m", "screen_on_minutes"}
        assert 0.0 <= sample.confidence <= 1.0

    def test_the_response_carries_the_standard_envelope(self, client, sealed):
        body = post(client, sealed()).json()

        assert set(body) == {"data", "error", "meta"}
        assert body["meta"]["service"] == "traycer-ingest"

    def test_the_device_last_seen_is_updated(self, client, sealed, store):
        post(client, sealed())

        sdk_version, _ = store.last_seen[PATIENT_ID]
        assert sdk_version == "0.1.0"


class TestDeduplication:
    def test_a_replayed_batch_returns_409(self, client, sealed, store):
        envelope = sealed()

        assert post(client, envelope).status_code == 202
        replay = post(client, envelope)

        # The SDK treats 409 as delivered, which is what makes a retry after a
        # lost response harmless.
        assert replay.status_code == 409
        assert replay.json()["data"]["duplicate"] is True

    def test_a_replay_does_not_duplicate_samples(self, client, sealed, store):
        envelope = sealed()
        post(client, envelope)
        post(client, envelope)

        assert len(store.samples) == 6

    def test_a_different_batch_id_is_not_a_duplicate(self, client, sealed):
        assert post(client, sealed()).status_code == 202

        other = make_payload(batch_id="c7f8e0f2-3a4c-4d5e-8f90-1a2b3c4d5e70")
        assert post(client, sealed(other)).status_code == 202


class TestAuthentication:
    def test_a_missing_token_is_401(self, client, sealed):
        assert client.post("/v1/batches", json=sealed()).status_code == 401

    def test_an_expired_token_is_401(self, client, sealed):
        response = post(client, sealed(), token=device_token(expires_in=-60))

        # 401 keeps the batch on the device so no data is lost while the host
        # app refreshes the token.
        assert response.status_code == 401

    def test_a_token_without_the_ingest_scope_is_403(self, client, sealed):
        response = post(client, sealed(), token=device_token(scope="voice:submit"))
        assert response.status_code == 403

    def test_a_device_cannot_submit_for_another_patient(self, client, sealed):
        response = post(client, sealed(), token=device_token(patient_code="TRC-otherpat"))

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "patient_scope_mismatch"


class TestEnvelopeHandling:
    def test_a_tampered_ciphertext_is_rejected_permanently(self, client, sealed):
        envelope = sealed()
        # Flip a character in the middle of the ciphertext.
        text = envelope["ciphertext"]
        midpoint = len(text) // 2
        swapped = "A" if text[midpoint] != "A" else "B"
        envelope["ciphertext"] = text[:midpoint] + swapped + text[midpoint + 1 :]

        response = post(client, envelope)

        # 422 drops the batch, which is right: the same ciphertext will fail
        # identically on every retry.
        assert response.status_code == 422
        assert response.json()["error"]["code"] in {"decryption_failed", "invalid_payload"}

    def test_relabelling_a_batch_breaks_authentication(self, client, sealed):
        envelope = sealed()
        envelope["batchId"] = "ffffffff-3a4c-4d5e-8f90-1a2b3c4d5e6f"

        # The batch id is authenticated as additional data, so changing it
        # after sealing invalidates the tag. This is what stops an attacker
        # replaying a captured batch under a fresh id to bypass dedup.
        response = post(client, envelope)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "decryption_failed"

    def test_an_unknown_key_id_is_retryable(self, client, sealed):
        envelope = sealed()
        envelope["keyId"] = "traycer-ingest-2099-12"

        response = post(client, envelope)

        # 503 rather than 422. An unknown key is usually an unmounted secret,
        # and dropping real data over a deployment mistake is unacceptable.
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "unknown_key"

    def test_a_header_that_disagrees_with_the_envelope_is_rejected(self, client, sealed):
        response = post(
            client, sealed(), headers={"x-traycer-batch-id": "not-the-same-batch-id"}
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "batch_id_mismatch"

    def test_a_matching_header_is_accepted(self, client, sealed):
        envelope = sealed()
        response = post(client, envelope, headers={"x-traycer-batch-id": envelope["batchId"]})

        assert response.status_code == 202

    def test_a_malformed_envelope_is_rejected(self, client):
        assert post(client, {"v": 1, "batchId": "short"}).status_code == 422


class TestPayloadValidation:
    def test_an_unknown_feature_is_rejected_by_the_schema(self, client, sealed):
        payload = make_payload()
        payload["windows"][0]["samples"][0]["feature"] = "home_address"

        response = post(client, sealed(payload))
        assert response.status_code == 422

    def test_a_value_above_the_ceiling_is_dropped_but_the_batch_is_kept(
        self, client, sealed, store
    ):
        payload = make_payload()
        payload["windows"][0]["samples"][0]["value"] = 900.0  # activity_ratio caps at 1

        response = post(client, sealed(payload))

        assert response.status_code == 202
        assert response.json()["data"]["rejected_samples"] == 1
        # Five of six survive, because one bad number should not cost a
        # patient an entire day of data.
        assert response.json()["data"]["accepted_samples"] == 5

    def test_a_window_far_in_the_future_is_rejected(self, client, sealed):
        payload = make_payload(start_ms=4_102_444_800_000)  # year 2100

        response = post(client, sealed(payload))
        assert response.status_code == 202
        assert response.json()["data"]["accepted_samples"] == 0

    def test_a_batch_with_nothing_storable_is_acknowledged_not_retried(self, client, sealed):
        payload = make_payload(start_ms=4_102_444_800_000)
        response = post(client, sealed(payload))

        # 202 rather than an error. The payload will fail identically forever,
        # so asking the device to retry it wastes battery and never succeeds.
        assert response.status_code == 202
        assert response.json()["data"]["accepted_windows"] == 0

    def test_a_payload_for_a_mismatched_batch_id_is_rejected(self, client, keypair):
        from .conftest import seal_envelope

        private_key, _ = keypair
        payload = make_payload(batch_id="aaaaaaaa-3a4c-4d5e-8f90-1a2b3c4d5e6f")
        # Seal under a different id than the payload declares.
        envelope = seal_envelope(
            payload, private_key.public_key(), batch_id="bbbbbbbb-3a4c-4d5e-8f90-1a2b3c4d5e6f"
        )

        response = post(client, envelope)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "batch_id_mismatch"


class TestStoreFailures:
    def test_an_unprovisioned_patient_is_retryable(self, client, keyring, sealed):
        empty = InMemoryIngestStore(patients={})
        batches.configure(keyring, empty)

        response = post(client, sealed())

        # The token is valid, so the data is real. 503 keeps it on the device
        # until provisioning catches up.
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "patient_not_provisioned"

    def test_a_sample_write_failure_is_retryable_and_leaves_no_receipt(
        self, client, keyring, sealed
    ):
        failing = InMemoryIngestStore(patients={PATIENT_CODE: PATIENT_ID})
        failing.fail_next_sample_write = True
        batches.configure(keyring, failing)

        envelope = sealed()
        response = post(client, envelope)

        assert response.status_code == 503
        # No receipt, so the retry is treated as a fresh batch rather than a
        # duplicate. This is the ordering argument in store.py.
        assert envelope["batchId"] not in failing.batches

        batches.configure(keyring, failing)
        retry = post(client, envelope)
        assert retry.status_code == 202
        assert len(failing.samples) == 6


class TestHealth:
    def test_health_reports_the_loaded_key_ids(self, client):
        body = client.get("/health").json()

        assert body["data"]["status"] == "ok"
        assert "traycer-ingest-test" in body["data"]["key_ids"]


class TestBodyLimit:
    def test_an_oversized_declared_body_is_refused(self, client, sealed):
        response = client.post(
            "/v1/batches",
            json=sealed(),
            headers={
                "authorization": f"Bearer {device_token()}",
                "content-length": str(50 * 1024 * 1024),
            },
        )

        assert response.status_code == 413
