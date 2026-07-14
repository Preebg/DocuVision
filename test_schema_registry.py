"""Unit tests for Pillar 3 schema registry + keyword classification."""

from __future__ import annotations

from pathlib import Path

import pytest

from docuvision.registry.schema_registry import (
    SchemaRegistry,
    schemas_to_typescript,
)
from docuvision.stages.classification import ClassificationStage


REPO_SCHEMAS = Path(__file__).resolve().parent / "config" / "schemas"


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry(REPO_SCHEMAS)


def test_registry_loads_builtin_schemas(registry: SchemaRegistry) -> None:
    ids = set(registry.list_class_ids())
    assert "commercial_invoice" in ids
    assert "medical_intake" in ids
    assert "unclassified" in ids
    assert registry.fallback.class_id == "unclassified"


def test_keyword_routing_invoice(registry: SchemaRegistry) -> None:
    text = "INVOICE\nVendor: Acme Supplies\nInvoice Number: INV-100\nAmount Due: $40.00"
    result = registry.classify_by_keywords(text)
    assert result is not None
    assert result.class_id == "commercial_invoice"
    assert result.method == "keyword"
    assert "invoice" in result.matched_keywords


def test_keyword_routing_medical(registry: SchemaRegistry) -> None:
    text = "Patient Intake Form\nPatient: Jane Doe\nDate of Birth: 1990-01-01\nProvider: Dr. Smith"
    result = registry.classify_by_keywords(text)
    assert result is not None
    assert result.class_id == "medical_intake"


def test_classify_fallback_when_empty(registry: SchemaRegistry) -> None:
    stage = ClassificationStage(registry, client=None, use_llm_fallback=False)
    result = stage.classify("")
    assert result.class_id == "unclassified"
    assert result.method == "fallback"


def test_normalize_extraction_matches_schema_keys(registry: SchemaRegistry) -> None:
    schema = registry.get("commercial_invoice")
    envelope = registry.normalize_extraction(
        schema,
        {
            "fields": {
                "invoice_number": "INV-9",
                "vendor": "Acme",
                "total_amount": "$12.00",
            },
            "full_text": "full",
            "summary": "an invoice",
        },
    )
    assert envelope["class_id"] == "commercial_invoice"
    assert envelope["form_type"] == "commercial_invoice"
    assert envelope["extracted_payload"]["fields"]["invoice_number"] == "INV-9"


def test_extraction_json_schema_is_class_scoped(registry: SchemaRegistry) -> None:
    schema = registry.get("medical_intake")
    js = registry.build_extraction_json_schema(schema)
    props = js["properties"]["fields"]["properties"]
    assert set(props) == {"patient_name", "dob", "provider"}
    assert "invoice_number" not in props


def test_typescript_codegen_contains_class_ids(registry: SchemaRegistry) -> None:
    ts = schemas_to_typescript(registry.list_schemas())
    assert "export type DocumentClassId" in ts
    assert '"commercial_invoice"' in ts
    assert "invoice_number" in ts
    assert "VERIFICATION_FIELD_KEYS" in ts
