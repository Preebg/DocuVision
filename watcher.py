import time
import requests
import json
import os
import base64
import tomllib
from pathlib import Path
from docling.document_converter import DocumentConverter
from supabase import create_client, Client


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
    
    try:
        # Step A: Download photo from cloud bucket down onto spare PC hardware
        print("Downloading form from secure storage queue...")
        with open(local_temp_path, "wb") as f:
            res = supabase.storage.from_('incoming_forms').download(file_name)
            f.write(res)

        # Step B: Execute Docling local layout/structural analysis
        print("Stage 1: Running Docling structural layout conversion...")
        conversion_result = get_doc_converter().convert(local_temp_path)
        markdown_layout = conversion_result.document.export_to_markdown()

        # Step C: Pipe clean text structure directly into Qwen 2.5 via local Ollama port
        print("Stage 2: Passing layout payload to Qwen via local Ollama...")
        system_prompt = (
            "You are a strict data parsing engine. Read this Markdown text extracted from a form. "
            "Convert it into a valid JSON object with describing what is in the image. add the description to the 'extracted_payload' key. Do not add any extra text or commentary. "
        )
        
        ollama_payload = {
            "model": "qwen2.5-vl:3b", # Ensure this matches your downloaded tag exactly
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Document Text Structure:\n\n{markdown_layout}"}
            ],
            "stream": False,
            "format": "json"
        }
        
        ollama_res = requests.post("http://localhost:11434/api/chat", json=ollama_payload, timeout=60)
        ai_text = ollama_res.json()['message']['content']
        parsed_json = json.loads(ai_text)

        # Step D: Save perfect structural payload back up to your tracking database
        print("Stage 3: Storing structural JSON data back into Supabase database...")
        supabase.table("document_pipeline").insert({
            "file_name": file_name,
            "extracted_payload": parsed_json
        }).execute()

        print(f"[SUCCESS] Form processing complete for {file_name}. Logged to database.")

    except Exception as e:
        print(f"[PIPELINE CRASH] Failed processing: {e}")
        
    finally:
        # Clean up local temp file and purge bucket so it won't loop process
        if os.path.exists(local_temp_path):
            os.remove(local_temp_path)
        try:
            supabase.storage.from_('incoming_forms').remove([file_name])
        except Exception:
            pass

if __name__ == "__main__":
    print("=== Secure On-Premises Docling + Qwen 2.5 Pipeline Online ===")
    validate_supabase_access()
    while True:
        listen_for_phone_uploads()
        time.sleep(3)  # Scan your queue bucket every 3 seconds