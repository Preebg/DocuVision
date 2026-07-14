"use client";

import { useCallback, useRef, useState } from "react";
import { getSupabaseClient } from "@/lib/supabase/client";
import { INCOMING_STORAGE_BUCKET } from "@/lib/types";

const ACCEPTED_EXTENSIONS = [
  ".pdf",
  ".jpg",
  ".jpeg",
  ".png",
  ".webp",
  ".tif",
  ".tiff",
];

type IncomingDropzoneProps = {
  onUploaded?: () => void;
};

function isAcceptedFile(file: File): boolean {
  const lower = file.name.toLowerCase();
  return (
    ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext)) ||
    file.type.startsWith("image/") ||
    file.type === "application/pdf"
  );
}

function sanitizeFileName(name: string): string {
  return name.replace(/[^a-zA-Z0-9._-]/g, "_");
}

export function IncomingDropzone({ onUploaded }: IncomingDropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const uploadFiles = useCallback(
    async (fileList: FileList | File[]) => {
      const files = Array.from(fileList).filter(isAcceptedFile);
      if (!files.length) {
        setError("Use a PDF or image file (.pdf, .jpg, .png, .webp, .tif).");
        return;
      }

      setUploading(true);
      setError(null);
      setMessage(null);

      try {
        const supabase = getSupabaseClient();

        for (const file of files) {
          const objectName = `${Date.now()}_${sanitizeFileName(file.name)}`;
          const { error: uploadError } = await supabase.storage
            .from(INCOMING_STORAGE_BUCKET)
            .upload(objectName, file, {
              contentType: file.type || "application/octet-stream",
              upsert: false,
            });

          if (uploadError) {
            throw new Error(uploadError.message);
          }

          const { error: insertError } = await supabase
            .from("document_pipeline")
            .insert({
              file_name: objectName,
              status: "IMPORTED",
              storage_bucket: INCOMING_STORAGE_BUCKET,
            });

          if (insertError) {
            throw new Error(insertError.message);
          }
        }

        setMessage(
          files.length === 1
            ? `Imported ${files[0].name} — watcher will start scanning shortly.`
            : `Imported ${files.length} files — watcher will process them in order.`,
        );
        onUploaded?.();
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Upload to incoming_forms failed.",
        );
      } finally {
        setUploading(false);
      }
    },
    [onUploaded],
  );

  return (
    <div className="border-b border-border bg-surface px-6 py-4">
      <div
        role="button"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(event) => {
          event.preventDefault();
          setDragging(false);
        }}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (event.dataTransfer.files?.length) {
            void uploadFiles(event.dataTransfer.files);
          }
        }}
        onClick={() => inputRef.current?.click()}
        className={`flex cursor-pointer flex-col items-center justify-center rounded-xl border border-dashed px-4 py-6 text-center transition ${
          dragging
            ? "border-accent bg-accent/10"
            : "border-border bg-background hover:border-accent/40 hover:bg-surface-elevated"
        } ${uploading ? "pointer-events-none opacity-70" : ""}`}
      >
        <p className="text-sm font-medium text-foreground">
          {uploading ? "Uploading to incoming_forms…" : "Drop a document to ingest"}
        </p>
        <p className="mt-1 text-xs text-muted">
          PDF or image · lands in the Supabase intake bucket · auto-processed
        </p>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED_EXTENSIONS.join(",")}
          multiple
          className="hidden"
          onChange={(event) => {
            if (event.target.files?.length) {
              void uploadFiles(event.target.files);
              event.target.value = "";
            }
          }}
        />
      </div>

      {message && (
        <p className="mt-2 text-xs text-success" role="status">
          {message}
        </p>
      )}
      {error && (
        <p className="mt-2 text-xs text-danger" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
