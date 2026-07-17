"""Resource-gated batch extraction with hard process memory boundaries.

The layout, OCR, and VLM stages run in sequential waves.  Heavy stages use
fresh spawned processes by default, so a worker exit releases Python and
native allocator state even when a library-level cache cannot be cleared.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import multiprocessing
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from docuvision.adapters.local_model_process import transformer_crop_worker
from docuvision.core.observability import configure_json_logger

logger = configure_json_logger("docuvision.gated_extraction")

_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_SERIAL_PATTERN = re.compile(r"(?=.{4,48}$)(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9._:/-]+")
_GPU_THREAD_LOCK = threading.Lock()


class GatedExtractionError(RuntimeError):
    """A stage failed without leaking its session workspace."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage


@dataclass(frozen=True, slots=True)
class RawDocument:
    """One source document supplied as a path or an in-memory byte payload."""

    name: str
    path: Path | str | None = None
    content: bytes | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.content is None):
            raise ValueError("RawDocument requires exactly one of path or content")


@dataclass(frozen=True, slots=True)
class LocalModelConfig:
    model_id: str
    prompt: str
    quantization: str = "none"
    device_map: str = "auto"
    torch_dtype: str = "auto"
    max_new_tokens: int = 256
    trust_remote_code: bool = True
    fail_fast: bool = False

    def worker_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GatedPipelineConfig:
    """Bounded-resource execution policy."""

    temp_root: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    layout_timeout_seconds: float = 900.0
    model_timeout_seconds: float = 900.0
    layout_worker_rss_limit_mb: int = 4096
    glm_worker_rss_limit_mb: int = 3072
    qwen_worker_rss_limit_mb: int = 8192
    pdf_dpi: int = 150
    crop_padding_pixels: int = 3
    checkbox_density_threshold: float = 0.12
    keep_failed_sessions: bool = False
    glm_ocr: LocalModelConfig = field(
        default_factory=lambda: LocalModelConfig(
            model_id="zai-org/GLM-OCR",
            prompt=(
                "Transcribe this handwriting or body-text crop exactly. "
                "Return plain text only."
            ),
        )
    )
    qwen_serial: LocalModelConfig = field(
        default_factory=lambda: LocalModelConfig(
            model_id="Qwen/Qwen3-VL-8B-Instruct",
            prompt=(
                "Read the alphanumeric identifier, serial, key, or barcode "
                "value exactly. Preserve case and punctuation. Return only the value."
            ),
            quantization="4bit",
            max_new_tokens=96,
        )
    )

    def __post_init__(self) -> None:
        if self.pdf_dpi < 72:
            raise ValueError("pdf_dpi must be at least 72")
        if self.crop_padding_pixels < 0:
            raise ValueError("crop_padding_pixels cannot be negative")
        if not 0.0 <= self.checkbox_density_threshold <= 1.0:
            raise ValueError("checkbox_density_threshold must be in [0, 1]")
        if min(
            self.layout_worker_rss_limit_mb,
            self.glm_worker_rss_limit_mb,
            self.qwen_worker_rss_limit_mb,
        ) < 256:
            raise ValueError("worker RSS limits must be at least 256 MB")

    @classmethod
    def from_settings(cls, settings: Any) -> "GatedPipelineConfig":
        """Build from :class:`docuvision.config.settings.Settings`."""
        defaults = cls()
        return cls(
            temp_root=Path(settings.gated_temp_root),
            layout_timeout_seconds=float(settings.gated_layout_timeout_seconds),
            model_timeout_seconds=float(settings.gated_model_timeout_seconds),
            layout_worker_rss_limit_mb=int(settings.gated_layout_rss_limit_mb),
            glm_worker_rss_limit_mb=int(settings.gated_glm_rss_limit_mb),
            qwen_worker_rss_limit_mb=int(settings.gated_qwen_rss_limit_mb),
            pdf_dpi=int(settings.gated_pdf_dpi),
            crop_padding_pixels=defaults.crop_padding_pixels,
            checkbox_density_threshold=float(
                settings.gated_checkbox_density_threshold
            ),
            keep_failed_sessions=defaults.keep_failed_sessions,
            glm_ocr=LocalModelConfig(
                model_id=str(settings.gated_glm_model),
                prompt=defaults.glm_ocr.prompt,
                quantization=defaults.glm_ocr.quantization,
                max_new_tokens=defaults.glm_ocr.max_new_tokens,
            ),
            qwen_serial=LocalModelConfig(
                model_id=str(settings.gated_qwen_model),
                prompt=defaults.qwen_serial.prompt,
                quantization=str(settings.gated_qwen_quantization),
                max_new_tokens=defaults.qwen_serial.max_new_tokens,
            ),
        )


@dataclass(frozen=True, slots=True)
class SessionPaths:
    session_id: str
    root: Path
    source: Path
    box: Path
    txt: Path
    sn: Path
    results: Path
    coordinates: Path

    @classmethod
    def create(cls, temp_root: Path) -> "SessionPaths":
        session_id = str(uuid4())
        root = temp_root / session_id
        instance = cls(
            session_id=session_id,
            root=root,
            source=root / "source",
            box=root / "box",
            txt=root / "txt",
            sn=root / "sn",
            results=root / "results",
            coordinates=root / "master_coordinates.json",
        )
        try:
            for path in (
                instance.source,
                instance.box,
                instance.txt,
                instance.sn,
                instance.results,
            ):
                path.mkdir(parents=True, exist_ok=False)
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return instance


LayoutRunner = Callable[[Sequence[dict[str, str]], SessionPaths], dict[str, Any]]
ModelRunner = Callable[
    [str, Path, Path, LocalModelConfig, float], dict[str, Any]
]


class GatedExtractionPipeline:
    """Run a document batch through mutually exclusive, memory-bounded waves."""

    def __init__(
        self,
        config: GatedPipelineConfig | None = None,
        *,
        layout_runner: LayoutRunner | None = None,
        model_runner: ModelRunner | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.config = config or GatedPipelineConfig()
        self._layout_runner = layout_runner or self._run_layout_subprocess
        self._model_runner = model_runner or self._run_model_subprocess
        self._log = logger_ or logger

    def process_batch(
        self,
        documents: Sequence[RawDocument | str | os.PathLike[str]],
    ) -> dict[str, Any]:
        """Synchronously process a non-empty document batch."""
        if not documents:
            raise ValueError("documents cannot be empty")

        session = SessionPaths.create(Path(self.config.temp_root))
        started = time.monotonic()
        succeeded = False
        self._log_memory("session.created", session.session_id)

        try:
            ingested = self._ingest(documents, session)
            self._log_memory("ingestion.complete", session.session_id)

            coordinates = self._run_stage(
                "layout",
                lambda: self._layout_runner(ingested, session),
            )
            _write_json_atomic(session.coordinates, coordinates)
            self._log_memory("layout.released", session.session_id)

            regions = coordinates.get("regions")
            if not isinstance(regions, list):
                raise GatedExtractionError("layout", "missing regions list")

            checkbox_results = self._run_stage(
                "checkbox",
                lambda: self._evaluate_checkboxes(session.box),
            )
            self._log_memory("wave_a.complete", session.session_id)

            text_results = self._run_model_wave(
                "glm_ocr",
                session.txt,
                session.results / "glm_ocr.json",
                self.config.glm_ocr,
                session,
            )
            self._log_memory("wave_b.released", session.session_id)

            if _contains_images(session.sn):
                serial_results = self._run_model_wave(
                    "qwen_serial",
                    session.sn,
                    session.results / "qwen_serial.json",
                    self.config.qwen_serial,
                    session,
                )
                self._log_memory("wave_c.released", session.session_id)
            else:
                serial_results = {
                    "ok": True,
                    "results": {},
                    "failures": {},
                    "skipped": True,
                    "reason": "no serial/alphanumeric crops",
                }
                self._log.info(
                    "gated.wave_skipped session_id=%s wave=qwen_serial reason=no_crops",
                    session.session_id,
                )

            output = self._aggregate(
                session=session,
                coordinates=coordinates,
                checkbox_results=checkbox_results,
                text_results=text_results,
                serial_results=serial_results,
                elapsed_seconds=time.monotonic() - started,
            )
            succeeded = True
            self._log_memory("aggregation.complete", session.session_id)
            return output
        finally:
            gc.collect()
            if succeeded or not self.config.keep_failed_sessions:
                shutil.rmtree(session.root, ignore_errors=True)
                self._log.info(
                    "gated.session_cleaned session_id=%s path=%s",
                    session.session_id,
                    session.root,
                )
            else:
                self._log.warning(
                    "gated.session_retained session_id=%s path=%s",
                    session.session_id,
                    session.root,
                )

    async def aprocess_batch(
        self,
        documents: Sequence[RawDocument | str | os.PathLike[str]],
    ) -> dict[str, Any]:
        """Non-blocking API entry; resource-heavy waves remain sequential."""
        return await asyncio.to_thread(self.process_batch, documents)

    def _ingest(
        self,
        documents: Sequence[RawDocument | str | os.PathLike[str]],
        session: SessionPaths,
    ) -> list[dict[str, str]]:
        ingested: list[dict[str, str]] = []
        used_names: set[str] = set()
        for index, item in enumerate(documents):
            raw = (
                item
                if isinstance(item, RawDocument)
                else RawDocument(name=Path(item).name, path=Path(item))
            )
            safe_name = _safe_unique_name(raw.name, index, used_names)
            destination = session.source / safe_name
            if raw.path is not None:
                source = Path(raw.path).expanduser().resolve()
                if not source.is_file():
                    raise GatedExtractionError(
                        "ingestion", f"source is not a file: {source}"
                    )
                shutil.copyfile(source, destination)
            else:
                assert raw.content is not None
                destination.write_bytes(raw.content)
            if destination.stat().st_size == 0:
                raise GatedExtractionError(
                    "ingestion", f"empty document: {raw.name}"
                )
            ingested.append(
                {
                    "document_id": f"doc-{index + 1:04d}",
                    "name": raw.name,
                    "path": str(destination),
                }
            )
        return ingested

    def _run_layout_subprocess(
        self,
        documents: Sequence[dict[str, str]],
        session: SessionPaths,
    ) -> dict[str, Any]:
        result_path = session.results / "layout.json"
        _spawn_and_read(
            target=_docling_layout_worker,
            args=(
                list(documents),
                str(session.root),
                str(result_path),
                self.config.pdf_dpi,
                self.config.crop_padding_pixels,
            ),
            output_path=result_path,
            timeout_seconds=self.config.layout_timeout_seconds,
            stage="layout",
            memory_limit_mb=self.config.layout_worker_rss_limit_mb,
        )
        payload = _read_json(result_path)
        if not payload.get("ok"):
            raise GatedExtractionError(
                "layout", str(payload.get("error") or "Docling worker failed")
            )
        master = payload.get("master")
        if not isinstance(master, dict):
            raise GatedExtractionError("layout", "worker returned no coordinate data")
        return master

    def _run_model_subprocess(
        self,
        wave: str,
        input_dir: Path,
        output_path: Path,
        model_config: LocalModelConfig,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        _spawn_and_read(
            target=transformer_crop_worker,
            args=(
                str(input_dir),
                str(output_path),
                model_config.worker_payload(),
            ),
            output_path=output_path,
            timeout_seconds=timeout_seconds,
            stage=wave,
            memory_limit_mb=(
                self.config.glm_worker_rss_limit_mb
                if wave == "glm_ocr"
                else self.config.qwen_worker_rss_limit_mb
            ),
        )
        return _read_json(output_path)

    def _run_model_wave(
        self,
        wave: str,
        input_dir: Path,
        output_path: Path,
        model_config: LocalModelConfig,
        session: SessionPaths,
    ) -> dict[str, Any]:
        if not _contains_images(input_dir):
            self._log.info(
                "gated.wave_skipped session_id=%s wave=%s reason=no_crops",
                session.session_id,
                wave,
            )
            return {
                "ok": True,
                "results": {},
                "failures": {},
                "skipped": True,
                "reason": "no crops",
            }

        lock_path = Path(self.config.temp_root) / "docuvision-local-gpu.lock"
        with _exclusive_gpu_lock(
            lock_path,
            timeout_seconds=self.config.model_timeout_seconds,
        ):
            payload = self._run_stage(
                wave,
                lambda: self._model_runner(
                    wave,
                    input_dir,
                    output_path,
                    model_config,
                    self.config.model_timeout_seconds,
                ),
            )
        if not payload.get("ok"):
            raise GatedExtractionError(
                wave, str(payload.get("error") or "model worker failed")
            )
        return payload

    def _evaluate_checkboxes(self, directory: Path) -> dict[str, Any]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise GatedExtractionError(
                "checkbox",
                "opencv-python-headless and numpy are required",
            ) from exc

        results: dict[str, dict[str, Any]] = {}
        failures: dict[str, str] = {}
        for image_path in _iter_images(directory):
            try:
                image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
                if image is None or image.size == 0:
                    raise ValueError("OpenCV could not decode crop")
                image = cv2.GaussianBlur(image, (3, 3), 0)
                binary = cv2.threshold(
                    image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
                )[1]

                height, width = binary.shape[:2]
                inset_y = max(1, int(height * 0.2))
                inset_x = max(1, int(width * 0.2))
                inner = binary[
                    inset_y : max(inset_y + 1, height - inset_y),
                    inset_x : max(inset_x + 1, width - inset_x),
                ]
                density = float(np.count_nonzero(inner)) / float(inner.size)
                contours, _ = cv2.findContours(
                    inner, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
                )
                stroke_area = sum(
                    cv2.contourArea(contour)
                    for contour in contours
                    if cv2.contourArea(contour) >= 2.0
                )
                contour_density = stroke_area / float(inner.size)
                checked = max(density, contour_density) >= (
                    self.config.checkbox_density_threshold
                )
                results[image_path.stem] = {
                    "value": "[X]" if checked else "[ ]",
                    "checked": checked,
                    "pixel_density": round(density, 6),
                    "contour_density": round(contour_density, 6),
                }
            except Exception as exc:
                failures[image_path.stem] = f"{type(exc).__name__}: {exc}"
        return {"ok": not failures, "results": results, "failures": failures}

    def _aggregate(
        self,
        *,
        session: SessionPaths,
        coordinates: Mapping[str, Any],
        checkbox_results: Mapping[str, Any],
        text_results: Mapping[str, Any],
        serial_results: Mapping[str, Any],
        elapsed_seconds: float,
    ) -> dict[str, Any]:
        wave_results = {
            "box": checkbox_results.get("results", {}),
            "txt": text_results.get("results", {}),
            "sn": serial_results.get("results", {}),
        }
        failures = {
            "box": checkbox_results.get("failures", {}),
            "txt": text_results.get("failures", {}),
            "sn": serial_results.get("failures", {}),
        }
        fields: dict[str, dict[str, Any]] = {}
        full_text: list[str] = []
        documents_by_id: dict[str, dict[str, Any]] = {}

        for document in coordinates.get("documents", []):
            if isinstance(document, dict) and document.get("document_id"):
                documents_by_id[str(document["document_id"])] = {
                    **document,
                    "pages": {},
                }

        for region in coordinates.get("regions", []):
            if not isinstance(region, dict):
                continue
            region_id = str(region.get("region_id") or "")
            category = str(region.get("category") or "")
            raw_result = wave_results.get(category, {}).get(region_id, {})
            failure = wave_results.get(category)
            value = ""
            metadata: dict[str, Any] = {}
            if isinstance(raw_result, dict):
                value = str(raw_result.get("value", raw_result.get("text", "")))
                metadata = {
                    key: val
                    for key, val in raw_result.items()
                    if key not in {"value", "text"}
                }
            error = failures.get(category, {}).get(region_id)
            entry = {
                "value": value,
                "bbox_2d": region.get("bbox_2d"),
                "normalized_bbox": region.get("normalized_bbox"),
                "confidence": None,
                "page": region.get("page"),
                "document_id": region.get("document_id"),
                "category": category,
                "label": region.get("label"),
                "source_text": region.get("source_text", ""),
                "status": "error" if error else "ok",
            }
            entry.update(metadata)
            if error:
                entry["error"] = error
            fields[region_id] = entry
            if category in {"txt", "sn"} and value.strip():
                full_text.append(value.strip())

            document = documents_by_id.get(str(region.get("document_id")))
            if document is not None:
                page_key = str(region.get("page") or 1)
                pages = document["pages"]
                page = pages.setdefault(page_key, {"page": int(page_key), "regions": []})
                page["regions"].append({"region_id": region_id, **entry})

        documents_output = []
        for document in documents_by_id.values():
            document["pages"] = [
                document["pages"][key]
                for key in sorted(document["pages"], key=lambda item: int(item))
            ]
            documents_output.append(document)

        all_failures = {
            wave: values for wave, values in failures.items() if values
        }
        return {
            "class_id": "gated_extraction",
            "form_type": "gated_extraction",
            "display_name": "Resource-gated extraction",
            "session_id": session.session_id,
            "extracted_payload": {
                "full_text": "\n".join(full_text),
                "fields": fields,
                "summary": (
                    f"Extracted {len(fields)} regions from "
                    f"{len(documents_output)} document(s)."
                ),
                "documents": documents_output,
            },
            "diagnostics": {
                "elapsed_seconds": round(elapsed_seconds, 3),
                "region_count": len(fields),
                "wave_failures": all_failures,
                "serial_wave_skipped": bool(serial_results.get("skipped")),
            },
        }

    def _run_stage(self, stage: str, operation: Callable[[], Any]) -> Any:
        started = time.monotonic()
        self._log.info("gated.stage_start stage=%s", stage)
        try:
            result = operation()
        except GatedExtractionError:
            raise
        except BaseException as exc:
            self._log.exception("gated.stage_failed stage=%s", stage)
            raise GatedExtractionError(
                stage, f"{type(exc).__name__}: {exc}"
            ) from exc
        self._log.info(
            "gated.stage_complete stage=%s elapsed_seconds=%.3f",
            stage,
            time.monotonic() - started,
        )
        return result

    def _log_memory(self, event: str, session_id: str) -> None:
        try:
            import psutil

            process = psutil.Process()
            rss_mb = process.memory_info().rss / (1024 * 1024)
            available_mb = psutil.virtual_memory().available / (1024 * 1024)
            self._log.info(
                "gated.memory event=%s session_id=%s rss_mb=%.1f available_mb=%.1f",
                event,
                session_id,
                rss_mb,
                available_mb,
            )
        except Exception:
            self._log.info(
                "gated.memory event=%s session_id=%s metrics=unavailable",
                event,
                session_id,
            )


class DeferredGatedLayoutStage:
    """Pipeline-compatible marker; gated vision owns the sole Docling pass."""

    def extract_layout(self, local_path: str) -> str:
        return f"gated-layout-deferred:{Path(local_path).name}"


class GatedVisionExtractionStage:
    """Adapt the gated engine to the existing single-document vision contract."""

    def __init__(self, pipeline: GatedExtractionPipeline) -> None:
        self._pipeline = pipeline

    def extract_fields(
        self,
        local_path: str,
        layout_markdown: str,
    ) -> dict[str, Any]:
        del layout_markdown
        return self._pipeline.process_batch([local_path])


def _docling_layout_worker(
    documents: list[dict[str, str]],
    session_root: str,
    output_path: str,
    pdf_dpi: int,
    crop_padding_pixels: int,
) -> None:
    """Docling layout + page rasterization + coordinate crop worker."""
    converter = None
    conversion_result = None
    try:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        root = Path(session_root)
        regions: list[dict[str, Any]] = []
        document_records: list[dict[str, Any]] = []

        for document in documents:
            source_path = Path(document["path"])
            conversion_result = converter.convert(str(source_path))
            docling_document = conversion_result.document
            markdown = docling_document.export_to_markdown()
            pages = _render_source_pages(source_path, pdf_dpi)
            page_sizes = _docling_page_sizes(docling_document)
            document_records.append(
                {
                    "document_id": document["document_id"],
                    "name": document["name"],
                    "page_count": len(pages),
                    "layout_markdown": markdown,
                }
            )

            region_index = 0
            for item, _level in docling_document.iterate_items():
                provenance = getattr(item, "prov", None)
                if not provenance:
                    continue
                prov = provenance[0]
                page_no = int(getattr(prov, "page_no", 1) or 1)
                if page_no < 1 or page_no > len(pages):
                    continue
                bbox = getattr(prov, "bbox", None)
                if bbox is None:
                    continue

                label = str(getattr(item, "label", type(item).__name__))
                source_text = str(getattr(item, "text", "") or "").strip()
                category = _classify_region(label, source_text)
                if category is None:
                    continue

                page_image = pages[page_no - 1]
                logical_size = page_sizes.get(
                    page_no, (float(page_image.width), float(page_image.height))
                )
                pixel_bbox = _bbox_to_pixels(
                    bbox,
                    logical_size=logical_size,
                    image_size=(page_image.width, page_image.height),
                    padding=crop_padding_pixels,
                )
                if pixel_bbox is None:
                    continue
                x1, y1, x2, y2 = pixel_bbox
                crop = page_image.crop((x1, y1, x2, y2))
                if crop.width < 2 or crop.height < 2:
                    continue

                region_index += 1
                region_id = f"{document['document_id']}-p{page_no:04d}-r{region_index:06d}"
                crop_path = root / category / f"{region_id}.png"
                crop.save(crop_path, format="PNG", optimize=False)
                normalized = [
                    round((x1 / page_image.width) * 1000, 4),
                    round((y1 / page_image.height) * 1000, 4),
                    round((x2 / page_image.width) * 1000, 4),
                    round((y2 / page_image.height) * 1000, 4),
                ]
                regions.append(
                    {
                        "region_id": region_id,
                        "document_id": document["document_id"],
                        "document_name": document["name"],
                        "page": page_no,
                        "category": category,
                        "label": label,
                        "source_text": source_text,
                        "bbox_2d": [x1, y1, x2, y2],
                        "normalized_bbox": normalized,
                        "crop_file": str(crop_path),
                    }
                )

            for page in pages:
                try:
                    page.close()
                except Exception:
                    pass
            del pages
            del docling_document
            del conversion_result
            conversion_result = None
            gc.collect()

        master = {
            "version": 1,
            "documents": document_records,
            "regions": regions,
        }
        _write_json_atomic(
            Path(output_path),
            {"ok": True, "master": master},
        )
    except BaseException as exc:
        _write_json_atomic(
            Path(output_path),
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
        )
    finally:
        try:
            del conversion_result
            del converter
        except Exception:
            pass
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        gc.collect()


def _render_source_pages(path: Path, dpi: int) -> list[Any]:
    from PIL import Image, ImageOps

    if path.suffix.lower() == ".pdf":
        import fitz

        scale = dpi / 72.0
        matrix = fitz.Matrix(scale, scale)
        rendered: list[Any] = []
        with fitz.open(path) as pdf:
            for page in pdf:
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                rendered.append(
                    Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                )
        return rendered

    with Image.open(path) as opened:
        return [ImageOps.exif_transpose(opened).convert("RGB").copy()]


def _docling_page_sizes(document: Any) -> dict[int, tuple[float, float]]:
    result: dict[int, tuple[float, float]] = {}
    pages = getattr(document, "pages", {})
    iterable = pages.items() if isinstance(pages, dict) else enumerate(pages, start=1)
    for key, page in iterable:
        size = getattr(page, "size", None)
        width = getattr(size, "width", None)
        height = getattr(size, "height", None)
        if width and height:
            result[int(key)] = (float(width), float(height))
    return result


def _bbox_to_pixels(
    bbox: Any,
    *,
    logical_size: tuple[float, float],
    image_size: tuple[int, int],
    padding: int,
) -> tuple[int, int, int, int] | None:
    logical_width, logical_height = logical_size
    if logical_width <= 0 or logical_height <= 0:
        return None

    converted = bbox
    to_top_left = getattr(bbox, "to_top_left_origin", None)
    if callable(to_top_left):
        try:
            converted = to_top_left(page_height=logical_height)
        except TypeError:
            converted = to_top_left(logical_height)

    try:
        left = float(getattr(converted, "l"))
        top = float(getattr(converted, "t"))
        right = float(getattr(converted, "r"))
        bottom = float(getattr(converted, "b"))
    except (AttributeError, TypeError, ValueError):
        return None

    x_low, x_high = sorted((left, right))
    y_low, y_high = sorted((top, bottom))
    image_width, image_height = image_size
    x1 = max(0, int((x_low / logical_width) * image_width) - padding)
    x2 = min(image_width, int((x_high / logical_width) * image_width + 0.999) + padding)
    y1 = max(0, int((y_low / logical_height) * image_height) - padding)
    y2 = min(
        image_height,
        int((y_high / logical_height) * image_height + 0.999) + padding,
    )
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _classify_region(label: str, text: str) -> str | None:
    normalized = label.lower().replace("-", "_").replace(" ", "_")
    if "checkbox" in normalized or "check_box" in normalized:
        return "box"
    if any(
        token in normalized
        for token in ("barcode", "serial", "code", "formula", "key_value")
    ):
        return "sn"
    compact = text.replace(" ", "")
    if compact and _SERIAL_PATTERN.fullmatch(compact):
        return "sn"
    if any(
        token in normalized
        for token in (
            "text",
            "paragraph",
            "handwriting",
            "title",
            "caption",
            "list",
            "table",
            "form",
        )
    ):
        return "txt"
    return None


def _spawn_and_read(
    *,
    target: Callable[..., None],
    args: tuple[Any, ...],
    output_path: Path,
    timeout_seconds: float,
    stage: str,
    memory_limit_mb: int,
) -> None:
    try:
        output_path.unlink()
    except FileNotFoundError:
        pass
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=target,
        args=args,
        name=f"docuvision-{stage}",
        daemon=False,
    )
    process.start()
    deadline = time.monotonic() + timeout_seconds
    peak_rss_mb = 0.0
    while process.is_alive() and time.monotonic() < deadline:
        process.join(0.25)
        current_rss_mb = _process_tree_rss_mb(process.pid)
        peak_rss_mb = max(peak_rss_mb, current_rss_mb)
        if current_rss_mb > memory_limit_mb:
            _terminate_process(process)
            raise GatedExtractionError(
                stage,
                f"worker RSS {current_rss_mb:.1f} MB exceeded "
                f"{memory_limit_mb} MB limit",
            )
    if process.is_alive():
        _terminate_process(process)
        raise GatedExtractionError(
            stage, f"worker exceeded {timeout_seconds:.0f}s timeout"
        )
    if not output_path.is_file():
        raise GatedExtractionError(
            stage,
            f"worker exited with code {process.exitcode} without a result",
        )
    logger.info(
        "gated.worker_exit stage=%s exit_code=%s peak_rss_mb=%.1f limit_mb=%s",
        stage,
        process.exitcode,
        peak_rss_mb,
        memory_limit_mb,
    )


def _process_tree_rss_mb(pid: int | None) -> float:
    if pid is None:
        return 0.0
    try:
        import psutil

        process = psutil.Process(pid)
        processes = [process, *process.children(recursive=True)]
        return sum(
            child.memory_info().rss
            for child in processes
            if child.is_running()
        ) / (1024 * 1024)
    except Exception:
        return 0.0


def _terminate_process(process: multiprocessing.Process) -> None:
    process.terminate()
    process.join(10)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(5)


@contextmanager
def _exclusive_gpu_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    """Serialize local GPU model loads across threads and OS processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _GPU_THREAD_LOCK:
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + timeout_seconds
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise GatedExtractionError(
                                "gpu_gate", "timed out waiting for exclusive GPU"
                            )
                        time.sleep(0.25)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                deadline = time.monotonic() + timeout_seconds
                while True:
                    try:
                        fcntl.flock(
                            handle.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise GatedExtractionError(
                                "gpu_gate", "timed out waiting for exclusive GPU"
                            )
                        time.sleep(0.25)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_unique_name(name: str, index: int, used: set[str]) -> str:
    basename = Path(name).name
    safe = _SAFE_NAME.sub("_", basename).strip("._")
    if not safe:
        safe = f"document-{index + 1:04d}.bin"
    stem, suffix = Path(safe).stem, Path(safe).suffix
    candidate = safe
    counter = 1
    while candidate.lower() in used:
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def _iter_images(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
    )


def _contains_images(directory: Path) -> bool:
    return any(
        path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
        for path in directory.iterdir()
    )


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "DeferredGatedLayoutStage",
    "GatedExtractionError",
    "GatedExtractionPipeline",
    "GatedPipelineConfig",
    "GatedVisionExtractionStage",
    "LocalModelConfig",
    "RawDocument",
]
