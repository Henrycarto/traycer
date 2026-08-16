"""Persistence for alerts, thresholds and clinician device registrations.

Two implementations behind one protocol. The in memory pair runs the test
suite and `docker compose up` without a database; the asyncpg pair is what the
deployed service uses. Routers depend on the protocol, so neither one leaks
into the request handlers.

The SQL here matches `infra/sql/001_relational_schema.sql`. When a column
moves, both move together.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Protocol, Sequence

from .schemas import Alert, AlertThresholds, Severity

logger = logging.getLogger("traycer.pulse.store")

POSTGRES_DSN = os.getenv("POSTGRES_DSN", "")


class AlertStore(Protocol):
    async def add(self, alert: Alert) -> None: ...

    async def get(self, alert_id: str) -> Alert | None: ...

    async def acknowledge(self, alert_id: str, by: str, at: datetime) -> Alert | None: ...

    async def mark_notified(self, alert_id: str) -> None: ...

    async def recent(self, limit: int = 100, patient_code: str | None = None) -> list[Alert]: ...


class ClinicianDeviceStore(Protocol):
    async def register(self, clinician_id: str, token: str, platform: str) -> None: ...

    async def tokens_for(self, clinician_id: str) -> list[str]: ...

    async def remove(self, token: str) -> None: ...


class InMemoryAlertStore:
    def __init__(self) -> None:
        self._alerts: dict[str, Alert] = {}

    async def add(self, alert: Alert) -> None:
        self._alerts[alert.alert_id] = alert

    async def get(self, alert_id: str) -> Alert | None:
        return self._alerts.get(alert_id)

    async def acknowledge(self, alert_id: str, by: str, at: datetime) -> Alert | None:
        existing = self._alerts.get(alert_id)
        if existing is None:
            return None
        if existing.acknowledged_at is not None:
            # Acknowledging twice is not an error. Two clinicians opening the
            # same alert on the same panel is routine, and the first one wins.
            return existing

        updated = existing.model_copy(update={"acknowledged_at": at, "acknowledged_by": by})
        self._alerts[alert_id] = updated
        return updated

    async def mark_notified(self, alert_id: str) -> None:
        existing = self._alerts.get(alert_id)
        if existing is not None:
            self._alerts[alert_id] = existing.model_copy(update={"notified": True})

    async def recent(self, limit: int = 100, patient_code: str | None = None) -> list[Alert]:
        rows = [
            alert
            for alert in self._alerts.values()
            if patient_code is None or alert.patient_code == patient_code
        ]
        rows.sort(key=lambda a: a.fired_at, reverse=True)
        return rows[:limit]


class InMemoryClinicianDeviceStore:
    def __init__(self) -> None:
        self._by_clinician: dict[str, dict[str, str]] = {}

    async def register(self, clinician_id: str, token: str, platform: str) -> None:
        self._by_clinician.setdefault(clinician_id, {})[token] = platform

    async def tokens_for(self, clinician_id: str) -> list[str]:
        return sorted(self._by_clinician.get(clinician_id, {}))

    async def remove(self, token: str) -> None:
        for tokens in self._by_clinician.values():
            tokens.pop(token, None)


class PostgresAlertStore:
    """asyncpg backed alert history.

    The pool is injected rather than created here so the lifespan handler owns
    connection lifetime and a test can pass a transaction scoped pool.
    """

    def __init__(self, pool) -> None:  # asyncpg.Pool, untyped to keep the import optional
        self._pool = pool

    async def add(self, alert: Alert) -> None:
        await self._pool.execute(
            """
            INSERT INTO alerts (
                id, patient_id, fired_at, severity, risk_score, trigger_signal, trigger_detail
            )
            SELECT $1::uuid, p.id, $3, $4, $5, $6, $7::jsonb
            FROM patients p
            WHERE p.patient_code = $2
            ON CONFLICT (id) DO NOTHING
            """,
            alert.alert_id,
            alert.patient_code,
            alert.fired_at,
            alert.severity.value,
            alert.risk_score,
            alert.trigger_signal,
            json.dumps(
                {"summary": alert.summary, "consecutive_windows": alert.consecutive_windows}
            ),
        )

    async def get(self, alert_id: str) -> Alert | None:
        row = await self._pool.fetchrow(
            """
            SELECT a.id, p.patient_code, a.fired_at, a.severity, a.risk_score,
                   a.trigger_signal, a.trigger_detail, a.acknowledged_at, a.acknowledged_by
            FROM alerts a
            JOIN patients p ON p.id = a.patient_id
            WHERE a.id = $1::uuid
            """,
            alert_id,
        )
        return _row_to_alert(row) if row else None

    async def acknowledge(self, alert_id: str, by: str, at: datetime) -> Alert | None:
        await self._pool.execute(
            """
            UPDATE alerts
            SET acknowledged_at = $2, acknowledged_by = $3::uuid
            WHERE id = $1::uuid AND acknowledged_at IS NULL
            """,
            alert_id,
            at,
            by,
        )
        return await self.get(alert_id)

    async def mark_notified(self, alert_id: str) -> None:
        await self._pool.execute(
            """
            UPDATE alerts
            SET trigger_detail = trigger_detail || '{"notified": true}'::jsonb
            WHERE id = $1::uuid
            """,
            alert_id,
        )

    async def recent(self, limit: int = 100, patient_code: str | None = None) -> list[Alert]:
        rows = await self._pool.fetch(
            """
            SELECT a.id, p.patient_code, a.fired_at, a.severity, a.risk_score,
                   a.trigger_signal, a.trigger_detail, a.acknowledged_at, a.acknowledged_by
            FROM alerts a
            JOIN patients p ON p.id = a.patient_id
            WHERE ($2::text IS NULL OR p.patient_code = $2)
            ORDER BY a.fired_at DESC
            LIMIT $1
            """,
            limit,
            patient_code,
        )
        return [_row_to_alert(row) for row in rows]


def _row_to_alert(row) -> Alert:
    detail = row["trigger_detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    detail = detail or {}

    return Alert(
        alert_id=str(row["id"]),
        patient_code=row["patient_code"],
        fired_at=row["fired_at"],
        severity=Severity(row["severity"]),
        risk_score=float(row["risk_score"]),
        trigger_signal=row["trigger_signal"],
        summary=detail.get("summary", ""),
        consecutive_windows=int(detail.get("consecutive_windows", 1)),
        acknowledged_at=row["acknowledged_at"],
        acknowledged_by=str(row["acknowledged_by"]) if row["acknowledged_by"] else None,
        notified=bool(detail.get("notified", False)),
    )


class PostgresThresholdStore:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def get(self, patient_code: str) -> AlertThresholds | None:
        row = await self._pool.fetchrow(
            """
            SELECT t.elevated_score, t.critical_score, t.min_consecutive,
                   t.cooldown_hours, t.muted_signals, t.updated_at
            FROM alert_thresholds t
            JOIN patients p ON p.id = t.patient_id
            WHERE p.patient_code = $1
            """,
            patient_code,
        )
        if row is None:
            return None

        return AlertThresholds(
            patient_code=patient_code,
            elevated_score=float(row["elevated_score"]),
            critical_score=float(row["critical_score"]),
            min_consecutive=int(row["min_consecutive"]),
            cooldown_hours=int(row["cooldown_hours"]),
            muted_signals=list(row["muted_signals"] or []),
            updated_at=row["updated_at"],
        )

    async def put(self, thresholds: AlertThresholds) -> None:
        await self._pool.execute(
            """
            INSERT INTO alert_thresholds (
                patient_id, elevated_score, critical_score, min_consecutive,
                cooldown_hours, muted_signals, updated_at
            )
            SELECT p.id, $2, $3, $4, $5, $6, $7
            FROM patients p
            WHERE p.patient_code = $1
            ON CONFLICT (patient_id) DO UPDATE SET
                elevated_score = EXCLUDED.elevated_score,
                critical_score = EXCLUDED.critical_score,
                min_consecutive = EXCLUDED.min_consecutive,
                cooldown_hours = EXCLUDED.cooldown_hours,
                muted_signals = EXCLUDED.muted_signals,
                updated_at = EXCLUDED.updated_at
            """,
            thresholds.patient_code,
            thresholds.elevated_score,
            thresholds.critical_score,
            thresholds.min_consecutive,
            thresholds.cooldown_hours,
            list(thresholds.muted_signals),
            thresholds.updated_at or datetime.now(timezone.utc),
        )

    async def all_patients(self) -> list[str]:
        rows = await self._pool.fetch("SELECT patient_code FROM patients ORDER BY patient_code")
        return [row["patient_code"] for row in rows]


async def create_pool():
    """Open an asyncpg pool, or return None when no DSN is configured.

    Returning None rather than raising lets the service run against the in
    memory stores for local development and CI, which is the same code path
    the tests exercise.
    """
    if not POSTGRES_DSN:
        logger.info("POSTGRES_DSN is not set, using in memory stores")
        return None

    import asyncpg

    pool = await asyncpg.create_pool(POSTGRES_DSN, min_size=1, max_size=8)
    logger.info("connected to postgres")
    return pool


def clinician_tokens_stub(devices: Sequence[str]) -> list[str]:
    """Small helper used by the notify router when a caller passes tokens."""
    return [token for token in devices if token]
