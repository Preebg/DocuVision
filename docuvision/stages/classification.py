"""Classification pre-pass — keyword router with optional lightweight LLM."""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

from docuvision.registry.schema_registry import (
    ClassificationResult,
    DocumentSchema,
    SchemaRegistry,
)

logger = logging.getLogger(__name__)


class _TextChatClient(Protocol):
    """Minimal surface used for lightweight classification (no images)."""

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
    ) -> str: ...

    @property
    def fallback_model(self) -> str: ...

    @property
    def primary_model(self) -> str: ...


class ClassificationStage:
    """Resolve ``class_id`` before schema-driven vision extraction.

    Prefer the offline keyword pre-pass. Only when inconclusive (and an Ollama
    client is wired) does this stage spend a small text-only chat call using
    ``config/prompts/classify_router.md``.
    """

    def __init__(
        self,
        registry: SchemaRegistry,
        *,
        client: _TextChatClient | None = None,
        use_llm_fallback: bool = True,
        classify_model: str | None = None,
    ) -> None:
        self._registry = registry
        self._client = client
        self._use_llm_fallback = use_llm_fallback
        self._classify_model = classify_model

    @property
    def registry(self) -> SchemaRegistry:
        return self._registry

    def classify(self, layout_markdown: str) -> ClassificationResult:
        llm_fn: Callable[[str], str] | None = None
        if self._use_llm_fallback and self._client is not None:
            llm_fn = self._llm_classify

        result = self._registry.classify(
            layout_markdown,
            llm_classify=llm_fn,
            use_llm_fallback=self._use_llm_fallback and llm_fn is not None,
        )
        logger.info(
            "classification.result class_id=%s method=%s confidence=%.2f "
            "keywords=%s",
            result.class_id,
            result.method,
            result.confidence,
            ",".join(result.matched_keywords) or "-",
        )
        return result

    def resolve_schema(self, layout_markdown: str) -> DocumentSchema:
        return self.classify(layout_markdown).schema

    def _llm_classify(self, prompt: str) -> str:
        assert self._client is not None
        # Prefer the lighter fallback model for the router — vision VRAM stays free.
        model = self._classify_model or self._client.fallback_model
        messages = [
            {
                "role": "system",
                "content": (
                    "You route documents to a semantic class_id. "
                    "Reply with JSON only."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        response_format = {
            "type": "object",
            "properties": {
                "class_id": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["class_id", "confidence"],
        }
        logger.info(
            "classification.llm_router model=%s prompt_chars=%s",
            model,
            len(prompt),
        )
        return self._client.chat(model, messages, response_format=response_format)


__all__ = ["ClassificationStage"]
