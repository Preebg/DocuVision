"""Isolated Docling layout extraction stage."""

from __future__ import annotations

import logging

from docling.document_converter import DocumentConverter

logger = logging.getLogger(__name__)


class DoclingLayoutStage:
    """``LayoutExtractor`` implementation using Docling ``DocumentConverter``."""

    def __init__(self, converter: DocumentConverter | None = None) -> None:
        self._converter = converter
        self._owned = converter is None

    def _get_converter(self) -> DocumentConverter:
        if self._converter is None:
            logger.info("layout.docling initializing DocumentConverter")
            self._converter = DocumentConverter()
        return self._converter

    def extract_layout(self, local_path: str) -> str:
        logger.info("layout.start path=%s", local_path)
        conversion_result = self._get_converter().convert(local_path)
        markdown = conversion_result.document.export_to_markdown()
        logger.info(
            "layout.complete characters=%s",
            len(markdown.strip()),
        )
        return markdown
