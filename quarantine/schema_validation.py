"""Strict JSON Schema verification for LLM extraction payloads."""

from __future__ import annotations

import json

import jsonschema

from config import EXTRACTION_JSON_SCHEMA, ValidationError


def parse_json_response(ai_text: str) -> dict:
    try:
        return json.loads(ai_text)
    except json.JSONDecodeError:
        start = ai_text.find("{")
        end = ai_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(ai_text[start : end + 1])


def validate_extraction_payload(parsed_json: dict) -> dict:
    """
    Run strict jsonschema validation after raw LLM parse.
    Raises config.ValidationError on any schema mismatch.
    """
    try:
        jsonschema.validate(instance=parsed_json, schema=EXTRACTION_JSON_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ValidationError(
            f"Extraction payload failed schema verification: {exc.message}",
            cause=exc,
        ) from exc
    return parsed_json
