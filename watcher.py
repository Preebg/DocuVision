import time
import requests
import json
import os
import base64
import io
import tomllib
from pathlib import Path
from docling.document_converter import DocumentConverter
from supabase import create_client, Client

try:
    from PIL import Image
except ImportError:
    Image = None


def get_supabase_key_role(key: str) -> str:
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("role", "unknown")
    except Exception:
        return "unknown"


def load_supabase_credentials():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")

    if not url or not key:
        secrets_path = Path(__file__).with_name("secrets.toml")
        if secrets_path.exists():
            with secrets_path.open("rb") as secrets_file:
                secrets = tomllib.load(secrets_file)
            url = url or secrets.get("SUPABASE_URL")
            key = key or secrets.get("SUPABASE_KEY")

    if not url or not key:
        raise SystemExit(
            "Missing Supabase credentials. Set SUPABASE_URL and SUPABASE_KEY "
            "as environment variables or add them to secrets.toml."
        )

    return url, key


# 1. Setup Environment Connections
SUPABASE_URL, SUPABASE_KEY = load_supabase_credentials()
SUPABASE_KEY_ROLE = get_supabase_key_role(SUPABASE_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize Docling Layout Engine lazily (first run downloads models and can take a while)
doc_converter = None


def get_doc_converter():
    global doc_converter
    if doc_converter is None:
        doc_converter = DocumentConverter()
    return doc_converter


def get_ollama_config():
    return {
        "url": os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        "model": os.getenv("OLLAMA_MODEL", "qwen2.5vl:3b"),
        "timeout": int(os.getenv("OLLAMA_TIMEOUT", "300")),
        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192")),
        "image_max_dimension": int(os.getenv("OLLAMA_IMAGE_MAX_DIMENSION", "1280")),
        "json_mode": os.getenv("OLLAMA_JSON_MODE", "true").lower() in {"1", "true", "yes"},
        "warmup": os.getenv("OLLAMA_WARMUP", "true").lower() in {"1", "true", "yes"},
    }


def list_ollama_models():
    config = get_ollama_config()
    try:
        response = requests.get(f"{config['url']}/api/tags", timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(
            f"Could not reach Ollama at {config['url']}. "
            "Start the Ollama app or run `ollama serve` in a separate terminal."
        ) from e

    payload = response.json()
    return [model["name"] for model in payload.get("models", [])]


def suggest_ollama_model_name(requested_model, installed_models):
    if not installed_models:
        return None

    requested_lower = requested_model.lower()
    for model in installed_models:
        if model.lower() == requested_lower:
            return model

    # Common typo: qwen2.5v1:3b vs qwen2.5vl:3b
    normalized = requested_lower.replace("v1", "vl")
    for model in installed_models:
        if model.lower() == normalized:
            return model

    for model in installed_models:
        if requested_lower in model.lower() or model.lower() in requested_lower:
            return model

    return installed_models[0]


def validate_ollama_access():
    config = get_ollama_config()
    installed_models = list_ollama_models()
    print(f"Ollama endpoint: {config['url']}")
    print(f"Configured model: {config['model']}")
    print(f"Context window (num_ctx): {config['num_ctx']}")
    print(f"Installed models: {', '.join(installed_models) if installed_models else '(none)'}")

    if config["model"] not in installed_models:
        suggestion = suggest_ollama_model_name(config["model"], installed_models)
        typo_hint = ""
        if config["model"].lower().replace("v1", "vl") != config["model"].lower():
            typo_hint = (
                "\nDid you mean 'qwen2.5vl:3b'? "
                "The model uses the letter L in 'vl', not the number 1 in 'v1'."
            )
        suggestion_hint = f"\nTry instead: OLLAMA_MODEL={suggestion}" if suggestion else ""
        raise SystemExit(
            f"Configured Ollama model '{config['model']}' is not installed.{typo_hint}"
            f"{suggestion_hint}\n"
            f"Run: ollama pull qwen2.5vl:3b"
        )

    if not config["warmup"]:
        print("Ollama warmup skipped.")
        return

    print("Warming up Ollama model (first load can take a minute)...")
    warmup_text = call_ollama_chat(
        [{"role": "user", "content": 'Reply with exactly {"ok": true}'}],
        response_format="json" if config["json_mode"] else None,
    )
    print(f"Ollama warmup succeeded: {warmup_text[:120]}")


def call_ollama_chat(messages, response_format="json"):
    config = get_ollama_config()
    ollama_payload = {
        "model": config["model"],
        "messages": messages,
        "stream": False,
    }
    if response_format:
        ollama_payload["format"] = response_format
    ollama_payload["options"] = {"num_ctx": config["num_ctx"]}

    try:
        ollama_res = requests.post(
            f"{config['url']}/api/chat",
            json=ollama_payload,
            timeout=config["timeout"],
        )
    except requests.RequestException as e:
        raise RuntimeError(
            f"Could not reach Ollama at {config['url']}. "
            "Make sure the Ollama app is running on this machine."
        ) from e

    try:
        response_data = ollama_res.json()
    except ValueError as e:
        raise RuntimeError(
            f"Ollama returned a non-JSON response (HTTP {ollama_res.status_code}): "
            f"{ollama_res.text[:500]}"
        ) from e

    if ollama_res.status_code != 200:
        error_message = response_data.get("error") or response_data
        raise RuntimeError(
            f"Ollama request failed (HTTP {ollama_res.status_code}) for model "
            f"'{config['model']}': {error_message}"
        )

    if "error" in response_data:
        raise RuntimeError(f"Ollama error for model '{config['model']}': {response_data['error']}")

    message = response_data.get("message")
    if not message or "content" not in message:
        raise RuntimeError(
            "Ollama returned an unexpected response shape. "
            f"Response keys: {list(response_data.keys())}. "
            f"Full response: {response_data}"
        )

    return message["content"]

def prepare_image_bytes_for_ollama(image_path):
    config = get_ollama_config()
    max_dimension = config["image_max_dimension"]

    if Image is None:
        with open(image_path, "rb") as image_file:
            return image_file.read(), "original"

    with Image.open(image_path) as image:
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
        return buffer.getvalue(), size_label


def encode_image_base64(image_path):
    image_bytes, size_label = prepare_image_bytes_for_ollama(image_path)
    print(f"Prepared image for Ollama vision input ({size_label}, {len(image_bytes)} bytes).")
    return base64.b64encode(image_bytes).decode("utf-8")


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


def extract_document_with_ollama(image_path, markdown_layout):
    ollama_config = get_ollama_config()
    print(
        f"Stage 2: Analyzing image with Ollama model "
        f"'{ollama_config['model']}' at {ollama_config['url']}..."
    )

    docling_chars = len(markdown_layout.strip())
    print(f"Docling OCR extracted {docling_chars} characters of markdown text.")

    user_prompt = (
        "Read all visible text in this image and return JSON only:\n"
        '{"extracted_payload":{"full_text":"...","fields":{},"summary":"..."}}\n'
        "Put every readable word into full_text. Do not leave it empty."
    )

    if docling_chars > 0:
        markdown_hint = markdown_layout.strip()
        if len(markdown_hint) > 1500:
            markdown_hint = markdown_hint[:1500] + "\n...[truncated]"
        user_prompt += (
            "\n\nOptional OCR hint from Docling (trust the image if different):\n"
            f"{markdown_hint}"
        )

    ai_text = call_ollama_chat(
        [
            {
                "role": "system",
                "content": (
                    "You are a document OCR and extraction engine. "
                    "You must read the attached image directly and return only valid JSON."
                ),
            },
            {
                "role": "user",
                "content": user_prompt,
                "images": [encode_image_base64(image_path)],
            },
        ],
        response_format="json" if ollama_config["json_mode"] else None,
    )
    parsed_json = parse_json_response(ai_text)

    if is_empty_extraction(parsed_json):
        raise RuntimeError(
            "Ollama returned empty extracted text. "
            f"Raw model response: {ai_text[:500]}"
        )

    return parsed_json


def list_incoming_form_files():
    return supabase.storage.from_("incoming_forms").list()


def is_processable_upload(file_entry):
    file_name = file_entry.get("name", "")
    if not file_name or file_name == ".emptyFolderPlaceholder":
        return False

    metadata = file_entry.get("metadata") or {}
    if metadata.get("mimetype", "").startswith("image/"):
        return True

    # Root-level uploads are files; nested entries without metadata are folders.
    return file_entry.get("id") is not None


def validate_supabase_access():
    print(f"Connected to Supabase project: {SUPABASE_URL}")
    print(f"Using Supabase key role: {SUPABASE_KEY_ROLE}")

    if SUPABASE_KEY_ROLE != "service_role":
        print(
            "[WARNING] This watcher needs the service_role key in secrets.toml. "
            "The anon/publishable key cannot list private bucket files because "
            "incoming_forms only allows authenticated users."
        )

    try:
        files = list_incoming_form_files()
    except Exception as e:
        raise SystemExit(f"Could not reach Supabase storage: {e}") from e

    uploads = [file for file in files if is_processable_upload(file)]
    print(f"Startup scan: {len(uploads)} image(s) waiting in incoming_forms")
    return uploads


def listen_for_phone_uploads():
    try:
        files = list_incoming_form_files()
    except Exception as e:
        print(f"Connection error to Supabase: {e}")
        return

    uploads = [file for file in files if is_processable_upload(file)]
    if uploads:
        print(f"[SCAN] Found {len(uploads)} image(s) in incoming_forms")

    for file in uploads:
        file_name = file["name"]
        print(f"\n[NEW CAPTURE] Found phone image upload: {file_name}")
        process_pipeline(file_name)

def process_pipeline(file_name):
    local_temp_path = f"./temp_{file_name}"
    pipeline_succeeded = False

    try:
        # Step A: Download photo from cloud bucket down onto spare PC hardware
        print("Downloading form from secure storage queue...")
        with open(local_temp_path, "wb") as f:
            res = supabase.storage.from_("incoming_forms").download(file_name)
            f.write(res)

        # Step B: Execute Docling local layout/structural analysis
        print("Stage 1: Running Docling structural layout conversion...")
        conversion_result = get_doc_converter().convert(local_temp_path)
        markdown_layout = conversion_result.document.export_to_markdown()

        # Step C: Send the actual image to the vision model for text extraction
        parsed_json = extract_document_with_ollama(local_temp_path, markdown_layout)

        # Step D: Save perfect structural payload back up to your tracking database
        print("Stage 3: Storing structural JSON data back into Supabase database...")
        supabase.table("document_pipeline").insert({
            "file_name": file_name,
            "extracted_payload": parsed_json,
        }).execute()

        pipeline_succeeded = True
        print(f"[SUCCESS] Form processing complete for {file_name}. Logged to database.")

    except Exception as e:
        print(f"[PIPELINE CRASH] Failed processing {file_name}: {e}")
        print(
            "The file was left in incoming_forms so you can retry after fixing the issue."
        )

    finally:
        if os.path.exists(local_temp_path):
            os.remove(local_temp_path)

        if pipeline_succeeded:
            try:
                supabase.storage.from_("incoming_forms").remove([file_name])
            except Exception as e:
                print(f"[WARNING] Processed file but could not remove it from storage: {e}")

if __name__ == "__main__":
    print("=== Secure On-Premises Docling + Qwen 2.5 Pipeline Online ===")
    print(
        "Run this script with the project venv activated. "
        "Ollama itself is a separate app and does not use the venv."
    )
    validate_supabase_access()
    validate_ollama_access()
    while True:
        listen_for_phone_uploads()
        time.sleep(3)  # Scan your queue bucket every 3 seconds