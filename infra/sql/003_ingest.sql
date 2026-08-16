-- Batch receipts for upload deduplication.
--
-- The SDK generates a batch id once, at enqueue time, and reuses it across
-- every retry of that batch. This table is what makes a retry after a lost
-- response harmless: the second delivery finds a receipt, answers 409, and
-- the device removes the batch from its queue.
--
-- Deliberately narrow. It holds no behavioral values, only the fact that a
-- batch arrived and how much it carried, so it can be kept far longer than
-- the samples themselves for reconciliation without extending the retention
-- of any patient data.

CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_id        UUID PRIMARY KEY,
    patient_id      UUID NOT NULL REFERENCES patients (id) ON DELETE CASCADE,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_count    INTEGER NOT NULL DEFAULT 0,
    sample_count    INTEGER NOT NULL DEFAULT 0
);

-- Answers "when did this patient last successfully upload", which is what the
-- SDK status column on the dashboard reports, and it does so without touching
-- the hypertable.
CREATE INDEX IF NOT EXISTS ingest_batches_patient_received_idx
    ON ingest_batches (patient_id, received_at DESC);

-- Supports the operational question of how much arrived in the last hour
-- across the whole panel.
CREATE INDEX IF NOT EXISTS ingest_batches_received_idx
    ON ingest_batches (received_at DESC);
