-- DocuVision batch + page domain (Migration 002)
-- Dual-engine compatible: PostgreSQL 12+ and SQLite 3.35+
--
-- Type mapping matches 001 (LCD):
--   UUID / Text PK  → TEXT holding canonical UUID strings
--   TIMESTAMPTZ     → TEXT ISO-8601 UTC timestamps, DEFAULT CURRENT_TIMESTAMP
--
-- Batch status values must match contracts/batch-status.json
--   ingested | processing | ready_for_hitl | completed | failed
--
-- Priority: 1=Low, 2=Med (default), 3=High

CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 2,
    status      TEXT NOT NULL DEFAULT 'ingested',
    source_id   TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (priority IN (1, 2, 3))
);

CREATE TABLE IF NOT EXISTS batch_documents (
    batch_id    TEXT NOT NULL,
    document_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    PRIMARY KEY (batch_id, document_id),
    FOREIGN KEY (batch_id) REFERENCES batches (id) ON DELETE CASCADE,
    FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS document_pages (
    id          TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    page_no     INTEGER NOT NULL,
    file_path   TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE,
    UNIQUE (document_id, page_no)
);

CREATE INDEX IF NOT EXISTS idx_batches_status
    ON batches (status);

CREATE INDEX IF NOT EXISTS idx_batches_priority_created
    ON batches (priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_batch_documents_document_id
    ON batch_documents (document_id);

CREATE INDEX IF NOT EXISTS idx_batch_documents_sequence
    ON batch_documents (batch_id, sequence_no);

CREATE INDEX IF NOT EXISTS idx_document_pages_document_page
    ON document_pages (document_id, page_no);
