"""On-prem composition root — poll local incoming dir, run DocumentPipeline.

Usage (from laptop, targeting Mini PC Ollama)::

    set DOCUVISION_DATA_ROOT=C:\\data\\docuvision
    set DOCUVISION_OLLAMA_BASE_URL=http://192.168.1.50:11434
    python run_worker.py

Optional Postgres::

    set DOCUVISION_DATABASE_URL=postgresql://docuvision:docuvision@localhost:5432/docuvision
"""

from __future__ import annotations

import logging
import sys
import time

from docuvision.adapters.ollama_client import OllamaVisionClient
from docuvision.composition import build_backends
from docuvision.config.settings import Settings, get_settings
from docuvision.core.pipeline import DocumentPipeline
from docuvision.registry.schema_registry import SchemaRegistry
from docuvision.stages.gated_extraction import (
    DeferredGatedLayoutStage,
    GatedExtractionPipeline,
    GatedPipelineConfig,
    GatedVisionExtractionStage,
)
from docuvision.stages.layout import DoclingLayoutStage
from docuvision.stages.vision_extraction import VisionExtractionStage

logger = logging.getLogger("docuvision.worker")


def build_pipeline(
    settings: Settings,
    *,
    ollama: OllamaVisionClient | None = None,
) -> DocumentPipeline:
    """Wire concrete adapters into the Protocol-only orchestrator."""
    storage, repository = build_backends(settings)
    if settings.extraction_backend == "gated":
        logger.info("worker.extraction_backend=gated")
        layout = DeferredGatedLayoutStage()
        vision = GatedVisionExtractionStage(
            GatedExtractionPipeline(GatedPipelineConfig.from_settings(settings))
        )
    else:
        logger.info("worker.extraction_backend=legacy")
        layout = DoclingLayoutStage()
        vision_client = ollama or OllamaVisionClient(settings)
        registry = SchemaRegistry(settings.resolved_schemas_dir())
        vision = VisionExtractionStage(
            vision_client,
            registry=registry,
            use_llm_classification=settings.classification_use_llm,
        )

    return DocumentPipeline(
        storage=storage,
        repository=repository,
        layout=layout,
        vision=vision,
        max_retries=settings.MAX_RETRIES,
    )


def run_poll_loop(pipeline: DocumentPipeline, settings: Settings) -> None:
    interval = settings.watcher_poll_interval_seconds
    logger.info(
        "worker.poll_loop started interval_seconds=%s incoming=%s",
        interval,
        settings.resolved_incoming_dir(),
    )

    while True:
        try:
            result = pipeline.process_next()
            if result is None:
                time.sleep(interval)
                continue

            if result.success:
                logger.info(
                    "worker.job_ok job_id=%s file=%s status=%s",
                    result.job_id,
                    result.file_name,
                    result.status.value,
                )
            else:
                logger.error(
                    "worker.job_failed job_id=%s file=%s error=%s",
                    result.job_id,
                    result.file_name,
                    result.error_message,
                )
        except KeyboardInterrupt:
            logger.info("worker.shutdown requested")
            raise
        except Exception:
            logger.exception("worker.poll_error")
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    _ = argv  # reserved for future CLI flags
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = get_settings()
    logger.info("=== DocuVision on-prem worker ===")
    logger.info("data_root=%s", settings.data_root)
    logger.info("incoming=%s", settings.resolved_incoming_dir())
    logger.info("processed=%s", settings.resolved_processed_dir())
    logger.info("failed=%s", settings.resolved_failed_dir())
    logger.info("verification=%s", settings.resolved_verification_dir())
    logger.info("release=%s", settings.resolved_release_dir())
    if settings.database_url:
        logger.info("repository=postgres")
    else:
        logger.info("repository=sqlite path=%s", settings.resolved_sqlite_path())
    logger.info("ollama_base_url=%s", settings.ollama_base_url)
    logger.info("schemas_dir=%s", settings.resolved_schemas_dir())
    logger.info("classification_use_llm=%s", settings.classification_use_llm)
    logger.info("extraction_backend=%s", settings.extraction_backend)

    settings.ensure_directories()
    ollama: OllamaVisionClient | None = None
    if settings.extraction_backend == "legacy":
        ollama = OllamaVisionClient(settings)
        ollama.validate_access()

    pipeline = build_pipeline(settings, ollama=ollama)
    try:
        run_poll_loop(pipeline, settings)
    except KeyboardInterrupt:
        logger.info("worker stopped")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
