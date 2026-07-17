"""Typed XML and SQL serializers for verified document egress."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from docuvision.core.errors import ReleaseSerializationError


class ExtractedField(BaseModel):
    """Normalized scalar field emitted by the extraction pipeline."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1)
    value: Any = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    page: int | None = Field(default=None, ge=1)


class ExtractedCell(BaseModel):
    """One normalized cell in an extracted table."""

    model_config = ConfigDict(frozen=True)

    table_index: int = Field(ge=0)
    row_index: int = Field(ge=0)
    col_index: int = Field(ge=0)
    value: Any = None


class EgressDocument(BaseModel):
    """Stable, validated document contract shared by all serializers."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    status: str = Field(min_length=1)
    operator: str | None = None
    timestamp: datetime
    fields: tuple[ExtractedField, ...] = ()
    cells: tuple[ExtractedCell, ...] = ()


class EgressManager:
    """Factory/normalizer coordinating all document serializers."""

    def __init__(
        self,
        *,
        xml_exporter: XMLExporter | None = None,
        sql_exporter: SQLExporter | None = None,
    ) -> None:
        self.xml_exporter = xml_exporter or XMLExporter()
        self.sql_exporter = sql_exporter or SQLExporter()

    def normalize(self, document: dict[str, Any]) -> EgressDocument:
        document_id = str(document.get("document_id") or "").strip()
        filename = str(document.get("file_name") or "").strip()
        payload = _effective_payload(document)
        raw_fields = document.get("fields")
        if not isinstance(raw_fields, dict):
            raw_fields = payload.get("fields")
        if not isinstance(raw_fields, dict):
            raw_fields = {}

        raw_tables = payload.get("tables")
        if raw_tables is None:
            envelope = _effective_envelope(document)
            raw_tables = envelope.get("tables")

        try:
            return EgressDocument(
                document_id=document_id,
                filename=filename,
                status=str(document.get("status") or "VERIFIED"),
                operator=_optional_text(document.get("verified_by")),
                timestamp=document.get("timestamp") or datetime.now(timezone.utc),
                fields=tuple(_normalize_fields(raw_fields)),
                cells=tuple(_normalize_tables(raw_tables)),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise ReleaseSerializationError(
                "schema",
                document_id or filename or "<unknown>",
                f"invalid egress payload: {exc}",
            ) from exc

    def serialize(self, document: dict[str, Any]) -> tuple[bytes, str]:
        normalized = self.normalize(document)
        return (
            self.xml_exporter.serialize(normalized),
            self.sql_exporter.serialize(normalized),
        )


class XMLExporter:
    """Serialize a validated document to deterministic UTF-8 XML."""

    def serialize(self, document: EgressDocument) -> bytes:
        try:
            root = ET.Element("document")
            metadata = ET.SubElement(root, "metadata")
            _text_element(metadata, "id", document.document_id)
            _text_element(metadata, "filename", document.filename)
            _text_element(metadata, "status", document.status)
            _text_element(metadata, "operator", document.operator)
            _text_element(metadata, "timestamp", _iso_utc(document.timestamp))

            fields = ET.SubElement(root, "extracted_fields")
            for item in document.fields:
                attributes = {"key": item.key}
                if item.confidence_score is not None:
                    attributes["confidence_score"] = _number_text(
                        item.confidence_score
                    )
                if item.page is not None:
                    attributes["page"] = str(item.page)
                node = ET.SubElement(fields, "field", attributes)
                _text_element(node, "value", _value_text(item.value))

            tables = ET.SubElement(root, "tables")
            grouped: dict[int, list[ExtractedCell]] = {}
            for cell in document.cells:
                grouped.setdefault(cell.table_index, []).append(cell)
            for table_index in sorted(grouped):
                table = ET.SubElement(
                    tables, "table", {"index": str(table_index)}
                )
                rows: dict[int, list[ExtractedCell]] = {}
                for cell in grouped[table_index]:
                    rows.setdefault(cell.row_index, []).append(cell)
                for row_index in sorted(rows):
                    row = ET.SubElement(table, "row", {"index": str(row_index)})
                    for cell in sorted(rows[row_index], key=lambda value: value.col_index):
                        node = ET.SubElement(
                            row, "cell", {"col_index": str(cell.col_index)}
                        )
                        text = _value_text(cell.value)
                        if text is not None:
                            node.text = text

            ET.indent(root, space="  ")
            body = ET.tostring(
                root,
                encoding="utf-8",
                xml_declaration=True,
                short_empty_elements=True,
            )
            # Parse our output before release so illegal XML characters or a
            # malformed serializer change cannot reach the drop directory.
            parsed = ET.fromstring(body)
            _validate_xml_shape(parsed)
            return body + b"\n"
        except ReleaseSerializationError:
            raise
        except (ET.ParseError, TypeError, ValueError) as exc:
            raise ReleaseSerializationError(
                "xml", document.document_id, str(exc)
            ) from exc


class SQLExporter:
    """Render executable, transaction-wrapped relational INSERT statements."""

    _DOCUMENT_INSERT = (
        "INSERT INTO documents (id, filename, status, operator, timestamp)\n"
        "VALUES ({id}, {filename}, {status}, {operator}, {timestamp});"
    )
    _FIELD_INSERT = (
        "INSERT INTO extracted_metadata "
        "(document_id, field_key, field_value, confidence_score)\n"
        "VALUES ({document_id}, {field_key}, {field_value}, {confidence_score});"
    )
    _CELL_INSERT = (
        "INSERT INTO extracted_tables "
        "(document_id, table_index, row_index, col_index, cell_value)\n"
        "VALUES ({document_id}, {table_index}, {row_index}, {col_index}, "
        "{cell_value});"
    )

    def serialize(self, document: EgressDocument) -> str:
        try:
            document_id = _sql_literal(document.document_id)
            statements = [
                "BEGIN TRANSACTION;",
                self._DOCUMENT_INSERT.format(
                    id=document_id,
                    filename=_sql_literal(document.filename),
                    status=_sql_literal(document.status),
                    operator=_sql_literal(document.operator),
                    timestamp=_sql_literal(_iso_utc(document.timestamp)),
                ),
            ]
            statements.extend(
                self._FIELD_INSERT.format(
                    document_id=document_id,
                    field_key=_sql_literal(item.key),
                    field_value=_sql_literal(_value_text(item.value)),
                    confidence_score=_sql_literal(item.confidence_score),
                )
                for item in document.fields
            )
            statements.extend(
                self._CELL_INSERT.format(
                    document_id=document_id,
                    table_index=cell.table_index,
                    row_index=cell.row_index,
                    col_index=cell.col_index,
                    cell_value=_sql_literal(_value_text(cell.value)),
                )
                for cell in document.cells
            )
            statements.append("COMMIT;")
            return "\n\n".join(statements) + "\n"
        except (TypeError, ValueError) as exc:
            raise ReleaseSerializationError(
                "sql", document.document_id, str(exc)
            ) from exc


def _effective_envelope(document: dict[str, Any]) -> dict[str, Any]:
    payload = document.get("verified_payload")
    if not isinstance(payload, dict):
        payload = document.get("extracted_payload")
    return payload if isinstance(payload, dict) else {}


def _effective_payload(document: dict[str, Any]) -> dict[str, Any]:
    envelope = _effective_envelope(document)
    nested = envelope.get("extracted_payload")
    return nested if isinstance(nested, dict) else envelope


def _normalize_fields(raw_fields: dict[str, Any]) -> Iterable[ExtractedField]:
    for key in sorted(raw_fields, key=str):
        raw = raw_fields[key]
        if isinstance(raw, dict) and "value" in raw:
            value = raw.get("value")
            confidence = raw.get("confidence")
            page = raw.get("page")
        else:
            value = raw
            confidence = None
            page = None
        yield ExtractedField(
            key=str(key),
            value=value,
            confidence_score=confidence,
            page=page,
        )


def _normalize_tables(raw_tables: Any) -> Iterable[ExtractedCell]:
    if raw_tables is None:
        return
    tables = raw_tables if isinstance(raw_tables, list) else [raw_tables]
    for table_index, table in enumerate(tables):
        if isinstance(table, dict) and isinstance(table.get("cells"), list):
            for cell in table["cells"]:
                if not isinstance(cell, dict):
                    raise ValueError(f"table {table_index} contains a non-object cell")
                yield ExtractedCell(
                    table_index=int(cell.get("table_index", table_index)),
                    row_index=int(cell.get("row_index", cell.get("row", 0))),
                    col_index=int(cell.get("col_index", cell.get("col", 0))),
                    value=cell.get("value", cell.get("cell_value")),
                )
            continue

        rows = table.get("rows") if isinstance(table, dict) else table
        if not isinstance(rows, list):
            raise ValueError(f"table {table_index} must contain a rows list")
        for row_index, row in enumerate(rows):
            cells = row.get("cells") if isinstance(row, dict) else row
            if not isinstance(cells, list):
                raise ValueError(
                    f"table {table_index} row {row_index} must be a cell list"
                )
            for col_index, value in enumerate(cells):
                if isinstance(value, dict) and "value" in value:
                    value = value["value"]
                yield ExtractedCell(
                    table_index=table_index,
                    row_index=row_index,
                    col_index=col_index,
                    value=value,
                )


def _text_element(parent: ET.Element, tag: str, value: str | None) -> ET.Element:
    node = ET.SubElement(parent, tag)
    if value is not None:
        node.text = value
    return node


def _validate_xml_shape(root: ET.Element) -> None:
    if root.tag != "document":
        raise ValueError("XML root must be <document>")
    required = {"metadata", "extracted_fields", "tables"}
    actual = {child.tag for child in root}
    missing = required - actual
    if missing:
        raise ValueError(f"XML is missing required sections: {sorted(missing)}")
    metadata = root.find("metadata")
    required_metadata = {"id", "filename", "status", "operator", "timestamp"}
    if metadata is None or required_metadata - {child.tag for child in metadata}:
        raise ValueError("XML metadata does not match the egress schema")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _value_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, float):
        return _number_text(value)
    return str(value)


def _number_text(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("non-finite numbers cannot be serialized")
    return format(value, ".15g")


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number_text(value)
    return "'" + str(value).replace("'", "''") + "'"


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "EgressDocument",
    "EgressManager",
    "ExtractedCell",
    "ExtractedField",
    "SQLExporter",
    "XMLExporter",
]
