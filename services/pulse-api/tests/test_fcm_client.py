from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.engine.fcm_client import (
    FcmClient,
    FcmConfigurationError,
    ServiceAccount,
)
from app.schemas import Alert, Severity

FIRED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


def make_alert(severity: Severity = Severity.CRITICAL) -> Alert:
    return Alert(
        alert_id="0f0a4d10-2c3b-4a5d-9e8f-112233445566",
        patient_code="TRC-2f81a9c4",
        fired_at=FIRED_AT,
        severity=severity,
        risk_score=81.4,
        trigger_signal="mobility_radius_m",
        summary="Sustained deviation across 3 windows: movement radius well below their usual range.",
        consecutive_windows=3,
    )


class TestDryRun:
    async def test_dry_run_reports_delivery_without_sending(self) -> None:
        client = FcmClient(dry_run=True)
        result = await client.send_alert(make_alert(), ["token-a", "token-b"])

        assert result.dry_run is True
        assert result.delivered == 2
        assert result.failed == 0

    async def test_dry_run_counts_as_configured(self) -> None:
        assert FcmClient(dry_run=True).configured is True

    async def test_no_tokens_is_reported_rather_than_treated_as_success(self) -> None:
        result = await FcmClient(dry_run=True).send_alert(make_alert(), [])

        assert result.delivered == 0
        assert result.suppressed_reason == "no registered clinician devices"


class TestMessageComposition:
    def test_the_visible_body_carries_no_identity_beyond_the_patient_code(self) -> None:
        message = FcmClient(dry_run=True)._build_message(make_alert(), "token-a")
        body = message["notification"]["body"]

        assert "TRC-2f81a9c4" in body
        # A lock screen in a waiting room must not show a diagnosis or a score.
        assert "81.4" not in body
        assert "relapse" not in body.lower()

    def test_the_score_travels_in_the_data_block(self) -> None:
        message = FcmClient(dry_run=True)._build_message(make_alert(), "token-a")

        assert message["data"]["risk_score"] == "81.4"
        assert message["data"]["severity"] == "critical"
        assert message["data"]["trigger_signal"] == "mobility_radius_m"

    def test_a_critical_alert_gets_high_priority_on_both_platforms(self) -> None:
        message = FcmClient(dry_run=True)._build_message(make_alert(Severity.CRITICAL), "t")

        assert message["android"]["priority"] == "high"
        assert message["apns"]["headers"]["apns-priority"] == "10"
        assert message["apns"]["payload"]["aps"]["interruption-level"] == "time-sensitive"

    def test_an_elevated_alert_does_not_break_through_focus_modes(self) -> None:
        message = FcmClient(dry_run=True)._build_message(make_alert(Severity.ELEVATED), "t")

        assert message["android"]["priority"] == "normal"
        assert message["apns"]["headers"]["apns-priority"] == "5"
        assert message["apns"]["payload"]["aps"]["interruption-level"] == "active"

    def test_severity_selects_the_android_channel(self) -> None:
        critical = FcmClient(dry_run=True)._build_message(make_alert(Severity.CRITICAL), "t")
        elevated = FcmClient(dry_run=True)._build_message(make_alert(Severity.ELEVATED), "t")

        assert critical["android"]["notification"]["channel_id"] == "traycer_critical"
        assert elevated["android"]["notification"]["channel_id"] == "traycer_elevated"

    def test_the_message_targets_exactly_one_token(self) -> None:
        message = FcmClient(dry_run=True)._build_message(make_alert(), "token-a")
        assert message["token"] == "token-a"


class TestCredentialLoading:
    def test_a_complete_service_account_file_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "fcm.json"
        path.write_text(
            json.dumps(
                {
                    "project_id": "traycer-prod",
                    "client_email": "push@traycer-prod.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
                }
            ),
            encoding="utf-8",
        )

        account = ServiceAccount.from_file(path)
        assert account.project_id == "traycer-prod"

    def test_a_missing_file_is_a_configuration_error(self, tmp_path: Path) -> None:
        with pytest.raises(FcmConfigurationError, match="could not read"):
            ServiceAccount.from_file(tmp_path / "absent.json")

    def test_an_incomplete_file_names_what_is_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "fcm.json"
        path.write_text(json.dumps({"project_id": "traycer-prod"}), encoding="utf-8")

        with pytest.raises(FcmConfigurationError, match="client_email"):
            ServiceAccount.from_file(path)

    async def test_sending_without_credentials_and_without_dry_run_fails_loudly(self) -> None:
        client = FcmClient(credentials_path="", dry_run=False)

        with pytest.raises(FcmConfigurationError):
            await client.send_alert(make_alert(), ["token-a"])


class TestDelivery:
    async def test_a_successful_send_is_counted(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"name": "ok"}))
        client = FcmClient(
            credentials_path="unused",
            project_id="traycer-test",
            dry_run=False,
            client=httpx.AsyncClient(transport=transport),
        )
        client._access_token = "cached"
        client._token_expires_at = 4102444800.0  # far future
        client._account = ServiceAccount(
            project_id="traycer-test", client_email="a@b.c", private_key="k"
        )

        result = await client.send_alert(make_alert(), ["token-a", "token-b"])

        assert result.delivered == 2
        assert result.failed == 0

    async def test_an_unregistered_token_is_reported_for_removal(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "status": "NOT_FOUND",
                        "details": [{"errorCode": "UNREGISTERED"}],
                    }
                },
            )

        client = FcmClient(
            credentials_path="unused",
            project_id="traycer-test",
            dry_run=False,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._access_token = "cached"
        client._token_expires_at = 4102444800.0
        client._account = ServiceAccount(
            project_id="traycer-test", client_email="a@b.c", private_key="k"
        )

        result = await client.send_alert(make_alert(), ["stale-token"])

        assert result.failed == 1
        assert result.invalid_tokens == ["stale-token"]

    async def test_a_transport_failure_counts_as_failed_without_raising(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network down", request=request)

        client = FcmClient(
            credentials_path="unused",
            project_id="traycer-test",
            dry_run=False,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._access_token = "cached"
        client._token_expires_at = 4102444800.0
        client._account = ServiceAccount(
            project_id="traycer-test", client_email="a@b.c", private_key="k"
        )

        result = await client.send_alert(make_alert(), ["token-a"])

        assert result.delivered == 0
        assert result.failed == 1
        assert result.invalid_tokens == []
