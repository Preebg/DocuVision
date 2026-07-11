import asyncio
import os
import traceback
from pathlib import Path

from config import (
    PIPELINE_STATUS_FAILED,
    PIPELINE_STATUS_PENDING_VERIFICATION,
    SUPABASE_KEY_ROLE,
    SUPABASE_URL,
    VERIFICATION_STORAGE_BUCKET,
    supabase,
)
from document_processor import step1_extract_layout, step2_and_3_vision_extraction
from ollama_client import validate_ollama_access


def list_incoming_form_files():
    return supabase.storage.from_("incoming_forms").list()


def is_processable_upload(file_entry):
    file_name = file_entry.get("name", "")
    if not file_name or file_name == ".emptyFolderPlaceholder":
        return False

    suffix = Path(file_name).suffix.lower()
    if suffix in {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}:
        return True

    metadata = file_entry.get("metadata") or {}
    mimetype = metadata.get("mimetype", "")
    if mimetype.startswith(("image/", "application/pdf")):
        return True

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
    print(f"Startup scan: {len(uploads)} document(s) waiting in incoming_forms")
    return uploads


def download_incoming_file(file_name: str, local_temp_path: str) -> None:
    print("Downloading form from secure storage queue...")
    with open(local_temp_path, "wb") as file_handle:
        response = supabase.storage.from_("incoming_forms").download(file_name)
        file_handle.write(response)


def stage_file_for_verification(file_name: str, local_temp_path: str) -> None:
    print(f"Staging {file_name} in {VERIFICATION_STORAGE_BUCKET} for HITL review...")
    with open(local_temp_path, "rb") as file_handle:
        supabase.storage.from_(VERIFICATION_STORAGE_BUCKET).upload(
            file_name,
            file_handle.read(),
            {"upsert": "true", "content-type": "application/octet-stream"},
        )


def record_pipeline_success(file_name: str, parsed_json: dict) -> None:
    print("STEP 4: Storing successful extraction in document_pipeline...")
    supabase.table("document_pipeline").insert(
        {
            "file_name": file_name,
            "extracted_payload": parsed_json,
            "status": PIPELINE_STATUS_PENDING_VERIFICATION,
            "storage_bucket": VERIFICATION_STORAGE_BUCKET,
            "error_message": None,
        }
    ).execute()


def record_pipeline_failure(file_name: str, error_log: str) -> None:
    print("STEP 4: Marking document_pipeline row as FAILED...")
    supabase.table("document_pipeline").insert(
        {
            "file_name": file_name,
            "status": PIPELINE_STATUS_FAILED,
            "error_message": error_log,
        }
    ).execute()


def remove_processed_file(file_name: str) -> None:
    supabase.storage.from_("incoming_forms").remove([file_name])


async def process_document(file_name: str) -> None:
    local_temp_path = f"./temp_{file_name}"
    pipeline_succeeded = False

    print(f"\n[NEW CAPTURE] Processing document: {file_name}")

    try:
        await asyncio.to_thread(download_incoming_file, file_name, local_temp_path)
        markdown_layout = await asyncio.to_thread(step1_extract_layout, local_temp_path)
        parsed_json = await asyncio.to_thread(
            step2_and_3_vision_extraction,
            local_temp_path,
            markdown_layout,
        )
        await asyncio.to_thread(
            stage_file_for_verification, file_name, local_temp_path
        )
        await asyncio.to_thread(record_pipeline_success, file_name, parsed_json)

        pipeline_succeeded = True
        print(f"[SUCCESS] Pipeline complete for {file_name}. Logged to database.")

    except Exception as exc:
        error_log = traceback.format_exc()
        print(f"[PIPELINE FAILURE] {file_name} could not be processed: {exc}")
        print(
            "The original file remains in incoming_forms for manual Scan Operator review."
        )

        try:
            await asyncio.to_thread(record_pipeline_failure, file_name, error_log)
            print(f"[STATE] document_pipeline status set to FAILED for {file_name}.")
        except Exception as state_error:
            print(
                f"[STATE ERROR] Could not record FAILED status for {file_name}: "
                f"{state_error}"
            )

    finally:
        if os.path.exists(local_temp_path):
            os.remove(local_temp_path)

        if pipeline_succeeded:
            try:
                await asyncio.to_thread(remove_processed_file, file_name)
            except Exception as remove_error:
                print(
                    f"[WARNING] Processed file but could not remove it from storage: "
                    f"{remove_error}"
                )


async def poll_and_process_one() -> None:
    try:
        files = await asyncio.to_thread(list_incoming_form_files)
    except Exception as exc:
        print(f"Connection error to Supabase: {exc}")
        return

    uploads = [file_entry for file_entry in files if is_processable_upload(file_entry)]
    if not uploads:
        return

    queued_count = len(uploads)
    next_file = uploads[0]["name"]
    print(f"[SCAN] {queued_count} document(s) queued; processing one: {next_file}")
    await process_document(next_file)


async def run_watcher() -> None:
    poll_interval = int(os.getenv("WATCHER_POLL_INTERVAL", "3"))
    while True:
        await poll_and_process_one()
        await asyncio.sleep(poll_interval)


def main() -> None:
    print("=== Secure On-Premises Docling + Qwen Vision Pipeline Online ===")
    print(
        "Run this script with the project venv activated. "
        "Ollama itself is a separate app and does not use the venv."
    )
    validate_supabase_access()
    validate_ollama_access()
    asyncio.run(run_watcher())


if __name__ == "__main__":
    main()
