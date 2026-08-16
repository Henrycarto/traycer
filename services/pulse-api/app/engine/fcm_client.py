"""Firebase Cloud Messaging push to clinician devices.

Uses the FCM HTTP v1 API. The legacy server key API is deprecated and sends a
long lived shared secret with every request, which is the wrong shape for a
service that pushes patient alerts.

Access tokens are minted from the service account key with a signed JWT
assertion and cached until shortly before expiry. That is the whole OAuth flow
for a service account, and implementing it directly avoids pulling the full
firebase-admin dependency tree into a service that sends one message type.

What the notification carries matters as much as how it is sent. A push
notification renders on a lock screen in a waiting room, so the payload
contains a pseudonymous patient code, a severity and a one line summary.
No name, no diagnosis, and no score in the visible text.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import httpx
import jwt

from ..schemas import Alert, NotifyResult, Severity

logger = logging.getLogger("traycer.pulse.fcm")

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_ENDPOINT = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

# Refresh the access token this many seconds before it actually expires, so a
# request in flight never uses a token that expires mid delivery.
TOKEN_REFRESH_MARGIN_SECONDS = 300

# FCM returns these when a token belongs to an uninstalled app. They are the
# only failures worth acting on automatically, by removing the registration.
UNREGISTERED_ERRORS = {"UNREGISTERED", "INVALID_ARGUMENT", "NOT_FOUND"}

# Android notification channels, so a critical alert can bypass do not disturb
# while an elevated one does not.
CHANNEL_BY_SEVERITY = {
    Severity.CRITICAL: "traycer_critical",
    Severity.ELEVATED: "traycer_elevated",
    Severity.STABLE: "traycer_default",
}


class FcmConfigurationError(RuntimeError):
    """Credentials are missing or unusable."""


class FcmDeliveryError(RuntimeError):
    """FCM rejected the request for a reason that is not per token."""


@dataclass(frozen=True)
class ServiceAccount:
    project_id: str
    client_email: str
    private_key: str
    token_uri: str = GOOGLE_TOKEN_URL

    @classmethod
    def from_file(cls, path: Path) -> "ServiceAccount":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FcmConfigurationError(f"could not read FCM credentials: {exc}") from exc

        missing = [k for k in ("project_id", "client_email", "private_key") if k not in raw]
        if missing:
            raise FcmConfigurationError(
                f"FCM credentials are missing {', '.join(missing)}"
            )

        return cls(
            project_id=raw["project_id"],
            client_email=raw["client_email"],
            private_key=raw["private_key"],
            token_uri=raw.get("token_uri", GOOGLE_TOKEN_URL),
        )


class FcmClient:
    def __init__(
        self,
        credentials_path: str | None = None,
        project_id: str | None = None,
        dry_run: bool | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credentials_path = credentials_path or os.getenv("FCM_CREDENTIALS_PATH", "")
        self._project_id_override = project_id or os.getenv("FCM_PROJECT_ID", "")
        self._dry_run = (
            dry_run
            if dry_run is not None
            else os.getenv("FCM_DRY_RUN", "false").lower() in {"1", "true", "yes"}
        )
        self._client = client
        self._account: ServiceAccount | None = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def configured(self) -> bool:
        if self._dry_run:
            return True
        # An account already in hand counts as configured. It gets there
        # either from the credentials file or from an injected value, and the
        # second path is how the delivery tests run without a key on disk.
        if self._account is not None:
            return True
        return bool(self._credentials_path) and Path(self._credentials_path).is_file()

    async def send_alert(self, alert: Alert, tokens: Sequence[str]) -> NotifyResult:
        """Deliver one alert to every registered clinician device.

        Sends one request per token because the HTTP v1 API has no multicast
        endpoint. A clinician has one or two devices, so the loop is short and
        the per token error detail is worth more than a batch would save.
        """
        if not tokens:
            return NotifyResult(
                alert_id=alert.alert_id,
                delivered=0,
                failed=0,
                dry_run=self._dry_run,
                suppressed_reason="no registered clinician devices",
            )

        if self._dry_run:
            # Local development and CI. The message is built and validated so
            # a payload error still surfaces, it simply is not sent.
            for token in tokens:
                self._build_message(alert, token)
            logger.info("dry run, %d notifications not sent", len(tokens))
            return NotifyResult(
                alert_id=alert.alert_id,
                delivered=len(tokens),
                failed=0,
                dry_run=True,
                suppressed_reason=None,
            )

        if not self.configured:
            raise FcmConfigurationError(
                "FCM credentials are not configured and dry run is off"
            )

        access_token = await self._get_access_token()
        account = self._require_account()
        project_id = self._project_id_override or account.project_id
        url = FCM_ENDPOINT.format(project_id=project_id)

        delivered = 0
        failed = 0
        invalid: list[str] = []

        client = self._client or httpx.AsyncClient(timeout=15.0)
        owns_client = self._client is None

        try:
            for token in tokens:
                message = self._build_message(alert, token)
                try:
                    response = await client.post(
                        url,
                        json={"message": message},
                        headers={
                            "authorization": f"Bearer {access_token}",
                            "content-type": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    failed += 1
                    logger.warning("FCM request failed: %s", type(exc).__name__)
                    continue

                if response.status_code == 200:
                    delivered += 1
                    continue

                failed += 1
                reason = _error_status(response)
                if reason in UNREGISTERED_ERRORS:
                    invalid.append(token)
                logger.warning(
                    "FCM rejected a message", extra={"status": response.status_code}
                )
        finally:
            if owns_client:
                await client.aclose()

        return NotifyResult(
            alert_id=alert.alert_id,
            delivered=delivered,
            failed=failed,
            dry_run=False,
            invalid_tokens=invalid,
        )

    def _build_message(self, alert: Alert, token: str) -> dict[str, Any]:
        """Compose the FCM v1 message.

        The visible title and body avoid anything that would identify the
        patient to someone glancing at the phone. The score and the signal
        travel in the data block, which only the app can read.
        """
        title = (
            "Traycer: relapse risk"
            if alert.severity is Severity.CRITICAL
            else "Traycer: trajectory change"
        )
        body = f"{alert.patient_code}. {alert.summary}"

        return {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": {
                "alert_id": alert.alert_id,
                "patient_code": alert.patient_code,
                "severity": alert.severity.value,
                "risk_score": f"{alert.risk_score:.1f}",
                "trigger_signal": alert.trigger_signal,
                "fired_at": alert.fired_at.isoformat(),
            },
            "android": {
                "priority": "high" if alert.severity is Severity.CRITICAL else "normal",
                "notification": {
                    "channel_id": CHANNEL_BY_SEVERITY[alert.severity],
                    "notification_priority": (
                        "PRIORITY_MAX" if alert.severity is Severity.CRITICAL else "PRIORITY_DEFAULT"
                    ),
                },
            },
            "apns": {
                "headers": {
                    "apns-priority": "10" if alert.severity is Severity.CRITICAL else "5",
                    # Time sensitive so a critical alert can break through
                    # focus modes. Elevated alerts deliberately do not.
                    "apns-push-type": "alert",
                },
                "payload": {
                    "aps": {
                        "sound": "default" if alert.severity is Severity.CRITICAL else None,
                        "interruption-level": (
                            "time-sensitive"
                            if alert.severity is Severity.CRITICAL
                            else "active"
                        ),
                    }
                },
            },
        }

    def _require_account(self) -> ServiceAccount:
        if self._account is None:
            self._account = ServiceAccount.from_file(Path(self._credentials_path))
        return self._account

    async def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            return self._access_token

        account = self._require_account()
        assertion = self._build_assertion(account, now)

        client = self._client or httpx.AsyncClient(timeout=15.0)
        owns_client = self._client is None

        try:
            response = await client.post(
                account.token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise FcmDeliveryError(f"could not reach the token endpoint: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code != 200:
            raise FcmConfigurationError(
                f"token exchange failed with status {response.status_code}"
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise FcmConfigurationError("token endpoint returned no access token")

        self._access_token = str(token)
        self._token_expires_at = now + float(payload.get("expires_in", 3600))
        return self._access_token

    @staticmethod
    def _build_assertion(account: ServiceAccount, now: float) -> str:
        claims = {
            "iss": account.client_email,
            "scope": FCM_SCOPE,
            "aud": account.token_uri,
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        try:
            return jwt.encode(claims, account.private_key, algorithm="RS256")
        except Exception as exc:
            raise FcmConfigurationError(
                f"could not sign the service account assertion: {exc}"
            ) from exc


def _error_status(response: httpx.Response) -> str:
    """Pull the machine readable status out of an FCM error body."""
    try:
        payload = response.json()
    except ValueError:
        return "UNKNOWN"

    error = payload.get("error", {})
    for detail in error.get("details", []):
        code = detail.get("errorCode")
        if code:
            return str(code)
    return str(error.get("status", "UNKNOWN"))
