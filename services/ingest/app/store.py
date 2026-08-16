"""Persistence for ingest.

Two databases, and no distributed transaction across them. Behavioral samples
go to the Timescale hypertable, and the batch receipt used for deduplication
goes to the relational store.

The write order is samples first, receipt second, and that ordering is the
whole correctness argument:

  If the sample write fails, no receipt exists, the device retries, and
  nothing was stored. Correct.

  If the sample write succeeds and the receipt write fails, the device retries
  the same batch id. The sample insert is idempotent through ON CONFLICT DO
  NOTHING on the hypertable primary key, so the retry rewrites the same rows
  harmlessly and then records the receipt. Correct.

  The reverse order would let a receipt exist for samples that were never
  stored, and the device would treat the batch as delivered and delete it.
  That loses data silently, which is the one outcome worth designing against.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, Sequence

from .engine.validator import ValidatedSample

logger = logging.getLogger("traycer.ingest.store")

POSTGRES_DSN = os.getenv("POSTGRES_DSN", "")
TIMESCALE_DSN = os.getenv("TIMESCALE_DSN", "")


class PatientNotEnrolled(Exception):
    """No patient row matches the code carried by a valid device token.

    Treated as retryable rather than as a bad request. The token was signed by
    enrollment, so the patient is supposed to exist, and a missing row means a
    provisioning step has not caught up. Dropping the batch would discard
    valid data over a platform inconsistency that will resolve itself.
    """


@dataclass(frozen=True)
class BatchReceipt:
    batch_id: str
    patient_code: str
    received_at: datetime
    window_count: int
    sample_count: int


class IngestStore(Protocol):
    async def already_received(self, batch_id: str) -> bool: ...

    async def resolve_patient(self, patient_code: str) -> str: ...

    async def write_samples(self, patient_id: str, samples: Sequence[ValidatedSample]) -> int: ...

    async def record_batch(self, receipt: BatchReceipt, patient_id: str) -> None: ...

    async def touch_device(self, patient_id: str, sdk_version: str, seen_at: datetime) -> None: ...


@dataclass
class InMemoryIngestStore:
    """Used by the tests and by a local run without databases."""

    patients: dict[str, str] = field(default_factory=dict)
    batches: dict[str, BatchReceipt] = field(default_factory=dict)
    samples: list[tuple[str, ValidatedSample]] = field(default_factory=list)
    last_seen: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    # Set to make write_samples raise once, to exercise the retry path.
    fail_next_sample_write: bool = False

    async def already_received(self, batch_id: str) -> bool:
        return batch_id in self.batches

    async def resolve_patient(self, patient_code: str) -> str:
        patient_id = self.patients.get(patient_code)
        if patient_id is None:
            raise PatientNotEnrolled(patient_code)
        return patient_id

    async def write_samples(self, patient_id: str, samples: Sequence[ValidatedSample]) -> int:
        if self.fail_next_sample_write:
            self.fail_next_sample_write = False
            raise RuntimeError("simulated hypertable failure")

        existing = {(pid, s.observed_at_ms, s.feature) for pid, s in self.samples}
        written = 0

        for sample in samples:
            key = (patient_id, sample.observed_at_ms, sample.feature)
            if key in existing:
                continue
            existing.add(key)
            self.samples.append((patient_id, sample))
            written += 1

        return written

    async def record_batch(self, receipt: BatchReceipt, patient_id: str) -> None:
        self.batches[receipt.batch_id] = receipt

    async def touch_device(self, patient_id: str, sdk_version: str, seen_at: datetime) -> None:
        self.last_seen[patient_id] = (sdk_version, seen_at)


class PostgresIngestStore:
    """asyncpg backed store spanning the relational and Timescale instances."""

    def __init__(self, relational_pool, timeseries_pool) -> None:
        self._relational = relational_pool
        self._timeseries = timeseries_pool

    async def already_received(self, batch_id: str) -> bool:
        row = await self._relational.fetchrow(
            "SELECT 1 FROM ingest_batches WHERE batch_id = $1::uuid", batch_id
        )
        return row is not None

    async def resolve_patient(self, patient_code: str) -> str:
        row = await self._relational.fetchrow(
            "SELECT id FROM patients WHERE patient_code = $1", patient_code
        )
        if row is None:
            raise PatientNotEnrolled(patient_code)
        return str(row["id"])

    async def write_samples(self, patient_id: str, samples: Sequence[ValidatedSample]) -> int:
        if not samples:
            return 0

        records = [
            (
                patient_id,
                datetime.fromtimestamp(sample.observed_at_ms / 1000, tz=timezone.utc),
                sample.feature,
                sample.value,
                sample.confidence,
                sample.sdk_version,
            )
            for sample in samples
        ]

        # ON CONFLICT DO NOTHING against the hypertable primary key is what
        # makes a retry after a lost response harmless.
        async with self._timeseries.acquire() as connection:
            async with connection.transaction():
                await connection.executemany(
                    """
                    INSERT INTO behavioral_samples
                        (patient_id, observed_at, feature, value, confidence, sdk_version)
                    VALUES ($1::uuid, $2, $3, $4, $5, $6)
                    ON CONFLICT (patient_id, observed_at, feature) DO NOTHING
                    """,
                    records,
                )

        return len(records)

    async def record_batch(self, receipt: BatchReceipt, patient_id: str) -> None:
        await self._relational.execute(
            """
            INSERT INTO ingest_batches
                (batch_id, patient_id, received_at, window_count, sample_count)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5)
            ON CONFLICT (batch_id) DO NOTHING
            """,
            receipt.batch_id,
            patient_id,
            receipt.received_at,
            receipt.window_count,
            receipt.sample_count,
        )

    async def touch_device(self, patient_id: str, sdk_version: str, seen_at: datetime) -> None:
        await self._relational.execute(
            """
            UPDATE patients
            SET sdk_last_seen_at = GREATEST(COALESCE(sdk_last_seen_at, $2), $2),
                sdk_version = $3
            WHERE id = $1::uuid
            """,
            patient_id,
            seen_at,
            sdk_version,
        )


async def create_pools():
    """Open both pools, or return None when the DSNs are not configured."""
    if not POSTGRES_DSN or not TIMESCALE_DSN:
        logger.warning(
            "POSTGRES_DSN or TIMESCALE_DSN is not set, ingest is using the in memory store"
        )
        return None, None

    import asyncpg

    relational = await asyncpg.create_pool(POSTGRES_DSN, min_size=1, max_size=8)
    timeseries = await asyncpg.create_pool(TIMESCALE_DSN, min_size=1, max_size=8)
    logger.info("connected to the relational and timeseries stores")
    return relational, timeseries
