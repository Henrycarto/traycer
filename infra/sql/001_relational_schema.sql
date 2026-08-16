-- Relational store: patient profiles, clinician panels, alert history,
-- per-patient thresholds. No behavioral samples live here. Sample volume
-- belongs in the Timescale instance where compression and retention apply.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS clinicians (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fhir_user_id    TEXT UNIQUE,
    display_name    TEXT NOT NULL,
    email           TEXT UNIQUE NOT NULL,
    organization    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Patients are referenced by pseudonymous code inside the platform.
-- The FHIR resource id is the only link back to identity, and it is
-- resolved through the EHR at read time rather than copied here.
CREATE TABLE IF NOT EXISTS patients (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_code        TEXT UNIQUE NOT NULL,
    fhir_patient_id     TEXT,
    fhir_issuer         TEXT,
    enrolled_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    baseline_start_at   TIMESTAMPTZ,
    baseline_ready_at   TIMESTAMPTZ,
    sdk_version         TEXT,
    sdk_last_seen_at    TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'enrolling'
                        CHECK (status IN ('enrolling', 'active', 'paused', 'withdrawn'))
);

CREATE INDEX IF NOT EXISTS patients_sdk_last_seen_idx ON patients (sdk_last_seen_at DESC);

CREATE TABLE IF NOT EXISTS panel_membership (
    clinician_id    UUID NOT NULL REFERENCES clinicians (id) ON DELETE CASCADE,
    patient_id      UUID NOT NULL REFERENCES patients (id) ON DELETE CASCADE,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (clinician_id, patient_id)
);

-- Devices authenticate to the ingest endpoint with a device-scoped JWT.
-- The public key is stored so a lost device can be revoked without
-- rotating credentials for the whole panel.
CREATE TABLE IF NOT EXISTS devices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients (id) ON DELETE CASCADE,
    device_hash     TEXT UNIQUE NOT NULL,
    platform        TEXT NOT NULL CHECK (platform IN ('ios', 'android')),
    enrolled_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS patient_baselines (
    patient_id      UUID PRIMARY KEY REFERENCES patients (id) ON DELETE CASCADE,
    built_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    sample_days     INTEGER NOT NULL,
    feature_stats   JSONB NOT NULL,
    quality         TEXT NOT NULL CHECK (quality IN ('insufficient', 'provisional', 'established'))
);

CREATE TABLE IF NOT EXISTS alert_thresholds (
    patient_id          UUID PRIMARY KEY REFERENCES patients (id) ON DELETE CASCADE,
    elevated_score      NUMERIC(5, 2) NOT NULL DEFAULT 55.00,
    critical_score      NUMERIC(5, 2) NOT NULL DEFAULT 75.00,
    min_consecutive     INTEGER NOT NULL DEFAULT 2,
    cooldown_hours      INTEGER NOT NULL DEFAULT 24,
    muted_signals       TEXT[] NOT NULL DEFAULT '{}',
    updated_by          UUID REFERENCES clinicians (id),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (critical_score > elevated_score)
);

CREATE TABLE IF NOT EXISTS alerts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id          UUID NOT NULL REFERENCES patients (id) ON DELETE CASCADE,
    fired_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    severity            TEXT NOT NULL CHECK (severity IN ('stable', 'elevated', 'critical')),
    risk_score          NUMERIC(5, 2) NOT NULL,
    trigger_signal      TEXT NOT NULL,
    trigger_detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    acknowledged_by     UUID REFERENCES clinicians (id),
    acknowledged_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS alerts_patient_fired_idx ON alerts (patient_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS alerts_open_idx ON alerts (fired_at DESC) WHERE acknowledged_at IS NULL;

CREATE TABLE IF NOT EXISTS clinician_devices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinician_id    UUID NOT NULL REFERENCES clinicians (id) ON DELETE CASCADE,
    fcm_token       TEXT UNIQUE NOT NULL,
    platform        TEXT NOT NULL CHECK (platform IN ('ios', 'android', 'web')),
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_success_at TIMESTAMPTZ,
    failure_count   INTEGER NOT NULL DEFAULT 0
);

-- Every read of patient data is recorded. Required for the audit trail
-- an IRB or a covered entity will ask for.
CREATE TABLE IF NOT EXISTS access_log (
    id              BIGSERIAL PRIMARY KEY,
    clinician_id    UUID REFERENCES clinicians (id),
    patient_id      UUID REFERENCES patients (id),
    action          TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_ip       INET
);

CREATE INDEX IF NOT EXISTS access_log_occurred_idx ON access_log (occurred_at DESC);
