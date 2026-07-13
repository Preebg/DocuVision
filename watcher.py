"""DocuVision secure document ingestion watcher (pipeline runner)."""

from __future__ import annotations

import asyncio
import os
import traceback
from pathlib import Path

from config import (
    PIPELINE_STATUS_FAILED,
    PIPELINE_STATUS_PENDING_VERIFICATION,
    VERIFICATION_STORAGE_BUCKET,
    get_supabase,
    get_supabase_key_role_cached,
    get_supabase_url,
)
from document_processor import step1_extract_layout, step2_and_3_vision_extraction
from observability import (
    configure_structured_logging,
    derive_correlation_id,
    get_logger,
    reset_correlation_id,
    set_correlation_id,
)
from ollama_client import validate_ollama_access
from temp_files import create_ingestion_temp_file, safe_remove

logger = get_logger(__name__)


def list_incoming_form_files():
    return get_supabase().storage.from_("incoming_forms").list()


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
    url = get_supabase_url()
    key_role = get_supabase_key_role_cached()
    logger.info("Connected to Supabase project: %s", url)
    logger.info("Using Supabase key role: %s", key_role)

    if key_role != "service_role":
        logger.warning(
            "This watcher needs the service_role key in secrets.toml. "
            "The anon/publishable key cannot list private bucket files because "
            "incoming_forms only allows authenticated users."
        )

    try:
        files = list_incoming_form_files()
    except Exception as e:
        raise SystemExit(f"Could not reach Supabase storage: {e}") from e

    uploads = [file for file in files if is_processable_upload(file)]
    logger.info(
        "Startup scan: %s document(s) waiting in incoming_forms",
        len(uploads),
    )
    return uploads


def download_incoming_file(file_name: str, local_temp_path: str) -> None:
    logger.info("Downloading form from secure storage queue...")
    with open(local_temp_path, "wb") as file_handle:
        response = get_supabase().storage.from_("incoming_forms").download(file_name)
        file_handle.write(response)


def stage_file_for_verification(file_name: str, local_temp_path: str) -> None:
    logger.info(
        "Staging %s in %s for HITL review...",
        file_name,
        VERIFICATION_STORAGE_BUCKET,
    )
    with open(local_temp_path, "rb") as file_handle:
        get_supabase().storage.from_(VERIFICATION_STORAGE_BUCKET).upload(
            file_name,
            file_handle.read(),
            {"upsert": "true", "content-type": "application/octet-stream"},
        )


def record_pipeline_success(file_name: str, parsed_json: dict) -> None:
    logger.info("STEP 4: Storing successful extraction in document_pipeline...")
    get_supabase().table("document_pipeline").insert(
        {
            "file_name": file_name,
            "extracted_payload": parsed_json,
            "status": PIPELINE_STATUS_PENDING_VERIFICATION,
            "storage_bucket": VERIFICATION_STORAGE_BUCKET,
            "error_message": None,
        }
    ).execute()


def record_pipeline_failure(file_name: str, error_log: str) -> None:
    logger.info("STEP 4: Marking document_pipeline row as FAILED...")
    get_supabase().table("document_pipeline").insert(
        {
            "file_name": file_name,
            "status": PIPELINE_STATUS_FAILED,
            "error_message": error_log,
        }
    ).execute()


def remove_processed_file(file_name: str) -> None:
    get_supabase().storage.from_("incoming_forms").remove([file_name])


async def process_document(file_name: str) -> None:
    correlation_id = derive_correlation_id(file_name)
    set_correlation_id(correlation_id)

    local_temp_path: str | None = None
    pipeline_succeeded = False

    logger.info("[NEW CAPTURE] Processing document: %s", file_name)

    try:
        local_temp_path = create_ingestion_temp_file(file_name)
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
        logger.info(
            "[SUCCESS] Pipeline complete for %s. Logged to database.",
            file_name,
        )

    except Exception as exc:
        error_log = traceback.format_exc()
        logger.error(
            "[PIPELINE FAILURE] %s could not be processed: %s",
            file_name,
            exc,
        )
        logger.info(
            "The original file remains in incoming_forms for manual Scan Operator review."
        )

        try:
            await asyncio.to_thread(record_pipeline_failure, file_name, error_log)
            logger.info(
                "[STATE] document_pipeline status set to FAILED for %s.",
                file_name,
            )
        except Exception as state_error:
            logger.error(
                "[STATE ERROR] Could not record FAILED status for %s: %s",
                file_name,
                state_error,
            )

    finally:
        safe_remove(local_temp_path)

        if pipeline_succeeded:
            try:
                await asyncio.to_thread(remove_processed_file, file_name)
            except Exception as remove_error:
                logger.warning(
                    "Processed file but could not remove it from storage: %s",
                    remove_error,
                )

        reset_correlation_id()


async def poll_and_process_one() -> None:
    try:
        files = await asyncio.to_thread(list_incoming_form_files)
    except Exception as exc:
        logger.error("Connection error to Supabase: %s", exc)
        return

    uploads = [file_entry for file_entry in files if is_processable_upload(file_entry)]
    if not uploads:
        return

    queued_count = len(uploads)
    next_file = uploads[0]["name"]
    logger.info(
        "[SCAN] %s document(s) queued; processing one: %s",
        queued_count,
        next_file,
    )
    await process_document(next_file)


async def run_watcher() -> None:
    poll_interval = int(os.getenv("WATCHER_POLL_INTERVAL", "3"))
    while True:
        await poll_and_process_one()
        await asyncio.sleep(poll_interval)


def main() -> None:
    configure_structured_logging()
    logger.info("=== Secure On-Premises Docling + Qwen Vision Pipeline Online ===")
    logger.info(
        "Run this script with the project venv activated. "
        "Ollama itself is a separate app and does not use the venv."
    )
    validate_supabase_access()
    validate_ollama_access()
    asyncio.run(run_watcher())


if __name__ == "__main__":
    main()
