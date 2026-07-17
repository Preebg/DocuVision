-- Crash-recoverable pipeline quarantine attempts.
-- CREATE-only DDL keeps startup migration idempotent on SQLite and PostgreSQL.

CREATE TABLE IF NOT EXISTS quarantine_attempts (
    id                 TEXT PRIMARY KEY,
    document_id        TEXT NOT NULL,
    reason             TEXT NOT NULL,
    error_stack        TEXT,
    failed_stage       TEXT,
    payload_locator    TEXT,
    retry_count        INTEGER NOT NULL DEFAULT 0,
    quarantined_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    redrive_token      TEXT,
    redrive_started_at TEXT,
    redriven_at        TEXT,
    redriven_by        TEXT,
    FOREIGN KEY (document_id) REFERENCES documents (id)
);

CREATE INDEX IF NOT EXISTS idx_quarantine_attempts_document
    ON quarantine_attempts (document_id);

CREATE INDEX IF NOT EXISTS idx_quarantine_attempts_active
    ON quarantine_attempts (redriven_at, quarantined_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_quarantine_attempts_one_active
    ON quarantine_attempts (document_id)
    WHERE redriven_at IS NULL;
