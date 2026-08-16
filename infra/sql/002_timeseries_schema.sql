-- Continuous behavioral metric store. One row per feature per interval per
-- patient. Volume is the reason this is a hypertable: 40 patients at 24
-- windows a day across 9 features is roughly 3.1 million rows a year, and
-- the scoring engine reads 30-day slices on every panel refresh.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS behavioral_samples (
    patient_id      UUID        NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL,
    feature         TEXT        NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    -- Fraction of the window the sensor was actually available. Scoring
    -- discounts low-confidence windows instead of dropping them, so a
    -- patient who charges their phone overnight does not read as anomalous.
    confidence      REAL        NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    sdk_version     TEXT,
    PRIMARY KEY (patient_id, observed_at, feature)
);

SELECT create_hypertable(
    'behavioral_samples',
    'observed_at',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS behavioral_samples_feature_idx
    ON behavioral_samples (patient_id, feature, observed_at DESC);

-- Voice biomarker vectors arrive far less often than behavioral samples
-- because the sample is optional, so they get their own hypertable with a
-- wider chunk interval.
CREATE TABLE IF NOT EXISTS voice_samples (
    patient_id      UUID        NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL,
    feature         TEXT        NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    sample_seconds  REAL        NOT NULL,
    extractor       TEXT        NOT NULL DEFAULT 'opensmile/eGeMAPSv02',
    PRIMARY KEY (patient_id, observed_at, feature)
);

SELECT create_hypertable(
    'voice_samples',
    'observed_at',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

-- Daily rollup drives the 30-day trajectory line. Reading the raw hypertable
-- for a 40-patient panel on every dashboard load is the obvious way to make
-- the dashboard slow, so the chart reads this view instead.
CREATE MATERIALIZED VIEW IF NOT EXISTS behavioral_daily
WITH (timescaledb.continuous) AS
SELECT
    patient_id,
    feature,
    time_bucket(INTERVAL '1 day', observed_at) AS day,
    avg(value)                                  AS mean_value,
    stddev_samp(value)                          AS sd_value,
    min(value)                                  AS min_value,
    max(value)                                  AS max_value,
    avg(confidence)                             AS mean_confidence,
    count(*)                                    AS sample_count
FROM behavioral_samples
GROUP BY patient_id, feature, day
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'behavioral_daily',
    start_offset => INTERVAL '45 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- Trajectory scores produced by the scoring service. Kept separate from raw
-- samples because they are recomputed when a baseline is rebuilt.
CREATE TABLE IF NOT EXISTS trajectory_scores (
    patient_id      UUID        NOT NULL,
    scored_at       TIMESTAMPTZ NOT NULL,
    risk_score      DOUBLE PRECISION NOT NULL CHECK (risk_score >= 0 AND risk_score <= 100),
    baseline_score  DOUBLE PRECISION NOT NULL,
    deviation       DOUBLE PRECISION NOT NULL,
    dominant_signal TEXT,
    model_version   TEXT NOT NULL,
    PRIMARY KEY (patient_id, scored_at)
);

SELECT create_hypertable(
    'trajectory_scores',
    'scored_at',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists => TRUE
);

-- Raw sample resolution stops being useful once a score exists for the
-- window. Compression after 30 days, drop after two years.
ALTER TABLE behavioral_samples SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'patient_id, feature'
);

SELECT add_compression_policy('behavioral_samples', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('behavioral_samples', INTERVAL '730 days', if_not_exists => TRUE);
