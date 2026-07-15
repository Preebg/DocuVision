"use client";

import { useCallback, useEffect, useState } from "react";
import { IncomingDropzone } from "@/components/IncomingDropzone";
import { QueueClearState } from "@/components/QueueClearState";
import {
  fetchHitlDocuments,
  type HitlDocumentListItem,
} from "@/lib/api";

const POLL_MS = 2500;

export type BatchWorkbenchProps = {
  /** Open the split-screen VerificationDashboard for a queue item. */
  onSelectDocument: (id: string) => void;
};

/**
 * HITL queue screen backed by `GET /api/documents?status=VERIFYING`.
 * Selecting a row routes into VerificationDashboard.
 */
export function BatchWorkbench({ onSelectDocument }: BatchWorkbenchProps) {
  const [items, setItems] = useState<HitlDocumentListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const rows = await fetchHitlDocuments("VERIFYING", signal);
      if (signal?.aborted) {
        return;
      }
      setItems(rows);
      setError(null);
    } catch (err) {
      if (signal?.aborted) {
        return;
      }
      setError(
        err instanceof Error ? err.message : "Failed to load verification queue.",
      );
    } finally {
      if (!signal?.aborted) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    const poll = window.setInterval(() => {
      void refresh();
    }, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(poll);
    };
  }, [refresh]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border bg-surface px-6 py-3">
        <div>
          <h1 className="text-base font-semibold text-foreground">
            Batch Workbench
          </h1>
          <p className="text-xs text-muted">
            Verification queue · select a document to review &amp; correct
          </p>
        </div>
        <div className="rounded-md border border-border bg-surface-elevated px-3 py-1.5 font-mono text-[11px] text-muted">
          {items.length} pending
        </div>
      </header>

      <IncomingDropzone onUploaded={() => void refresh()} />

      <div className="min-h-0 flex-1 overflow-y-auto">
        {loading && items.length === 0 ? (
          <div className="flex h-full items-center justify-center">
            <div className="flex flex-col items-center gap-3 text-muted">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
              <p className="text-sm">Loading verification queue…</p>
            </div>
          </div>
        ) : error && items.length === 0 ? (
          <div className="flex h-full items-center justify-center p-8">
            <div className="max-w-md rounded-xl border border-danger/30 bg-danger/5 p-6 text-center">
              <p className="text-sm font-medium text-danger">Queue unavailable</p>
              <p className="mt-2 text-xs text-muted">{error}</p>
              <button
                type="button"
                onClick={() => void refresh()}
                className="mt-4 rounded-lg border border-border px-3 py-1.5 text-xs text-muted transition hover:bg-surface-elevated hover:text-foreground"
              >
                Retry
              </button>
            </div>
          </div>
        ) : items.length === 0 ? (
          <QueueClearState />
        ) : (
          <div className="mx-auto w-full max-w-4xl px-6 py-6">
            <div className="mb-3 flex items-baseline justify-between gap-3">
              <h2 className="text-xs font-semibold uppercase tracking-[0.14em] text-foreground">
                Pending Verification
              </h2>
              {error && (
                <p className="text-[11px] text-danger">{error}</p>
              )}
            </div>
            <ul className="overflow-hidden rounded-xl border border-border bg-surface">
              {items.map((item) => (
                <li key={item.id} className="border-b border-border last:border-b-0">
                  <button
                    type="button"
                    onClick={() => onSelectDocument(item.id)}
                    className="flex w-full items-center gap-4 px-4 py-3.5 text-left transition hover:bg-surface-elevated"
                  >
                    <div className="min-w-0 flex-1">
                      <p className="truncate font-mono text-sm text-foreground">
                        {item.filename}
                      </p>
                      <p className="mt-0.5 text-[11px] text-muted">
                        {new Date(item.created_at).toLocaleString()}
                        {item.file_path ? ` · ${item.file_path}` : ""}
                      </p>
                    </div>
                    <span className="shrink-0 rounded-md border border-accent/40 bg-accent/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-accent">
                      Review
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
