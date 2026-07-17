-- Idempotent child-document lineage for physical batch separators.
CREATE TABLE IF NOT EXISTS document_split_lineage (
    parent_document_id TEXT NOT NULL,
    child_document_id  TEXT NOT NULL,
    batch_id           TEXT NOT NULL,
    part_index         INTEGER NOT NULL,
    part_count         INTEGER NOT NULL,
    source_start_page  INTEGER NOT NULL,
    source_end_page    INTEGER NOT NULL,
    strategy           TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (parent_document_id, batch_id, part_index),
    UNIQUE (child_document_id),
    FOREIGN KEY (parent_document_id) REFERENCES documents (id),
    FOREIGN KEY (child_document_id) REFERENCES documents (id) ON DELETE CASCADE,
    FOREIGN KEY (batch_id) REFERENCES batches (id) ON DELETE CASCADE,
    CHECK (part_index >= 1),
    CHECK (part_count >= 1),
    CHECK (source_start_page >= 1),
    CHECK (source_end_page >= source_start_page)
);

CREATE INDEX IF NOT EXISTS idx_split_lineage_parent
    ON document_split_lineage (parent_document_id, batch_id, part_index);
