"""
Milestone 1 unit tests — no live Ollama or Supabase network calls.
"""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from config import ValidationError
from observability import (
    JsonFormatter,
    configure_structured_logging,
    derive_correlation_id,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from schema_validation import parse_json_response, validate_extraction_payload
from temp_files import create_ingestion_temp_file, safe_remove, sanitize_file_stem


VALID_EXTRACTION = {
    "patient_name": "Jane Doe",
    "dob": "1985-04-12",
    "form_type": "intake",
    "extracted_payload": {
        "full_text": "Patient Jane Doe DOB 1985-04-12",
        "fields": {"Applicant Name": "Jane Doe"},
        "summary": "Intake form for Jane Doe",
    },
}


@pytest.fixture(autouse=True)
def _reset_correlation_context():
    reset_correlation_id()
    yield
    reset_correlation_id()


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


def test_json_log_formatter_injects_correlation_id():
    configure_structured_logging()
    expected_id = derive_correlation_id("intake_form.pdf")
    set_correlation_id(expected_id)

    record = logging.LogRecord(
        name="docuvision.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ingestion started",
        args=(),
        exc_info=None,
    )
    # Mimic CorrelationIdFilter injection
    record.correlation_id = get_correlation_id()

    formatted = JsonFormatter().format(record)
    payload = json.loads(formatted)

    assert payload["correlation_id"] == expected_id
    assert payload["message"] == "ingestion started"
    assert payload["level"] == "INFO"
    assert "timestamp" in payload


def test_json_log_formatter_defaults_correlation_id_when_unset():
    reset_correlation_id()
    record = logging.LogRecord(
        name="docuvision.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="orphan event",
        args=(),
        exc_info=None,
    )
    formatted = JsonFormatter().format(record)
    payload = json.loads(formatted)

    assert payload["correlation_id"]
    # UUID-shaped string
    assert len(payload["correlation_id"]) == 36


def test_derive_correlation_id_is_stable_for_same_file_name():
    first = derive_correlation_id("claim_123.pdf")
    second = derive_correlation_id("claim_123.pdf")
    assert first == second
    assert first != derive_correlation_id("other.pdf")


# ---------------------------------------------------------------------------
# Safe temporary files
# ---------------------------------------------------------------------------


def test_temp_file_utility_creates_and_destroys():
    path = create_ingestion_temp_file("patient_form.pdf")
    try:
        assert os.path.isabs(path) or os.path.dirname(path) != ""
        assert os.path.exists(path)
        assert path.endswith(".pdf")
        assert "docuvision_" in os.path.basename(path)
        assert "./temp_" not in path

        with open(path, "wb") as handle:
            handle.write(b"%PDF-1.4 mock")

        assert os.path.getsize(path) > 0
    finally:
        safe_remove(path)

    assert not os.path.exists(path)


def test_temp_file_utility_sanitizes_path_traversal_names():
    path = create_ingestion_temp_file("../../etc/passwd\x00evil.pdf")
    try:
        basename = os.path.basename(path)
        assert ".." not in basename
        assert "\x00" not in basename
        assert basename.startswith("docuvision_")
    finally:
        safe_remove(path)


def test_sanitize_file_stem_strips_directories():
    assert sanitize_file_stem(r"..\..\secret\file.pdf") == "file.pdf"
    assert sanitize_file_stem("plain_name.jpg") == "plain_name.jpg"


# ---------------------------------------------------------------------------
# JSON schema validation
# ---------------------------------------------------------------------------


def test_schema_validator_accepts_well_formed_payload():
    assert validate_extraction_payload(VALID_EXTRACTION) is VALID_EXTRACTION


def test_schema_validator_rejects_missing_mandatory_fields():
    malformed = {
        "patient_name": "Jane Doe",
        # dob missing
        "form_type": "intake",
        "extracted_payload": {
            "full_text": "text",
            "fields": {},
            "summary": "summary",
        },
    }
    with pytest.raises(ValidationError) as exc_info:
        validate_extraction_payload(malformed)

    assert "schema verification" in str(exc_info.value).lower()


def test_schema_validator_rejects_wrong_types():
    malformed = {
        "patient_name": 12345,  # must be string
        "dob": "1985-04-12",
        "form_type": "intake",
        "extracted_payload": {
            "full_text": "text",
            "fields": {},
            "summary": "summary",
        },
    }
    with pytest.raises(ValidationError):
        validate_extraction_payload(malformed)


def test_parse_json_response_then_validate_rejects_llm_garbage():
    raw = '{"patient_name": "Only Name"}'
    parsed = parse_json_response(raw)
    with pytest.raises(ValidationError):
        validate_extraction_payload(parsed)


# ---------------------------------------------------------------------------
# Pipeline runner — mocked remote I/O (no live Ollama / Supabase)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_document_does_not_call_live_networks():
    mocked_payload = dict(VALID_EXTRACTION)

    with (
        patch("watcher.download_incoming_file") as mock_download,
        patch(
            "watcher.step1_extract_layout",
            return_value="# layout",
        ) as mock_layout,
        patch(
            "watcher.step2_and_3_vision_extraction",
            return_value=mocked_payload,
        ) as mock_vision,
        patch("watcher.stage_file_for_verification") as mock_stage,
        patch("watcher.record_pipeline_success") as mock_success,
        patch("watcher.remove_processed_file") as mock_remove,
        patch("watcher.get_supabase") as mock_supabase,
    ):
        mock_supabase.side_effect = AssertionError(
            "get_supabase must not be called when I/O helpers are mocked"
        )

        from watcher import process_document

        await process_document("intake_jane.pdf")

        mock_download.assert_called_once()
        mock_layout.assert_called_once()
        mock_vision.assert_called_once()
        mock_stage.assert_called_once()
        mock_success.assert_called_once_with("intake_jane.pdf", mocked_payload)
        mock_remove.assert_called_once_with("intake_jane.pdf")


@pytest.mark.asyncio
async def test_process_document_cleans_temp_file_on_pipeline_crash():
    created_paths: list[str] = []

    def _tracking_create(file_name: str, suffix: str | None = None) -> str:
        path = create_ingestion_temp_file(file_name, suffix=suffix)
        created_paths.append(path)
        return path

    with (
        patch("watcher.create_ingestion_temp_file", side_effect=_tracking_create),
        patch("watcher.download_incoming_file"),
        patch(
            "watcher.step1_extract_layout",
            side_effect=RuntimeError("Docling exploded"),
        ),
        patch("watcher.record_pipeline_failure") as mock_failure,
        patch("watcher.record_pipeline_success") as mock_success,
        patch("watcher.remove_processed_file") as mock_remove,
    ):
        from watcher import process_document

        await process_document("broken.pdf")

        mock_failure.assert_called_once()
        mock_success.assert_not_called()
        mock_remove.assert_not_called()

    assert created_paths
    for path in created_paths:
        assert not os.path.exists(path)


def test_call_ollama_chat_is_mockable_without_network():
    from ollama_client import call_ollama_chat

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "message": {"content": json.dumps(VALID_EXTRACTION)}
    }

    with patch("ollama_client.requests.post", return_value=mock_response) as mock_post:
        content = call_ollama_chat(
            "mock-model",
            [{"role": "user", "content": "extract"}],
        )
        mock_post.assert_called_once()
        assert "patient_name" in content
        # Ensure we never pointed at a real host in this test path
        called_url = mock_post.call_args.args[0]
        assert called_url.endswith("/api/chat")
