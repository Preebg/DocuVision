import base64
import json
import os
from pathlib import Path

import tomllib
from supabase import Client, create_client


def get_supabase_key_role(key: str) -> str:
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("role", "unknown")
    except Exception:
        return "unknown"


def load_supabase_credentials():
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


SUPABASE_URL, SUPABASE_KEY = load_supabase_credentials()
SUPABASE_KEY_ROLE = get_supabase_key_role(SUPABASE_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

PRIMARY_MODEL = os.getenv("OLLAMA_PRIMARY_MODEL", "qwen3-vl:8b-thinking")
FALLBACK_MODEL = os.getenv("OLLAMA_FALLBACK_MODEL", "qwen2.5vl:3b")
PIPELINE_STATUS_PENDING_VERIFICATION = "PENDING_VERIFICATION"
PIPELINE_STATUS_COMPLETED = "COMPLETED"
PIPELINE_STATUS_FAILED = "FAILED"
VERIFICATION_STORAGE_BUCKET = "verification_queue"

EXTRACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "extracted_payload": {
            "type": "object",
            "properties": {
                "full_text": {"type": "string"},
                "fields": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["full_text", "fields", "summary"],
        }
    },
    "required": ["extracted_payload"],
}

OLLAMA_INFERENCE_OPTIONS = {
    "temperature": 0.0,
    "top_p": 0.1,
}


def get_ollama_config():
    return {
        "url": os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        "timeout": int(os.getenv("OLLAMA_TIMEOUT", "300")),
        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192")),
        "image_max_dimension": int(os.getenv("OLLAMA_IMAGE_MAX_DIMENSION", "1280")),
        "json_mode": os.getenv("OLLAMA_JSON_MODE", "true").lower() in {"1", "true", "yes"},
        "warmup": os.getenv("OLLAMA_WARMUP", "true").lower() in {"1", "true", "yes"},
        "layout_max_chars": int(os.getenv("DOCLING_LAYOUT_MAX_CHARS", "8000")),
    }
