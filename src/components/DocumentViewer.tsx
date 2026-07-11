"use client";

import { isPdfFileName } from "@/lib/types";

interface DocumentViewerProps {
  fileName: string | null;
  documentUrl: string | null;
  loading: boolean;
  error: string | null;
}

export function DocumentViewer({
  fileName,
  documentUrl,
  loading,
  error,
}: DocumentViewerProps) {
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex flex-col items-center gap-3 text-muted">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
          <p className="text-sm">Loading document preview…</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="max-w-md rounded-xl border border-danger/30 bg-danger/5 p-6 text-center">
          <p className="text-sm font-medium text-danger">Preview unavailable</p>
          <p className="mt-2 text-xs text-muted">{error}</p>
        </div>
      </div>
    );
  }

  if (!documentUrl || !fileName) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-sm text-muted">
        No document attached to this queue item.
      </div>
    );
  }

  const isPdf = isPdfFileName(fileName);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <p className="truncate font-mono text-xs text-muted">{fileName}</p>
        <span className="rounded-md bg-surface-elevated px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted">
          {isPdf ? "PDF" : "Image"}
        </span>
      </div>
      <div className="relative min-h-0 flex-1 overflow-hidden bg-[#08090c]">
        {isPdf ? (
          <object
            data={documentUrl}
            type="application/pdf"
            className="h-full w-full"
            aria-label={`PDF preview of ${fileName}`}
          >
            <iframe
              src={documentUrl}
              title={fileName}
              className="h-full w-full border-0"
            />
          </object>
        ) : (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={documentUrl}
            alt={`Document preview: ${fileName}`}
            className="h-full w-full object-contain"
          />
        )}
      </div>
    </div>
  );
}
