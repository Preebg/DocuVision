"""Batch PDF separator strategies and split orchestration."""

from __future__ import annotations

import logging
import tempfile
import traceback
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from docuvision.core.contracts import PipelineRepository, StorageBackend
from docuvision.core.models import QuarantineReason

logger = logging.getLogger(__name__)


class BarcodeStrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["barcode"] = "barcode"
    separator_values: list[str] = Field(
        default_factory=lambda: ["DOC-BREAK"], min_length=1, max_length=100
    )
    dpi: int = Field(default=200, ge=72, le=400)


class BlankPageStrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["blank_page"] = "blank_page"
    non_white_threshold: float = Field(default=0.01, gt=0, le=0.1)
    white_cutoff: int = Field(default=245, ge=1, le=255)
    dpi: int = Field(default=100, ge=36, le=300)


class FixedIntervalStrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["fixed_interval"] = "fixed_interval"
    pages_per_document: int = Field(ge=1, le=100_000)


StrategyConfig = Annotated[
    BarcodeStrategyConfig | BlankPageStrategyConfig | FixedIntervalStrategyConfig,
    Field(discriminator="strategy"),
]


class SplitRange(BaseModel):
    """Inclusive, one-based source PDF page range."""

    start_page: int = Field(ge=1)
    end_page: int = Field(ge=1)


class SplitArtifact(BaseModel):
    """One temporary child PDF produced by a splitter adapter."""

    file_name: str
    path: str
    source_range: SplitRange


class BatchSplitPart(BaseModel):
    document_id: str
    file_name: str
    part_index: int
    part_count: int
    start_page: int
    end_page: int
    sequence_no: int


class BatchSplitResult(BaseModel):
    batch_id: str
    parent_document_id: str
    strategy: str
    source_page_count: int
    separator_pages: list[int] = Field(default_factory=list)
    parts: list[BatchSplitPart] = Field(default_factory=list)
    idempotent_replay: bool = False


class PdfSplitError(Exception):
    """Base class for a split request that cannot be completed."""


class EncryptedPdfError(PdfSplitError):
    """The source PDF requires a password."""


class MalformedPdfError(PdfSplitError):
    """The source payload is not a readable PDF."""


class NoContentPagesError(PdfSplitError):
    """Every source page was a separator, so no child can be created."""


class PdfBatchSplitter(Protocol):
    def split(
        self,
        file_path: str,
        strategy_config: StrategyConfig,
        output_dir: Path,
        *,
        name_prefix: str,
    ) -> tuple[list[SplitArtifact], list[int], int]:
        """Write child PDFs and return artifacts, separators, and page count."""
        ...


class BatchSplitEngine:
    """Coordinates trusted PDF splitting, storage, persistence, and quarantine."""

    def __init__(
        self,
        *,
        repository: PipelineRepository,
        storage: StorageBackend,
        splitter: PdfBatchSplitter,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._splitter = splitter

    def process_batch_splits(
        self,
        batch_id: str,
        file_path: str,
        strategy_config: StrategyConfig,
        *,
        source_document_id: str | None = None,
    ) -> BatchSplitResult:
        batch = self._repository.get_batch(batch_id)
        if batch is None:
            raise KeyError(f"Batch not found: {batch_id}")

        if source_document_id:
            explicit_source = self._repository.get_job(source_document_id)
            if explicit_source is None:
                raise KeyError(f"Document not found: {source_document_id}")
            prior = self._repository.get_batch_split_children(
                batch_id, source_document_id
            )
            if prior:
                return self._build_result(
                    batch_id,
                    source_document_id,
                    strategy_config.strategy,
                    prior,
                    source_page_count=max(item[4] for item in prior),
                    idempotent_replay=True,
                )

        source = self._resolve_source(batch_id, file_path, source_document_id)
        prior = self._repository.get_batch_split_children(batch_id, source.id)
        if prior:
            return self._build_result(
                batch_id,
                source.id,
                strategy_config.strategy,
                prior,
                source_page_count=max(item[4] for item in prior),
                idempotent_replay=True,
            )

        if Path(file_path).suffix.lower() != ".pdf":
            raise ValueError("Batch splitting only supports PDF source documents.")

        stored_names: list[str] = []
        committed = False
        try:
            with tempfile.TemporaryDirectory(prefix="docuvision-split-") as temp_dir:
                artifacts, separators, page_count = self._splitter.split(
                    file_path,
                    strategy_config,
                    Path(temp_dir),
                    name_prefix=f"{Path(source.file_name).stem}__{source.id[:8]}",
                )
                streams = []
                try:
                    streams = [open(item.path, "rb") for item in artifacts]
                    stored_names = self._storage.write_incoming_batch(
                        [
                            (artifact.file_name, stream)
                            for artifact, stream in zip(artifacts, streams, strict=True)
                        ]
                    )
                finally:
                    for stream in streams:
                        stream.close()

                children = self._repository.commit_batch_split(
                    batch_id,
                    source.id,
                    [
                        (
                            stored_name,
                            artifact.source_range.start_page,
                            artifact.source_range.end_page,
                        )
                        for stored_name, artifact in zip(
                            stored_names, artifacts, strict=True
                        )
                    ],
                    strategy=strategy_config.strategy,
                )
                committed = True
                committed = self._repository.get_batch_split_children(
                    batch_id, source.id
                )
                if len(committed) != len(children):
                    raise RuntimeError("Split lineage did not match committed children.")
                result = self._build_result(
                    batch_id,
                    source.id,
                    strategy_config.strategy,
                    committed,
                    source_page_count=page_count,
                )
                result.separator_pages = separators
                try:
                    self._storage.mark_processed(source.file_name, file_path)
                except Exception:
                    logger.exception(
                        "batch_split.source_archive_incomplete document_id=%s",
                        source.id,
                    )
                return result
        except (EncryptedPdfError, MalformedPdfError) as exc:
            self._quarantine_source(source.id, source.file_name, file_path, exc)
            raise
        except Exception:
            if stored_names and not committed:
                self._storage.remove_incoming(stored_names)
            raise

    def _resolve_source(
        self, batch_id: str, file_path: str, source_document_id: str | None
    ):
        batch = self._repository.get_batch(batch_id)
        assert batch is not None
        if source_document_id:
            matching = [
                item for item in batch.documents if item.document_id == source_document_id
            ]
        else:
            matching = [
                item
                for item in batch.documents
                if Path(item.file_name or "").name == Path(file_path).name
            ]
        if len(matching) != 1:
            raise ValueError("Source document is not uniquely linked to this batch.")
        source = self._repository.get_job(matching[0].document_id)
        if source is None:
            raise KeyError(f"Document not found: {matching[0].document_id}")
        memberships = self._repository.list_batches_for_document(source.id)
        if len(memberships) != 1 or memberships[0].id != batch_id:
            raise ValueError(
                "Source document must belong exclusively to the requested batch."
            )
        return source

    def _quarantine_source(
        self, document_id: str, file_name: str, file_path: str, exc: Exception
    ) -> None:
        stack = "".join(traceback.format_exception(exc))[-16_000:]
        item = self._repository.begin_quarantine(
            document_id,
            reason=QuarantineReason.PARSING_ERROR,
            error_stack=stack,
            failed_stage="BATCH_SPLIT",
        )
        try:
            locator = self._storage.quarantine_payload(
                document_id, file_name, file_path
            )
            self._repository.set_quarantine_locator(item.id, locator)
        except Exception:
            logger.exception(
                "batch_split.quarantine_storage_incomplete document_id=%s",
                document_id,
            )

    @staticmethod
    def _build_result(
        batch_id: str,
        parent_document_id: str,
        strategy: str,
        rows: list[tuple[str, str, int, int, int, int]],
        *,
        source_page_count: int,
        idempotent_replay: bool = False,
    ) -> BatchSplitResult:
        return BatchSplitResult(
            batch_id=batch_id,
            parent_document_id=parent_document_id,
            strategy=strategy,
            source_page_count=source_page_count,
            idempotent_replay=idempotent_replay,
            parts=[
                BatchSplitPart(
                    document_id=document_id,
                    file_name=file_name,
                    part_index=index,
                    part_count=len(rows),
                    start_page=start,
                    end_page=end,
                    sequence_no=sequence,
                )
                for document_id, file_name, index, start, end, sequence in rows
            ],
        )
