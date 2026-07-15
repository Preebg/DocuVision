"""Strict extraction JSON Schema — local-only, schema-registry compatible."""

from __future__ import annotations

import json
from typing import Any

import jsonschema


class ValidationError(Exception):
    """Raised when LLM extraction JSON fails schema verification."""

    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


# Stable pipeline envelope after SchemaRegistry.normalize_extraction.
# Accepts both modern class-routed payloads and legacy medical top-level keys.
EXTRACTION_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "class_id": {"type": "string", "minLength": 1},
        "form_type": {"type": "string", "minLength": 1},
        "display_name": {"type": "string"},
        "patient_name": {"type": "string"},
        "dob": {"type": "string"},
        "extracted_payload": {
            "type": "object",
            "properties": {
                "full_text": {"type": "string"},
                "fields": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["full_text", "fields", "summary"],
            "additionalProperties": True,
        },
    },
    "required": ["form_type", "extracted_payload"],
    "additionalProperties": True,
}


def parse_json_response(ai_text: str) -> dict[str, Any]:
    try:
        return json.loads(ai_text)
    except json.JSONDecodeError:
        start = ai_text.find("{")
        end = ai_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(ai_text[start : end + 1])


def validate_extraction_payload(parsed_json: dict[str, Any]) -> dict[str, Any]:
    try:
        jsonschema.validate(instance=parsed_json, schema=EXTRACTION_JSON_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ValidationError(
            f"Extraction payload failed schema verification: {exc.message}",
            cause=exc,
        ) from exc
    return parsed_json


def is_empty_extraction(parsed_json: dict[str, Any]) -> bool:
    if not parsed_json:
        return True

    payload = parsed_json.get("extracted_payload", parsed_json)
    if isinstance(payload, str):
        return not payload.strip()

    if isinstance(payload, dict):
        text_values = [
            str(value).strip()
            for value in payload.values()
            if isinstance(value, (str, int, float))
        ]
        fields = payload.get("fields")
        if isinstance(fields, dict):
            for value in fields.values():
                if isinstance(value, (str, int, float)):
                    text_values.append(str(value).strip())
                elif isinstance(value, dict) and "value" in value:
                    # Rich field objects: {value, bbox_2d, confidence, ...}
                    inner = value.get("value")
                    if isinstance(inner, (str, int, float)):
                        text_values.append(str(inner).strip())
        return not any(text_values)

    return False
