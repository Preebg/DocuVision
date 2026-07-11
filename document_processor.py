import base64
import io
import json
import os
from pathlib import Path

from docling.document_converter import DocumentConverter

from config import EXTRACTION_JSON_SCHEMA, FALLBACK_MODEL, PRIMARY_MODEL, get_ollama_config
from ollama_client import ModelRecoverableError, call_ollama_chat

try:
    from PIL import Image
except ImportError:
    Image = None


doc_converter = None


def get_doc_converter():
    global doc_converter
    if doc_converter is None:
        doc_converter = DocumentConverter()
    return doc_converter


def is_pdf_path(file_path):
    return Path(file_path).suffix.lower() == ".pdf"


def render_pdf_pages_for_ollama(pdf_path):
    try:
        import fitz
    except ImportError as e:
        raise RuntimeError(
            "PDF support requires PyMuPDF. Install it with: pip install pymupdf"
        ) from e

    config = get_ollama_config()
    max_dimension = config["image_max_dimension"]
    max_pages = int(os.getenv("OLLAMA_PDF_MAX_PAGES", "5"))

    rendered_pages = []
    with fitz.open(pdf_path) as doc:
        page_count = len(doc)
        pages_to_render = min(page_count, max_pages)
        if page_count > max_pages:
            print(
                f"PDF has {page_count} pages; sending the first {max_pages} "
                "page image(s) to Ollama."
            )

        for page_index in range(pages_to_render):
            page = doc[page_index]
            rect = page.rect
            if rect.width <= 0 or rect.height <= 0:
                continue

            scale = min(max_dimension / rect.width, max_dimension / rect.height, 2.0)
            scale = max(scale, 0.1)
            matrix = fitz.Matrix(scale, scale)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            image_bytes = pixmap.tobytes("jpeg")
            label = (
                f"pdf page {page_index + 1}/{page_count} "
                f"({pixmap.width}x{pixmap.height})"
            )
            rendered_pages.append((image_bytes, label))

    if not rendered_pages:
        raise RuntimeError("PDF did not contain any renderable pages.")

    return rendered_pages


def prepare_images_for_ollama(file_path):
    if is_pdf_path(file_path):
        return render_pdf_pages_for_ollama(file_path)

    config = get_ollama_config()
    max_dimension = config["image_max_dimension"]

    if Image is None:
        with open(file_path, "rb") as image_file:
            return [(image_file.read(), "original")]

    with Image.open(file_path) as image:
        image = image.convert("RGB")
        width, height = image.size

        if max(width, height) > max_dimension:
            scale = max_dimension / max(width, height)
            resized = (
                max(1, int(width * scale)),
                max(1, int(height * scale)),
            )
            image = image.resize(resized, Image.Resampling.LANCZOS)
            size_label = f"{width}x{height} -> {resized[0]}x{resized[1]}"
        else:
            size_label = f"{width}x{height}"

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85, optimize=True)
        return [(buffer.getvalue(), size_label)]


def encode_images_base64(file_path):
    encoded_images = []
    for image_bytes, size_label in prepare_images_for_ollama(file_path):
        print(f"Prepared vision input ({size_label}, {len(image_bytes)} bytes).")
        encoded_images.append(base64.b64encode(image_bytes).decode("utf-8"))
    return encoded_images


def parse_json_response(ai_text):
    try:
        return json.loads(ai_text)
    except json.JSONDecodeError:
        start = ai_text.find("{")
        end = ai_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(ai_text[start : end + 1])


def is_empty_extraction(parsed_json):
    if not parsed_json:
        return True

    payload = parsed_json.get("extracted_payload", parsed_json)
    if isinstance(payload, str):
        return not payload.strip()

    if isinstance(payload, dict):
        text_values = [
            str(value).strip()
            for value in payload.values()
            if isinstance(value, (str, int, float))
        ]
        return not any(text_values)

    return False


def bundle_layout_with_prompt(markdown_layout: str) -> str:
    config = get_ollama_config()
    layout_text = markdown_layout.strip()
    if len(layout_text) > config["layout_max_chars"]:
        layout_text = layout_text[: config["layout_max_chars"]] + "\n...[truncated]"

    return (
        "You are given a document page image payload and its Docling structural markdown layout.\n"
        "Use BOTH the attached image(s) and the bundled layout text together.\n"
        "Return JSON only that strictly matches the required schema:\n"
        '{"extracted_payload":{"full_text":"...","fields":{},"summary":"..."}}\n'
        "Put every readable word into full_text. Do not leave it empty.\n\n"
        "Bundled Docling structural markdown layout:\n"
        f"{layout_text if layout_text else '(no layout text extracted)'}"
    )


def build_vision_messages(file_path: str, markdown_layout: str):
    return [
        {
            "role": "system",
            "content": (
                "You are a document OCR and extraction engine. "
                "Read the attached page image(s) and bundled layout text. "
                "Return only valid JSON matching the required schema."
            ),
        },
        {
            "role": "user",
            "content": bundle_layout_with_prompt(markdown_layout),
            "images": encode_images_base64(file_path),
        },
    ]


def extract_with_ollama_model(file_path: str, markdown_layout: str, model: str):
    config = get_ollama_config()
    document_type = "PDF" if is_pdf_path(file_path) else "image"
    print(
        f"STEP 2: Running vision extraction on {document_type} with "
        f"Ollama model '{model}'..."
    )

    docling_chars = len(markdown_layout.strip())
    print(f"Docling layout bundled with image payload ({docling_chars} characters).")

    response_format = EXTRACTION_JSON_SCHEMA if config["json_mode"] else None
    ai_text = call_ollama_chat(
        model,
        build_vision_messages(file_path, markdown_layout),
        response_format=response_format,
    )
    parsed_json = parse_json_response(ai_text)

    if is_empty_extraction(parsed_json):
        raise RuntimeError(
            f"Model '{model}' returned empty extracted text. "
            f"Raw model response: {ai_text[:500]}"
        )

    return parsed_json


def step1_extract_layout(local_path: str) -> str:
    print("STEP 1: Running Docling structural layout extraction...")
    conversion_result = get_doc_converter().convert(local_path)
    markdown_layout = conversion_result.document.export_to_markdown()
    print(f"STEP 1 complete: extracted {len(markdown_layout.strip())} layout characters.")
    return markdown_layout


def step2_and_3_vision_extraction(local_path: str, markdown_layout: str):
    try:
        return extract_with_ollama_model(local_path, markdown_layout, PRIMARY_MODEL)
    except ModelRecoverableError:
        print(
            "[FALLBACK] Qwen3-8B maxed out VRAM or timed out. "
            "Falling back to memory-safe recovery..."
        )
        return extract_with_ollama_model(local_path, markdown_layout, FALLBACK_MODEL)
