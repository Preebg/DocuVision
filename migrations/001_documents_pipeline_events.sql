-- DocuVision durable job + append-only audit schema
-- Dual-engine compatible: PostgreSQL 12+ and SQLite 3.35+
--
-- Type mapping (least-common-denominator that both engines accept):
--   UUID / Text PK  → TEXT holding canonical UUID strings
--   JSON / JSONB    → TEXT storing UTF-8 JSON (app serializes; Postgres may
--                     ALTER COLUMN … TYPE JSONB USING metadata::jsonb later)
--   TIMESTAMPTZ     → TEXT ISO-8601 UTC timestamps, DEFAULT CURRENT_TIMESTAMP
--                     (Postgres: optionally ALTER to TIMESTAMPTZ USING …::timestamptz)
--
-- Status values must match contracts/pipeline-status.json
--   QUEUED | SCANNING | LAYOUT | VISION | STAGING |
--   PENDING_VERIFICATION | VERIFIED | FAILED

CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_events (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    details     TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES documents (id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_events_job_id
    ON pipeline_events (job_id);

CREATE INDEX IF NOT EXISTS idx_pipeline_events_created_at
    ON pipeline_events (created_at);

-- Optional Postgres hardenings (run only on PostgreSQL; ignored by SQLite tooling
-- that strips/--skips this section, or apply manually):
--   ALTER TABLE documents ALTER COLUMN metadata TYPE JSONB USING metadata::jsonb;
--   ALTER TABLE documents ALTER COLUMN created_at TYPE TIMESTAMPTZ
--       USING created_at::timestamptz;
--   ALTER TABLE pipeline_events ALTER COLUMN details TYPE JSONB
--       USING CASE WHEN details IS NULL THEN NULL ELSE details::jsonb END;
--   ALTER TABLE pipeline_events ALTER COLUMN created_at TYPE TIMESTAMPTZ
--       USING created_at::timestamptz;
