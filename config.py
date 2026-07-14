import base64
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import tomllib
from supabase import Client, create_client


class ValidationError(Exception):
    """Raised when LLM extraction JSON fails schema verification."""

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


def get_supabase_key_role(key: str) -> str:
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("role", "unknown")
    except Exception:
        return "unknown"


def load_supabase_credentials() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")

    if not url or not key:
        secrets_path = Path(__file__).with_name("secrets.toml")
        if secrets_path.exists():
            with secrets_path.open("rb") as secrets_file:
                secrets = tomllib.load(secrets_file)
            url = url or secrets.get("SUPABASE_URL")
            key = key or secrets.get("SUPABASE_KEY")

    if not url or not key:
        raise SystemExit(
            "Missing Supabase credentials. Set SUPABASE_URL and SUPABASE_KEY "
            "as environment variables or add them to secrets.toml."
        )

    return url, key


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """Lazy Supabase client — avoids network/credential import-time side effects."""
    url, key = load_supabase_credentials()
    return create_client(url, key)


def get_supabase_url() -> str:
    url, _ = load_supabase_credentials()
    return url


def get_supabase_key_role_cached() -> str:
    _, key = load_supabase_credentials()
    return get_supabase_key_role(key)


# Backwards-compatible aliases (resolved on first attribute access via __getattr__).
def __getattr__(name: str) -> Any:
    if name == "supabase":
        return get_supabase()
    if name == "SUPABASE_URL":
        return get_supabase_url()
    if name == "SUPABASE_KEY":
        _, key = load_supabase_credentials()
        return key
    if name == "SUPABASE_KEY_ROLE":
        return get_supabase_key_role_cached()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


PRIMARY_MODEL = os.getenv("OLLAMA_PRIMARY_MODEL", "qwen3-vl:8b-thinking")
FALLBACK_MODEL = os.getenv("OLLAMA_FALLBACK_MODEL", "qwen2.5vl:3b")
# Aligned with contracts/pipeline-status.json (QUEUED replaces legacy IMPORTED).
PIPELINE_STATUS_QUEUED = "QUEUED"
PIPELINE_STATUS_IMPORTED = PIPELINE_STATUS_QUEUED  # backwards-compatible alias
PIPELINE_STATUS_SCANNING = "SCANNING"
PIPELINE_STATUS_PENDING_VERIFICATION = "PENDING_VERIFICATION"
PIPELINE_STATUS_VERIFIED = "VERIFIED"
PIPELINE_STATUS_COMPLETED = "COMPLETED"
PIPELINE_STATUS_FAILED = "FAILED"
INCOMING_STORAGE_BUCKET = "incoming_forms"
VERIFICATION_STORAGE_BUCKET = "verification_queue"

# Strict contract for LLM extraction payloads before any DB write.
EXTRACTION_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "patient_name": {"type": "string", "minLength": 1},
        "dob": {"type": "string", "minLength": 1},
        "form_type": {"type": "string", "minLength": 1},
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
    "required": ["patient_name", "dob", "form_type", "extracted_payload"],
    "additionalProperties": False,
}

OLLAMA_INFERENCE_OPTIONS = {
    "temperature": 0.0,
    "top_p": 0.1,
}


def get_ollama_config() -> dict[str, Any]:
    return {
        "url": os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        "timeout": int(os.getenv("OLLAMA_TIMEOUT", "300")),
        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192")),
        "image_max_dimension": int(os.getenv("OLLAMA_IMAGE_MAX_DIMENSION", "1280")),
        "json_mode": os.getenv("OLLAMA_JSON_MODE", "true").lower() in {"1", "true", "yes"},
        "warmup": os.getenv("OLLAMA_WARMUP", "true").lower() in {"1", "true", "yes"},
        "layout_max_chars": int(os.getenv("DOCLING_LAYOUT_MAX_CHARS", "8000")),
    }
