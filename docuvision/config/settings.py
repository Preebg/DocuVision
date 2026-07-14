"""Pydantic-settings for air-gapped local paths and Mini PC Ollama endpoint."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """On-prem configuration — no cloud credentials.

    Environment variables use the ``DOCUVISION_`` prefix, e.g.:

        DOCUVISION_DATA_ROOT=/data
        DOCUVISION_OLLAMA_BASE_URL=http://192.168.87.219:11434
    """

    model_config = SettingsConfigDict(
        env_prefix="DOCUVISION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- local directory layout -------------------------------------------
    data_root: Path = Field(
        default=Path("/data"),
        description="Root for on-prem document directories.",
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

    # -- Mini PC Ollama (network-pluggable) -------------------------------
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description=(
            "Ollama HTTP base URL. Point at the Mini PC on the LAN, e.g. "
            "http://192.168.1.50:11434."
        ),
    )
    ollama_timeout_seconds: int = Field(default=300, ge=1)
    ollama_primary_model: str = Field(default="qwen3-vl:8b-thinking")
    ollama_fallback_model: str = Field(default="qwen2.5vl:3b")
    ollama_num_ctx: int = Field(default=8192, ge=512)
    ollama_image_max_dimension: int = Field(default=1280, ge=256)
    ollama_json_mode: bool = Field(default=True)
    ollama_warmup: bool = Field(default=True)
    ollama_pdf_max_pages: int = Field(default=5, ge=1)

    # -- layout / watcher / fault protection -----------------------------
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

    @field_validator("ollama_base_url")
    @classmethod
    def _normalize_ollama_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("database_url", mode="before")
    @classmethod
    def _empty_database_url_to_none(cls, value: object) -> object:
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

    def resolved_schemas_dir(self) -> Path:
        if self.schemas_dir is not None:
            return self.schemas_dir
        # docuvision/config/settings.py → repo root / config / schemas
        return Path(__file__).resolve().parents[2] / "config" / "schemas"

    def ensure_directories(self) -> None:
        """Create the on-prem directory tree if missing."""
        for path in (
            self.resolved_incoming_dir(),
            self.resolved_processed_dir(),
            self.resolved_failed_dir(),
            self.resolved_verification_dir(),
            self.resolved_workspace_dir(),
            self.resolved_release_dir(),
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.resolved_sqlite_path().parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton for the process."""
    return Settings()
