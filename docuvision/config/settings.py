"""Pydantic-settings for air-gapped local paths and Mini PC Ollama endpoint."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from tempfile import gettempdir
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# docuvision/config/settings.py → repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_ROOT = _REPO_ROOT / "data"


class Settings(BaseSettings):
    """On-prem configuration — no cloud credentials.

    Environment variables use the ``DOCUVISION_`` prefix, e.g.:

        DOCUVISION_DATA_ROOT=C:\\Projects\\DocuVision\\data
        DOCUVISION_OLLAMA_BASE_URL=http://192.168.87.219:11434

    Docker compose overrides ``data_root`` to ``/data``. Local Windows
    defaults to ``<repo>/data`` so SQLite and release paths stay air-gapped.
    """

    model_config = SettingsConfigDict(
        env_prefix="DOCUVISION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- local directory layout -------------------------------------------
    data_root: Path = Field(
        default=_DEFAULT_DATA_ROOT,
        description="Root for on-prem document directories.",
    )
    api_host: str = Field(
        default="127.0.0.1",
        description=(
            "FastAPI bind address for ``run_api.py``. "
            "Env: DOCUVISION_API_HOST. Use 0.0.0.0 only inside containers."
        ),
    )
    api_port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="FastAPI bind port. Env: DOCUVISION_API_PORT.",
    )
    incoming_dir: Path | None = Field(
        default=None,
        description="Ingress queue. Defaults to ``{data_root}/incoming``.",
    )
    processed_dir: Path | None = Field(
        default=None,
        description="Successful archive. Defaults to ``{data_root}/processed``.",
    )
    failed_dir: Path | None = Field(
        default=None,
        description="Dead-letter area. Defaults to ``{data_root}/failed``.",
    )
    verification_dir: Path | None = Field(
        default=None,
        description="HITL staging area. Defaults to ``{data_root}/verification``.",
    )
    workspace_dir: Path | None = Field(
        default=None,
        description="Scratch copies during processing. Defaults to ``{data_root}/workspace``.",
    )
    release_dir: Path | None = Field(
        default=None,
        description=(
            "Air-gapped enterprise release drop root (network share mount). "
            "Env: DOCUVISION_RELEASE_DIR. Defaults to ``{data_root}/release``."
        ),
    )

    # -- persistence ------------------------------------------------------
    database_url: str | None = Field(
        default=None,
        description=(
            "Postgres DSN for the pipeline repository. "
            "Env: DOCUVISION_DATABASE_URL. Empty or unset → embedded SQLite."
        ),
    )
    sqlite_path: Path | None = Field(
        default=None,
        description="Job DB path when Postgres is not configured. "
        "Defaults to ``{data_root}/docuvision.db``.",
    )
    api_cors_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000,tauri://localhost",
        description=(
            "Comma-separated CORS origins for the FastAPI SPA host. "
            "Env: DOCUVISION_API_CORS_ORIGINS."
        ),
    )
    local_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LOCAL_API_KEY",
            "DOCUVISION_LOCAL_API_KEY",
            "local_api_key",
        ),
        description=(
            "Static local API handshake key for the desktop shell. "
            "Env: LOCAL_API_KEY or DOCUVISION_LOCAL_API_KEY. "
            "When unset, run_api mints a single-session token into the "
            "per-user token.lock file."
        ),
    )
    operator_auth_db_path: Path | None = Field(
        default=None,
        description="Local operator credential/session database path.",
    )
    operator_signing_key_path: Path | None = Field(
        default=None,
        description="Local Ed25519 private signing key path.",
    )
    operator_jwt_issuer: str = Field(
        default="docuvision://on-prem/operator-auth",
        description="Exact issuer for locally signed operator JWTs.",
    )
    operator_jwt_audience: str = Field(
        default="docuvision-api",
        description="Required operator access-token audience.",
    )
    operator_access_token_seconds: int = Field(default=300, ge=60, le=900)
    operator_refresh_token_seconds: int = Field(
        default=28_800,
        ge=900,
        le=86_400,
    )
    operator_password_min_length: int = Field(default=14, ge=12, le=128)
    operator_max_failed_attempts: int = Field(default=5, ge=3, le=20)
    operator_lockout_seconds: int = Field(default=900, ge=60, le=86_400)
    operator_jwt_leeway_seconds: int = Field(default=30, ge=0, le=300)

    # -- Mini PC Ollama (network-pluggable) -------------------------------
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description=(
            "Ollama HTTP base URL. Point at the Mini PC on the LAN, e.g. "
            "http://192.168.1.50:11434."
        ),
    )
    ollama_timeout_seconds: int = Field(default=300, ge=1)
    ollama_primary_model: str = Field(default="qwen2.5vl:3b")
    ollama_fallback_model: str = Field(default="qwen3-vl:8b-thinking")
    ollama_num_ctx: int = Field(default=8192, ge=512)
    ollama_image_max_dimension: int = Field(default=1280, ge=256)
    ollama_json_mode: bool = Field(default=True)
    ollama_warmup: bool = Field(default=True)
    ollama_pdf_max_pages: int = Field(default=5, ge=1)

    # -- layout / watcher / fault protection -----------------------------
    extraction_backend: Literal["legacy", "gated"] = Field(
        default="legacy",
        description=(
            "Extraction implementation. 'gated' uses isolated local Docling, "
            "GLM-OCR, and quantized Qwen workers."
        ),
    )
    gated_temp_root: Path = Field(
        default_factory=lambda: Path(gettempdir()),
        description="Session scratch root; resolves to /tmp on Linux.",
    )
    gated_layout_timeout_seconds: int = Field(default=900, ge=30)
    gated_model_timeout_seconds: int = Field(default=900, ge=30)
    gated_layout_rss_limit_mb: int = Field(default=4096, ge=256)
    gated_glm_rss_limit_mb: int = Field(default=3072, ge=256)
    gated_qwen_rss_limit_mb: int = Field(default=8192, ge=256)
    gated_pdf_dpi: int = Field(default=150, ge=72, le=600)
    gated_checkbox_density_threshold: float = Field(default=0.12, ge=0.0, le=1.0)
    gated_glm_model: str = Field(default="zai-org/GLM-OCR")
    gated_qwen_model: str = Field(default="Qwen/Qwen3-VL-8B-Instruct")
    gated_qwen_quantization: Literal["4bit", "8bit", "none"] = Field(
        default="4bit"
    )
    layout_max_chars: int = Field(default=8000, ge=500)
    watcher_poll_interval_seconds: int = Field(default=3, ge=1)
    MAX_RETRIES: int = Field(
        default=3,
        ge=0,
        description=(
            "Transient-error retry ceiling before dead-lettering a document. "
            "Env: DOCUVISION_MAX_RETRIES."
        ),
    )

    # -- Pillar 3: semantic schema routing --------------------------------
    schemas_dir: Path | None = Field(
        default=None,
        description=(
            "Directory of semantic schema YAML files. "
            "Env: DOCUVISION_SCHEMAS_DIR. Defaults to ``<repo>/config/schemas``."
        ),
    )
    classification_use_llm: bool = Field(
        default=True,
        description=(
            "When keyword routing is inconclusive, run a lightweight text-only "
            "LLM classify call (classify_router.md). Disable on ultra-low RAM."
        ),
    )

    # -- Pillar 1: advanced local ingestion --------------------------------
    ingestion_sources_path: Path | None = Field(
        default=None,
        description=(
            "YAML registry of autonomous drop sources. "
            "Env: DOCUVISION_INGESTION_SOURCES_PATH. "
            "Defaults to ``<repo>/config/ingestion_sources.yaml``."
        ),
    )
    ingestion_archive_dir: Path | None = Field(
        default=None,
        description=(
            "Internal archive for successfully ingested source files (moved, not copied). "
            "Env: DOCUVISION_INGESTION_ARCHIVE_DIR. "
            "Defaults to ``{data_root}/ingestion_archive``."
        ),
    )
    ingestion_status_stale_seconds: int = Field(
        default=45,
        ge=5,
        description=(
            "API treats the ingestion heartbeat as offline when older than this. "
            "Env: DOCUVISION_INGESTION_STATUS_STALE_SECONDS."
        ),
    )

    @field_validator("ollama_base_url")
    @classmethod
    def _normalize_ollama_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator(
        "database_url",
        mode="before",
    )
    @classmethod
    def _empty_optional_string_to_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def cors_origins_list(self) -> list[str]:
        return [part.strip() for part in self.api_cors_origins.split(",") if part.strip()]

    # -- resolved path helpers (composition root may call ensure_directories)
    def resolved_incoming_dir(self) -> Path:
        return self.incoming_dir or (self.data_root / "incoming")

    def resolved_processed_dir(self) -> Path:
        return self.processed_dir or (self.data_root / "processed")

    def resolved_failed_dir(self) -> Path:
        return self.failed_dir or (self.data_root / "failed")

    def resolved_verification_dir(self) -> Path:
        return self.verification_dir or (self.data_root / "verification")

    def resolved_workspace_dir(self) -> Path:
        return self.workspace_dir or (self.data_root / "workspace")

    def resolved_release_dir(self) -> Path:
        return self.release_dir or (self.data_root / "release")

    def resolved_sqlite_path(self) -> Path:
        return self.sqlite_path or (self.data_root / "docuvision.db")

    def resolved_operator_auth_db_path(self) -> Path:
        return self.operator_auth_db_path or (self.data_root / "auth" / "operators.db")

    def resolved_operator_signing_key_path(self) -> Path:
        return self.operator_signing_key_path or (
            self.data_root / "auth" / "operator-signing-key.pem"
        )

    def resolved_schemas_dir(self) -> Path:
        if self.schemas_dir is not None:
            return self.schemas_dir
        # docuvision/config/settings.py → repo root / config / schemas
        return Path(__file__).resolve().parents[2] / "config" / "schemas"

    def resolved_ingestion_sources_path(self) -> Path:
        if self.ingestion_sources_path is not None:
            return self.ingestion_sources_path
        return Path(__file__).resolve().parents[2] / "config" / "ingestion_sources.yaml"

    def resolved_ingestion_archive_dir(self) -> Path:
        return self.ingestion_archive_dir or (self.data_root / "ingestion_archive")

    def resolved_ingestion_status_path(self) -> Path:
        return self.data_root / "ingestion" / "status.json"

    def ensure_directories(self) -> None:
        """Create the on-prem directory tree if missing."""
        for path in (
            self.resolved_incoming_dir(),
            self.resolved_processed_dir(),
            self.resolved_failed_dir(),
            self.resolved_verification_dir(),
            self.resolved_workspace_dir(),
            self.resolved_release_dir(),
            self.resolved_ingestion_archive_dir(),
            self.resolved_ingestion_status_path().parent,
            self.resolved_operator_auth_db_path().parent,
            self.resolved_operator_signing_key_path().parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.resolved_sqlite_path().parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton for the process."""
    return Settings()
