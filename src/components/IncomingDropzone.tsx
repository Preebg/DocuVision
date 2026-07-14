"use client";

import { useCallback, useRef, useState } from "react";
import { ingestDocument } from "@/lib/api";

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
        for (const file of files) {
          await ingestDocument(file);
        }

        setMessage(
          files.length === 1
            ? `Imported ${files[0].name} — worker will start scanning shortly.`
            : `Imported ${files.length} files — worker will process them in order.`,
        );
        onUploaded?.();
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Upload to local intake failed.",
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
          {uploading ? "Writing to local intake…" : "Drop a document to ingest"}
        </p>
        <p className="mt-1 text-xs text-muted">
          PDF or image · lands in the local incoming folder · processed by the worker
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
