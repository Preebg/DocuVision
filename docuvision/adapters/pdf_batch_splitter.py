"""PyMuPDF/zxing-cpp implementation of physical separator detection."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import fitz
import numpy as np

from docuvision.core.batch_splitting import (
    BarcodeStrategyConfig,
    BlankPageStrategyConfig,
    EncryptedPdfError,
    FixedIntervalStrategyConfig,
    MalformedPdfError,
    NoContentPagesError,
    SplitArtifact,
    SplitRange,
    StrategyConfig,
)

logger = logging.getLogger(__name__)


class PyMuPdfBatchSplitter:
    """Detect boundaries one raster page at a time and copy PDF page objects."""

    def split(
        self,
        file_path: str,
        strategy_config: StrategyConfig,
        output_dir: Path,
        *,
        name_prefix: str,
    ) -> tuple[list[SplitArtifact], list[int], int]:
        source_path = Path(file_path)
        try:
            source = fitz.open(source_path)
        except (fitz.FileDataError, RuntimeError, ValueError) as exc:
            raise MalformedPdfError(
                f"Unable to parse PDF '{source_path.name}': {exc}"
            ) from exc

        with source:
            if source.needs_pass:
                raise EncryptedPdfError(
                    f"PDF '{source_path.name}' is encrypted or password-protected."
                )
            if not source.is_pdf or source.page_count < 1:
                raise MalformedPdfError(
                    f"PDF '{source_path.name}' contains no readable pages."
                )

            if isinstance(strategy_config, FixedIntervalStrategyConfig):
                separators: list[int] = []
                ranges = [
                    SplitRange(
                        start_page=start,
                        end_page=min(
                            source.page_count,
                            start + strategy_config.pages_per_document - 1,
                        ),
                    )
                    for start in range(
                        1, source.page_count + 1, strategy_config.pages_per_document
                    )
                ]
            else:
                separators = self._detect_separators(source, strategy_config)
                ranges = self._ranges_without_separators(
                    source.page_count, separators
                )

            output_dir.mkdir(parents=True, exist_ok=True)
            if not separators and len(ranges) == 1:
                # Preserve the complete original byte stream when no slicing is needed.
                name = f"{name_prefix}_part_001.pdf"
                destination = output_dir / name
                shutil.copyfile(source_path, destination)
                return (
                    [
                        SplitArtifact(
                            file_name=name,
                            path=str(destination),
                            source_range=ranges[0],
                        )
                    ],
                    separators,
                    source.page_count,
                )

            metadata = source.metadata
            xml_metadata = source.get_xml_metadata()
            toc = source.get_toc()
            artifacts: list[SplitArtifact] = []
            width = max(3, len(str(len(ranges))))
            for index, page_range in enumerate(ranges, start=1):
                name = f"{name_prefix}_part_{index:0{width}d}.pdf"
                destination = output_dir / name
                child = fitz.open()
                try:
                    child.insert_pdf(
                        source,
                        from_page=page_range.start_page - 1,
                        to_page=page_range.end_page - 1,
                        links=True,
                        annots=True,
                    )
                    if metadata:
                        child.set_metadata(metadata)
                    if xml_metadata:
                        child.set_xml_metadata(xml_metadata)
                    child_toc = self._range_toc(toc, page_range)
                    if child_toc:
                        child.set_toc(child_toc)
                    child.save(destination, garbage=4, deflate=True)
                finally:
                    child.close()
                artifacts.append(
                    SplitArtifact(
                        file_name=name,
                        path=str(destination),
                        source_range=page_range,
                    )
                )
            return artifacts, separators, source.page_count

    def _detect_separators(
        self,
        source: fitz.Document,
        config: BarcodeStrategyConfig | BlankPageStrategyConfig,
    ) -> list[int]:
        separators: list[int] = []
        scale = config.dpi / 72.0
        matrix = fitz.Matrix(scale, scale)
        expected = (
            set(config.separator_values)
            if isinstance(config, BarcodeStrategyConfig)
            else None
        )

        for page_index in range(source.page_count):
            page = source.load_page(page_index)
            colorspace = (
                fitz.csRGB
                if isinstance(config, BarcodeStrategyConfig)
                else fitz.csGRAY
            )
            pixmap = page.get_pixmap(
                matrix=matrix, colorspace=colorspace, alpha=False
            )
            image: np.ndarray | None = None
            samples: np.ndarray | None = None
            try:
                samples = np.frombuffer(pixmap.samples_mv, dtype=np.uint8)
                if isinstance(config, BlankPageStrategyConfig):
                    density = float(
                        np.count_nonzero(samples < config.white_cutoff)
                    ) / float(samples.size)
                    matched = density < config.non_white_threshold
                else:
                    image = samples.reshape(
                        pixmap.height, pixmap.width, pixmap.n
                    )
                    matched = any(
                        value in expected for value in self._decode_barcodes(image)
                    )
            finally:
                if image is not None:
                    del image
                if samples is not None:
                    del samples
                pixmap = None
            if matched:
                separators.append(page_index + 1)
        return separators

    @staticmethod
    def _decode_barcodes(image: np.ndarray) -> list[str]:
        try:
            import zxingcpp
        except ImportError as exc:  # pragma: no cover - deployment guard
            raise RuntimeError(
                "Barcode splitting requires the 'zxing-cpp' package."
            ) from exc
        return [
            result.text
            for result in zxingcpp.read_barcodes(image)
            if getattr(result, "text", "")
        ]

    @staticmethod
    def _ranges_without_separators(
        page_count: int, separators: list[int]
    ) -> list[SplitRange]:
        if not separators:
            return [SplitRange(start_page=1, end_page=page_count)]
        ranges: list[SplitRange] = []
        cursor = 1
        for separator in sorted(set(separators)):
            if separator > cursor:
                ranges.append(
                    SplitRange(start_page=cursor, end_page=separator - 1)
                )
            else:
                logger.warning(
                    "batch_split.empty_range_discarded separator_page=%s",
                    separator,
                )
            cursor = separator + 1
        if cursor <= page_count:
            ranges.append(SplitRange(start_page=cursor, end_page=page_count))
        if not ranges:
            raise NoContentPagesError(
                "Every PDF page was detected as a separator; no child was created."
            )
        return ranges

    @staticmethod
    def _range_toc(toc: list[list], page_range: SplitRange) -> list[list]:
        selected: list[list] = []
        previous_level = 0
        base_level: int | None = None
        for item in toc:
            if len(item) < 3:
                continue
            level, title, page = int(item[0]), str(item[1]), int(item[2])
            if not (page_range.start_page <= page <= page_range.end_page):
                continue
            if base_level is None:
                base_level = level - 1
            normalized = max(1, level - base_level)
            normalized = min(normalized, previous_level + 1)
            selected.append(
                [normalized, title, page - page_range.start_page + 1]
            )
            previous_level = normalized
        return selected
