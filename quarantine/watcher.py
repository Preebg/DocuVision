"""DocuVision secure document ingestion watcher (pipeline runner).

Processes incoming_forms **one document at a time per device**. When text
extraction finishes, the row moves to PENDING_VERIFICATION for the HITL
dashboard and the watcher immediately claims the next FIFO intake file.
"""

from __future__ import annotations

import asyncio
import os
import socket
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    INCOMING_STORAGE_BUCKET,
    PIPELINE_STATUS_FAILED,
    PIPELINE_STATUS_IMPORTED,
    PIPELINE_STATUS_PENDING_VERIFICATION,
    PIPELINE_STATUS_SCANNING,
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

ACTIVE_INTAKE_STATUSES = (PIPELINE_STATUS_IMPORTED, PIPELINE_STATUS_SCANNING)
TERMINAL_OR_READY_STATUSES = (
    PIPELINE_STATUS_PENDING_VERIFICATION,
    PIPELINE_STATUS_FAILED,
    "VERIFIED",
    "COMPLETED",
)

# How long a SCANNING claim may linger before another (or the same) device reclaims it.
STALE_SCANNING_MINUTES = int(os.getenv("WATCHER_STALE_SCANNING_MINUTES", "45"))


def get_device_id() -> str:
    """Stable id for the machine running this watcher (GPU / Ollama host)."""
    configured = os.getenv("DOCUVISION_DEVICE_ID", "").strip()
    if configured:
        return configured
    return socket.gethostname() or "unknown-device"


def list_incoming_form_files():
    return get_supabase().storage.from_(INCOMING_STORAGE_BUCKET).list()


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


def _storage_created_at(file_entry: dict) -> str:
    """Best-effort sort key so multi-file intake is processed FIFO."""
    metadata = file_entry.get("metadata") or {}
    return (
        file_entry.get("created_at")
        or metadata.get("lastModified")
        or metadata.get("updated_at")
        or file_entry.get("name")
        or ""
    )


def validate_supabase_access():
    url = get_supabase_url()
    key_role = get_supabase_key_role_cached()
    device_id = get_device_id()
    logger.info("Connected to Supabase project: %s", url)
    logger.info("Using Supabase key role: %s", key_role)
    logger.info("Watcher device id: %s (one document at a time on this device)", device_id)

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
        response = (
            get_supabase().storage.from_(INCOMING_STORAGE_BUCKET).download(file_name)
        )
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


def _is_stale_scanning_claim(row: dict) -> bool:
    started_raw = row.get("processing_started_at")
    if not started_raw:
        return True
    try:
        started = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - started
    return age > timedelta(minutes=STALE_SCANNING_MINUTES)


def reclaim_stale_scanning_jobs() -> int:
    """Return stuck SCANNING rows to IMPORTED so FIFO intake can resume."""
    client = get_supabase()
    scanning = (
        client.table("document_pipeline")
        .select("id,file_name,processing_device,processing_started_at")
        .eq("status", PIPELINE_STATUS_SCANNING)
        .limit(50)
        .execute()
    )
    reclaimed = 0
    for row in scanning.data or []:
        if not _is_stale_scanning_claim(row):
            continue
        client.table("document_pipeline").update(
            {
                "status": PIPELINE_STATUS_IMPORTED,
                "processing_device": None,
                "processing_started_at": None,
                "error_message": "Reclaimed stale SCANNING claim",
            }
        ).eq("id", row["id"]).eq("status", PIPELINE_STATUS_SCANNING).execute()
        logger.warning(
            "Reclaimed stale SCANNING job id=%s (%s) from device=%s",
            row["id"],
            row.get("file_name"),
            row.get("processing_device"),
        )
        reclaimed += 1
    return reclaimed


def ensure_imported_job(file_name: str) -> int:
    """Reuse a UI-created IMPORTED/SCANNING row, or create one for watcher-only ingress."""
    existing = (
        get_supabase()
        .table("document_pipeline")
        .select("id,status")
        .eq("file_name", file_name)
        .in_("status", list(ACTIVE_INTAKE_STATUSES))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if existing.data:
        job_id = int(existing.data[0]["id"])
        logger.info("Reusing pipeline job id=%s for %s", job_id, file_name)
        return job_id

    # Do not recreate if this file already completed extraction / HITL.
    already_ready = (
        get_supabase()
        .table("document_pipeline")
        .select("id,status")
        .eq("file_name", file_name)
        .in_("status", list(TERMINAL_OR_READY_STATUSES))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if already_ready.data:
        raise RuntimeError(
            f"{file_name} already has status={already_ready.data[0]['status']}; "
            "skipping re-intake"
        )

    logger.info("Recording QUEUED status for %s", file_name)
    created = (
        get_supabase()
        .table("document_pipeline")
        .insert(
            {
                "file_name": file_name,
                "status": PIPELINE_STATUS_IMPORTED,
                "storage_bucket": INCOMING_STORAGE_BUCKET,
                "error_message": None,
            }
        )
        .execute()
    )
    return int(created.data[0]["id"])


def update_pipeline_status(job_id: int, status: str, **fields) -> None:
    payload = {"status": status, **fields}
    get_supabase().table("document_pipeline").update(payload).eq("id", job_id).execute()


def mark_pipeline_scanning(job_id: int) -> None:
    device_id = get_device_id()
    logger.info(
        "Pipeline job id=%s -> SCANNING (device=%s)",
        job_id,
        device_id,
    )
    update_pipeline_status(
        job_id,
        PIPELINE_STATUS_SCANNING,
        processing_device=device_id,
        processing_started_at=datetime.now(timezone.utc).isoformat(),
        error_message=None,
    )


def record_pipeline_success(job_id: int, file_name: str, parsed_json: dict) -> None:
    logger.info(
        "STEP 4: Storing successful extraction for job id=%s (%s) → HITL queue...",
        job_id,
        file_name,
    )
    update_pipeline_status(
        job_id,
        PIPELINE_STATUS_PENDING_VERIFICATION,
        extracted_payload=parsed_json,
        storage_bucket=VERIFICATION_STORAGE_BUCKET,
        error_message=None,
        processing_device=None,
        processing_started_at=None,
    )


def record_pipeline_failure(job_id: int | None, file_name: str, error_log: str) -> None:
    logger.info("STEP 4: Marking document_pipeline row as FAILED for %s...", file_name)
    if job_id is not None:
        update_pipeline_status(
            job_id,
            PIPELINE_STATUS_FAILED,
            error_message=error_log,
            processing_device=None,
            processing_started_at=None,
        )
        return

    get_supabase().table("document_pipeline").insert(
        {
            "file_name": file_name,
            "status": PIPELINE_STATUS_FAILED,
            "error_message": error_log,
        }
    ).execute()


def remove_processed_file(file_name: str) -> None:
    get_supabase().storage.from_(INCOMING_STORAGE_BUCKET).remove([file_name])


def select_next_intake_file() -> str | None:
    """Pick the next FIFO file for this device. Never starts a second concurrent scan."""
    reclaim_stale_scanning_jobs()
    device_id = get_device_id()
    client = get_supabase()

    # Resume our own in-flight claim first (crash recovery mid-download is ok).
    ours = (
        client.table("document_pipeline")
        .select("id,file_name")
        .eq("status", PIPELINE_STATUS_SCANNING)
        .eq("processing_device", device_id)
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )
    if ours.data and ours.data[0].get("file_name"):
        file_name = ours.data[0]["file_name"]
        logger.info(
            "Resuming in-flight SCANNING job id=%s (%s) on device=%s",
            ours.data[0]["id"],
            file_name,
            device_id,
        )
        return file_name

    # Another device is actively scanning — do not pile on this GPU/host.
    other = (
        client.table("document_pipeline")
        .select("id,file_name,processing_device")
        .eq("status", PIPELINE_STATUS_SCANNING)
        .neq("processing_device", device_id)
        .limit(1)
        .execute()
    )
    if other.data:
        logger.info(
            "Device %s idle: job id=%s held by %s",
            device_id,
            other.data[0]["id"],
            other.data[0].get("processing_device"),
        )
        return None

    files = list_incoming_form_files()
    uploads = [entry for entry in files if is_processable_upload(entry)]
    if not uploads:
        return None

    uploads.sort(key=_storage_created_at)
    names_in_bucket = {entry["name"] for entry in uploads}

    # Prefer oldest IMPORTED pipeline row whose object is still in the bucket.
    imported = (
        client.table("document_pipeline")
        .select("id,file_name,created_at")
        .eq("status", PIPELINE_STATUS_IMPORTED)
        .order("created_at", desc=False)
        .limit(50)
        .execute()
    )
    for row in imported.data or []:
        name = row.get("file_name")
        if name and name in names_in_bucket:
            logger.info(
                "[SCAN] Claiming FIFO IMPORTED job id=%s (%s) on device=%s",
                row["id"],
                name,
                device_id,
            )
            return name

    # Watcher-only ingress: object in bucket with no IMPORTED row yet.
    for entry in uploads:
        name = entry["name"]
        active = (
            client.table("document_pipeline")
            .select("id,status")
            .eq("file_name", name)
            .in_("status", list(ACTIVE_INTAKE_STATUSES + TERMINAL_OR_READY_STATUSES))
            .limit(1)
            .execute()
        )
        if active.data:
            status = active.data[0]["status"]
            if status in TERMINAL_OR_READY_STATUSES:
                continue
            if status == PIPELINE_STATUS_SCANNING:
                continue
        logger.info(
            "[SCAN] %s document(s) queued; processing one: %s (device=%s)",
            len(uploads),
            name,
            device_id,
        )
        return name

    return None


async def process_document(file_name: str) -> None:
    correlation_id = derive_correlation_id(file_name)
    set_correlation_id(correlation_id)

    local_temp_path: str | None = None
    pipeline_succeeded = False
    job_id: int | None = None

    logger.info(
        "[NEW CAPTURE] Processing document: %s on device=%s",
        file_name,
        get_device_id(),
    )

    try:
        job_id = await asyncio.to_thread(ensure_imported_job, file_name)
        await asyncio.to_thread(mark_pipeline_scanning, job_id)

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
        # HITL handoff happens here — next poll may start scanning another file
        # while operators approve/reject this one.
        await asyncio.to_thread(
            record_pipeline_success, job_id, file_name, parsed_json
        )

        pipeline_succeeded = True
        logger.info(
            "[SUCCESS] Extraction complete for %s → PENDING_VERIFICATION (HITL). "
            "Device %s is free for the next document.",
            file_name,
            get_device_id(),
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
            await asyncio.to_thread(
                record_pipeline_failure, job_id, file_name, error_log
            )
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


async def poll_and_process_one() -> bool:
    """Process exactly one queued intake file on this device. Returns True if work ran."""
    try:
        next_file = await asyncio.to_thread(select_next_intake_file)
    except Exception as exc:
        logger.error("Connection error to Supabase: %s", exc)
        return False

    if not next_file:
        return False

    await process_document(next_file)
    return True


async def run_watcher() -> None:
    poll_interval = int(os.getenv("WATCHER_POLL_INTERVAL", "1"))
    while True:
        had_work = await poll_and_process_one()
        if not had_work:
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
