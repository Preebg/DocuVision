"""Network-pluggable Ollama client targeting a LAN Mini PC endpoint.

VRAM protection: a process-wide ``asyncio.Semaphore(1)`` gates every chat /
vision request so concurrent CPU workers never schedule more than one
in-flight model run against the host GPU.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, TypeVar

import requests

from docuvision.config.settings import Settings
from docuvision.extraction_schema import (
    EXTRACTION_JSON_SCHEMA,
    parse_json_response,
)
from docuvision.registry.schema_registry import DocumentSchema, SchemaRegistry

logger = logging.getLogger(__name__)

OLLAMA_INFERENCE_OPTIONS = {
    "temperature": 0.0,
    "top_p": 0.1,
}

T = TypeVar("T")

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[misc, assignment]


class _VramExclusiveGate:
    """Serialize VRAM-bound Ollama calls with ``asyncio.Semaphore(1)``.

    A dedicated background event loop owns the semaphore so sync worker
    threads (and async callers) share one exclusive token across the process.
    """

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ollama-vram-gate",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("Failed to start Ollama VRAM gate event loop")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._semaphore = asyncio.Semaphore(1)
        self._ready.set()
        loop.run_forever()

    async def _gated(self, fn: Callable[[], T]) -> T:
        assert self._semaphore is not None
        async with self._semaphore:
            return await asyncio.to_thread(fn)

    def run(self, fn: Callable[[], T]) -> T:
        assert self._loop is not None
        future: Future[T] = asyncio.run_coroutine_threadsafe(
            self._gated(fn), self._loop
        )
        return future.result()

    async def run_async(self, fn: Callable[[], T]) -> T:
        """Async entry — still serializes through the shared semaphore."""
        assert self._loop is not None
        if asyncio.get_running_loop() is self._loop:
            return await self._gated(fn)
        future: Future[T] = asyncio.run_coroutine_threadsafe(
            self._gated(fn), self._loop
        )
        return await asyncio.wrap_future(future)


# Process-wide: at most one vision inference hits local VRAM at a time.
_VRAM_GATE: _VramExclusiveGate | None = None
_VRAM_GATE_LOCK = threading.Lock()


def _get_vram_gate() -> _VramExclusiveGate:
    global _VRAM_GATE
    with _VRAM_GATE_LOCK:
        if _VRAM_GATE is None:
            _VRAM_GATE = _VramExclusiveGate()
        return _VRAM_GATE


class ModelRecoverableError(RuntimeError):
    """Primary vision model failed due to OOM, timeout, or system crash."""


class OllamaVisionClient:
    """``VisionLLMClient`` backed by HTTP calls to ``DOCUVISION_OLLAMA_BASE_URL``.

    The worker laptop posts to the Mini PC; no local GPU is required here.
    All ``/api/chat`` traffic is funneled through ``asyncio.Semaphore(1)``.
    """

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._timeout = settings.ollama_timeout_seconds
        self._num_ctx = settings.ollama_num_ctx
        self._image_max_dimension = settings.ollama_image_max_dimension
        self._pdf_max_pages = settings.ollama_pdf_max_pages
        self._json_mode = settings.ollama_json_mode
        self._layout_max_chars = settings.layout_max_chars
        self._primary_model = settings.ollama_primary_model
        self._fallback_model = settings.ollama_fallback_model
        self._warmup = settings.ollama_warmup
        # (width, height) of the first processed page/image last sent to the VLM.
        self._last_processed_image_size: tuple[int, int] | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def primary_model(self) -> str:
        return self._primary_model

    @property
    def fallback_model(self) -> str:
        return self._fallback_model

    @property
    def last_processed_image_size(self) -> tuple[int, int] | None:
        """Pixel size of the first image encoded for the latest vision call."""
        return self._last_processed_image_size

    def extract_fields(
        self,
        local_path: str,
        layout_markdown: str,
        *,
        model: str | None = None,
        schema: DocumentSchema | None = None,
        registry: SchemaRegistry | None = None,
    ) -> dict[str, Any]:
        """POST images + layout markdown to Mini PC ``/api/chat``; return parsed JSON.

        When ``schema`` is provided, prompts and the Ollama ``format`` schema are
        built dynamically from the matched semantic field contract.
        """
        chosen_model = model or self._primary_model
        messages = self.build_vision_messages(
            local_path,
            layout_markdown,
            schema=schema,
            registry=registry,
        )
        if self._json_mode:
            if schema is not None:
                reg = registry or SchemaRegistry()
                response_format = reg.build_extraction_json_schema(schema)
            else:
                response_format = EXTRACTION_JSON_SCHEMA
        else:
            response_format = None
        ai_text = self.chat(chosen_model, messages, response_format=response_format)
        return parse_json_response(ai_text)

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Sync chat — exclusive via process-wide ``asyncio.Semaphore(1)``."""
        return _get_vram_gate().run(
            lambda: self._chat_http(model, messages, response_format)
        )

    async def achat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Async chat — same VRAM exclusivity as ``chat``."""
        return await _get_vram_gate().run_async(
            lambda: self._chat_http(model, messages, response_format)
        )

    def _chat_http(
        self,
        model: str,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_ctx": self._num_ctx,
                **OLLAMA_INFERENCE_OPTIONS,
            },
        }
        if response_format is not None:
            payload["format"] = response_format

        url = f"{self._base_url}/api/chat"
        try:
            response = requests.post(url, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            if self.is_recoverable_failure(exc):
                raise ModelRecoverableError(
                    f"Model '{model}' request failed: {exc}"
                ) from exc
            raise RuntimeError(
                f"Could not reach Ollama at {self._base_url}. "
                "Confirm the Mini PC is online and Ollama is listening on :11434."
            ) from exc

        try:
            response_data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Ollama returned a non-JSON response (HTTP {response.status_code}): "
                f"{response.text[:500]}"
            ) from exc

        if response.status_code != 200:
            error_message = response_data.get("error") or response_data
            wrapped = RuntimeError(
                f"Ollama request failed (HTTP {response.status_code}) for model "
                f"'{model}': {error_message}"
            )
            if self.is_recoverable_failure(wrapped):
                raise ModelRecoverableError(str(wrapped)) from wrapped
            raise wrapped

        if "error" in response_data:
            wrapped = RuntimeError(
                f"Ollama error for model '{model}': {response_data['error']}"
            )
            if self.is_recoverable_failure(wrapped):
                raise ModelRecoverableError(str(wrapped)) from wrapped
            raise wrapped

        message = response_data.get("message")
        if not message or "content" not in message:
            raise RuntimeError(
                "Ollama returned an unexpected response shape. "
                f"Response keys: {list(response_data.keys())}."
            )
        return message["content"]

    def list_models(self) -> list[str]:
        try:
            response = requests.get(f"{self._base_url}/api/tags", timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {self._base_url}. "
                "Confirm the Mini PC IP and that `ollama serve` is running."
            ) from exc
        payload = response.json()
        return [model["name"] for model in payload.get("models", [])]

    def validate_access(self) -> None:
        installed = self.list_models()
        logger.info("ollama.endpoint=%s", self._base_url)
        logger.info("ollama.primary_model=%s", self._primary_model)
        logger.info("ollama.fallback_model=%s", self._fallback_model)
        logger.info(
            "ollama.installed=%s",
            ", ".join(installed) if installed else "(none)",
        )
        self._require_model(self._primary_model, installed, "Primary")
        self._require_model(self._fallback_model, installed, "Fallback")

        if not self._warmup:
            logger.info("ollama.warmup skipped")
            return

        logger.info("ollama.warmup starting model=%s", self._primary_model)
        text = self.chat(
            self._primary_model,
            [{"role": "user", "content": 'Reply with exactly {"ok": true}'}],
            response_format=EXTRACTION_JSON_SCHEMA if self._json_mode else None,
        )
        logger.info("ollama.warmup ok snippet=%s", text[:120])

    def build_vision_messages(
        self,
        local_path: str,
        layout_markdown: str,
        *,
        schema: DocumentSchema | None = None,
        registry: SchemaRegistry | None = None,
    ) -> list[dict[str, Any]]:
        if schema is not None:
            reg = registry or SchemaRegistry()
            system = reg.build_extraction_system_prompt(schema)
            user = reg.build_extraction_user_prompt(
                schema,
                layout_markdown,
                layout_max_chars=self._layout_max_chars,
            )
        else:
            system = (
                "You are a document OCR and extraction engine. "
                "Read the attached page image(s) and bundled layout text. "
                "Return only valid JSON matching the required schema."
            )
            user = self._bundle_layout_prompt(layout_markdown)

        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": user,
                "images": self.encode_images_base64(local_path),
            },
        ]

    def encode_images_base64(self, file_path: str) -> list[str]:
        encoded: list[str] = []
        self._last_processed_image_size = None
        for image_bytes, size_label, width, height in self._prepare_images(file_path):
            if self._last_processed_image_size is None:
                self._last_processed_image_size = (width, height)
            logger.info(
                "ollama.image prepared label=%s bytes=%s size=%sx%s",
                size_label,
                len(image_bytes),
                width,
                height,
            )
            encoded.append(base64.b64encode(image_bytes).decode("utf-8"))
        return encoded

    def _bundle_layout_prompt(self, markdown_layout: str) -> str:
        layout_text = markdown_layout.strip()
        if len(layout_text) > self._layout_max_chars:
            layout_text = layout_text[: self._layout_max_chars] + "\n...[truncated]"

        return (
            "You are given a document page image payload and its Docling structural "
            "markdown layout.\n"
            "Use BOTH the attached image(s) and the bundled layout text together.\n"
            "Return JSON only that strictly matches the required schema:\n"
            '{"patient_name":"...","dob":"...","form_type":"...",'
            '"extracted_payload":{"full_text":"...","fields":{},"summary":"..."}}\n'
            "patient_name, dob, and form_type are mandatory non-empty strings.\n"
            "Put every readable word into full_text. Do not leave it empty.\n\n"
            "Bundled Docling structural markdown layout:\n"
            f"{layout_text if layout_text else '(no layout text extracted)'}"
        )

    def _prepare_images(self, file_path: str) -> list[tuple[bytes, str, int, int]]:
        path = Path(file_path)
        if path.suffix.lower() == ".pdf":
            return self._render_pdf_pages(file_path)

        if Image is None:
            with open(file_path, "rb") as handle:
                data = handle.read()
            return [(data, "original", 0, 0)]

        with Image.open(file_path) as image:
            image = image.convert("RGB")
            width, height = image.size
            max_dim = self._image_max_dimension

            if max(width, height) > max_dim:
                scale = max_dim / max(width, height)
                resized = (max(1, int(width * scale)), max(1, int(height * scale)))
                image = image.resize(resized, Image.Resampling.LANCZOS)
                out_w, out_h = resized
                size_label = f"{width}x{height} -> {out_w}x{out_h}"
            else:
                out_w, out_h = width, height
                size_label = f"{width}x{height}"

            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85, optimize=True)
            return [(buffer.getvalue(), size_label, out_w, out_h)]

    def _render_pdf_pages(self, pdf_path: str) -> list[tuple[bytes, str, int, int]]:
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError(
                "PDF support requires PyMuPDF. Install with: pip install pymupdf"
            ) from exc

        rendered: list[tuple[bytes, str, int, int]] = []
        with fitz.open(pdf_path) as doc:
            page_count = len(doc)
            pages_to_render = min(page_count, self._pdf_max_pages)
            if page_count > self._pdf_max_pages:
                logger.info(
                    "ollama.pdf truncated pages=%s max=%s",
                    page_count,
                    self._pdf_max_pages,
                )

            for page_index in range(pages_to_render):
                page = doc[page_index]
                rect = page.rect
                if rect.width <= 0 or rect.height <= 0:
                    continue

                scale = min(
                    self._image_max_dimension / rect.width,
                    self._image_max_dimension / rect.height,
                    2.0,
                )
                scale = max(scale, 0.1)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                label = (
                    f"pdf page {page_index + 1}/{page_count} "
                    f"({pixmap.width}x{pixmap.height})"
                )
                rendered.append(
                    (pixmap.tobytes("jpeg"), label, pixmap.width, pixmap.height)
                )

        if not rendered:
            raise RuntimeError("PDF did not contain any renderable pages.")
        return rendered

    @staticmethod
    def is_recoverable_failure(exc: BaseException) -> bool:
        if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return True
        message = str(exc).lower()
        markers = (
            "out of memory",
            "oom",
            "cuda",
            "vram",
            "memory",
            "timeout",
            "timed out",
            "resource exhausted",
            "model runner has terminated",
            "crashed",
            "killed",
            "500 internal server error",
            "server error",
        )
        return any(marker in message for marker in markers)

    @staticmethod
    def _require_model(model_name: str, installed: list[str], label: str) -> None:
        if model_name in installed:
            return
        suggestion = next(
            (
                candidate
                for candidate in installed
                if model_name.lower() in candidate.lower()
                or candidate.lower() in model_name.lower()
            ),
            installed[0] if installed else None,
        )
        hint = f"\nTry instead: {suggestion}" if suggestion else ""
        raise SystemExit(
            f"{label} model '{model_name}' is not installed on the Mini PC.{hint}\n"
            f"Run on the Mini PC: ollama pull {model_name}"
        )
