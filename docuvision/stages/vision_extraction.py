"""Isolated vision extraction stage — Ollama client + schema validation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from docuvision.adapters.ollama_client import ModelRecoverableError, OllamaVisionClient
from docuvision.extraction_schema import (
    is_empty_extraction,
    validate_extraction_payload,
)

logger = logging.getLogger(__name__)


class VisionExtractionStage:
    """``VisionLLMClient`` facade: Mini PC inference, fallback, schema checks.

    The pipeline depends on this stage; the stage depends on ``OllamaVisionClient``.
    """

    def __init__(self, client: OllamaVisionClient) -> None:
        self._client = client

    def extract_fields(
        self,
        local_path: str,
        layout_markdown: str,
    ) -> dict[str, Any]:
        document_type = "PDF" if Path(local_path).suffix.lower() == ".pdf" else "image"
        primary = self._client.primary_model
        fallback = self._client.fallback_model

        try:
            return self._extract_with_model(
                local_path, layout_markdown, primary, document_type
            )
        except ModelRecoverableError as exc:
            logger.warning(
                "vision.fallback reason=%s primary=%s fallback=%s",
                exc,
                primary,
                fallback,
            )
            return self._extract_with_model(
                local_path, layout_markdown, fallback, document_type
            )

    def _extract_with_model(
        self,
        local_path: str,
        layout_markdown: str,
        model: str,
        document_type: str,
    ) -> dict[str, Any]:
        logger.info(
            "vision.start type=%s model=%s layout_chars=%s",
            document_type,
            model,
            len(layout_markdown.strip()),
        )
        try:
            parsed = self._client.extract_fields(
                local_path, layout_markdown, model=model
            )
            validated = validate_extraction_payload(parsed)
        except ModelRecoverableError:
            raise
        except Exception as exc:
            if OllamaVisionClient.is_recoverable_failure(exc):
                raise ModelRecoverableError(
                    f"Model '{model}' extraction failed: {exc}"
                ) from exc
            raise

        if is_empty_extraction(validated):
            raise RuntimeError(
                f"Model '{model}' returned empty extracted text for {local_path}"
            )

        logger.info("vision.complete model=%s", model)
        return validated
