"""Core domain: models, contracts, errors, observability, and orchestration."""

from docuvision.core.contracts import (
    LayoutExtractor,
    PipelineRepository,
    StorageBackend,
    VisionLLMClient,
)
from docuvision.core.errors import (
    FatalProcessingError,
    InfrastructureError,
    PipelineError,
    ReleaseExportError,
    TransientError,
    ValidationError,
)
from docuvision.core.models import (
    PIPELINE_HAPPY_PATH,
    Batch,
    BatchPriority,
    BatchStatus,
    BatchSummary,
    DocumentJob,
    DocumentPage,
    JobStatus,
    PipelineResult,
    PipelineStatus,
)
from docuvision.core.observability import (
    JsonFormatter,
    bind_correlation,
    configure_json_logger,
)
from docuvision.core.pipeline import DocumentPipeline
from docuvision.core.release import ReleaseEngine

# Deprecated aliases — prefer the taxonomy in errors.py.
LayoutStageError = ValidationError
RepositoryError = InfrastructureError
StagingError = InfrastructureError
StorageError = TransientError
VisionStageError = FatalProcessingError

__all__ = [
    "Batch",
    "BatchPriority",
    "BatchStatus",
    "BatchSummary",
    "DocumentJob",
    "DocumentPage",
    "DocumentPipeline",
    "FatalProcessingError",
    "InfrastructureError",
    "JobStatus",
    "JsonFormatter",
    "LayoutExtractor",
    "LayoutStageError",
    "PIPELINE_HAPPY_PATH",
    "PipelineError",
    "PipelineRepository",
    "PipelineResult",
    "PipelineStatus",
    "ReleaseEngine",
    "ReleaseExportError",
    "RepositoryError",
    "StagingError",
    "StorageBackend",
    "StorageError",
    "TransientError",
    "ValidationError",
    "VisionLLMClient",
    "VisionStageError",
    "bind_correlation",
    "configure_json_logger",
]
