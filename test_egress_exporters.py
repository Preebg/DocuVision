"""Unit and integration tests for XML/SQL release egress."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import fitz
import pytest

from docuvision.adapters.release import EgressManager, FSDropExporter
from docuvision.core.errors import ReleaseSerializationError


def _pdf(path: Path) -> Path:
    document = fitz.open()
    document.new_page().insert_text((72, 72), "egress")
    path.write_bytes(document.tobytes())
    document.close()
    return path


def _sample(source: Path) -> dict:
    return {
        "document_id": "doc-'42",
        "file_name": "patient & intake.pdf",
        "source_path": str(source),
        "status": "VERIFIED",
        "timestamp": datetime(2026, 7, 16, 20, 30, tzinfo=timezone.utc),
        "fields": {
            "empty": {"value": None, "confidence": None},
            "patient_name": {
                "value": "O'Brien & Sons <Clinic>",
                "confidence": 0.975,
                "page": 1,
            },
        },
        "verified_payload": {
            "extracted_payload": {
                "tables": [
                    {
                        "rows": [
                            ["Medication", "Dose"],
                            ["Children's Aspirin", None],
                        ]
                    }
                ]
            }
        },
    }


def test_egress_manager_generates_valid_xml_and_executable_sql(
    tmp_path: Path,
) -> None:
    manager = EgressManager()
    xml_bytes, sql = manager.serialize(_sample(_pdf(tmp_path / "source.pdf")))

    root = ET.fromstring(xml_bytes)
    assert root.findtext("./metadata/filename") == "patient & intake.pdf"
    assert root.find("./metadata/operator") is not None
    patient = root.find("./extracted_fields/field[@key='patient_name']")
    assert patient is not None
    assert patient.findtext("value") == "O'Brien & Sons <Clinic>"
    assert patient.attrib["confidence_score"] == "0.975"
    assert root.findtext("./tables/table/row/cell") == "Medication"

    assert sql.startswith("BEGIN TRANSACTION;")
    assert sql.rstrip().endswith("COMMIT;")
    assert "O''Brien & Sons <Clinic>" in sql
    assert "Children''s Aspirin" in sql
    assert "NULL" in sql

    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE documents (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            operator TEXT,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE extracted_metadata (
            document_id TEXT NOT NULL REFERENCES documents(id),
            field_key TEXT NOT NULL,
            field_value TEXT,
            confidence_score REAL
        );
        CREATE TABLE extracted_tables (
            document_id TEXT NOT NULL REFERENCES documents(id),
            table_index INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            col_index INTEGER NOT NULL,
            cell_value TEXT
        );
        """
    )
    connection.executescript(sql)
    assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert (
        connection.execute("SELECT COUNT(*) FROM extracted_metadata").fetchone()[0]
        == 2
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM extracted_tables").fetchone()[0]
        == 4
    )
    connection.close()


@pytest.mark.asyncio
async def test_fs_drop_publishes_pdf_xml_and_sql_per_document(
    tmp_path: Path,
) -> None:
    source = _pdf(tmp_path / "source.pdf")
    exporter = FSDropExporter(tmp_path / "release")
    payload = {
        "batch": {"name": "Egress"},
        "documents": [_sample(source)],
    }

    assert await exporter.export_batch("batch-42", payload) is True

    package = tmp_path / "release" / "Egress_batch-42"
    assert len(list(package.glob("*.pdf"))) == 1
    assert len(list(package.glob("*.xml"))) == 1
    assert len(list(package.glob("*.sql"))) == 1
    manifest = (package / "checksums.sha256").read_text(encoding="utf-8")
    assert ".pdf" in manifest
    assert ".xml" in manifest
    assert ".sql" in manifest


class _FailingEgressManager:
    def serialize(self, document: dict) -> tuple[bytes, str]:
        raise ReleaseSerializationError(
            "xml", str(document.get("document_id")), "invalid control character"
        )


@pytest.mark.asyncio
async def test_fs_drop_serialization_failure_leaves_no_partial_package(
    tmp_path: Path,
) -> None:
    release_root = tmp_path / "release"
    existing = release_root / "Egress_batch-42"
    existing.mkdir(parents=True)
    (existing / "previous.complete").write_text("preserved", encoding="utf-8")
    exporter = FSDropExporter(
        release_root,
        egress_manager=_FailingEgressManager(),  # type: ignore[arg-type]
    )

    with pytest.raises(ReleaseSerializationError, match="XML serialization failed"):
        await exporter.export_batch(
            "batch-42",
            {
                "batch": {"name": "Egress"},
                "documents": [_sample(_pdf(tmp_path / "source.pdf"))],
            },
        )

    assert (existing / "previous.complete").read_text(encoding="utf-8") == "preserved"
    assert not list(release_root.glob("*.staging"))
