"""Isolated vision extraction stage — classify → schema-driven Ollama call."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from docuvision.adapters.ollama_client import ModelRecoverableError, OllamaVisionClient
from docuvision.extraction_schema import (
    is_empty_extraction,
    validate_extraction_payload,
)
from docuvision.registry.schema_registry import DocumentSchema, SchemaRegistry
from docuvision.stages.classification import ClassificationStage

logger = logging.getLogger(__name__)


class VisionExtractionStage:
    """``VisionLLMClient`` facade: classify, Mini PC inference, schema checks.

    The pipeline depends on this stage; the stage depends on
    ``OllamaVisionClient`` + ``SchemaRegistry`` for dynamic field contracts.
    """

    def __init__(
        self,
        client: OllamaVisionClient,
        *,
        registry: SchemaRegistry | None = None,
        classifier: ClassificationStage | None = None,
        use_llm_classification: bool = True,
    ) -> None:
        self._client = client
        self._registry = registry or SchemaRegistry()
        self._classifier = classifier or ClassificationStage(
            self._registry,
            client=client,
            use_llm_fallback=use_llm_classification,
        )

    @property
    def registry(self) -> SchemaRegistry:
        return self._registry

    def extract_fields(
        self,
        local_path: str,
        layout_markdown: str,
    ) -> dict[str, Any]:
        document_type = "PDF" if Path(local_path).suffix.lower() == ".pdf" else "image"
        classification = self._classifier.classify(layout_markdown)
        schema = classification.schema
        primary = self._client.primary_model
        fallback = self._client.fallback_model

        try:
            return self._extract_with_model(
                local_path,
                layout_markdown,
                primary,
                document_type,
                schema,
                classification.method,
            )
        except ModelRecoverableError as exc:
            logger.warning(
                "vision.fallback reason=%s primary=%s fallback=%s",
                exc,
                primary,
                fallback,
            )
            return self._extract_with_model(
                local_path,
                layout_markdown,
                fallback,
                document_type,
                schema,
                classification.method,
            )

    def _extract_with_model(
        self,
        local_path: str,
        layout_markdown: str,
        model: str,
        document_type: str,
        schema: DocumentSchema,
        classify_method: str,
    ) -> dict[str, Any]:
        logger.info(
            "vision.start type=%s model=%s class_id=%s classify=%s "
            "layout_chars=%s fields=%s",
            document_type,
            model,
            schema.class_id,
            classify_method,
            len(layout_markdown.strip()),
            ",".join(schema.field_names()),
        )
        try:
            parsed = self._client.extract_fields(
                local_path,
                layout_markdown,
                model=model,
                schema=schema,
                registry=self._registry,
            )
            image_size = self._client.last_processed_image_size
            image_width = image_size[0] if image_size else None
            image_height = image_size[1] if image_size else None
            # Drop zero-sized placeholders (raw bytes without PIL).
            if image_width == 0 or image_height == 0:
                image_width = None
                image_height = None
            normalized = self._registry.normalize_extraction(
                schema,
                parsed,
                image_width=image_width,
                image_height=image_height,
            )
            validated = validate_extraction_payload(normalized)
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

        logger.info(
            "vision.complete model=%s class_id=%s image_size=%sx%s",
            model,
            schema.class_id,
            image_width or "?",
            image_height or "?",
        )
        return validated
