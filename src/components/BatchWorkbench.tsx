"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { IncomingDropzone } from "@/components/IncomingDropzone";
import { QueueClearState } from "@/components/QueueClearState";
import {
  fetchPendingBatches,
  type HitlBatchPriority,
  type HitlPendingBatch,
} from "@/lib/api";

const POLL_MS = 2500;

type PriorityFilter = "All" | HitlBatchPriority;
type SortOrder = "oldest" | "newest";

const PRIORITY_ORDER: Record<string, number> = {
  High: 3,
  Medium: 2,
  Low: 1,
};

export type BatchWorkbenchProps = {
  /** Open the split-screen VerificationDashboard for a queue item. */
  onSelectDocument: (id: string) => void;
};

/**
 * HITL queue screen backed by `GET /api/batches/pending`.
 * Review opens the first unverified document in batch sequence.
 */
export function BatchWorkbench({ onSelectDocument }: BatchWorkbenchProps) {
  const [batches, setBatches] = useState<HitlPendingBatch[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [priorityFilter, setPriorityFilter] = useState<PriorityFilter>("All");
  const [sortOrder, setSortOrder] = useState<SortOrder>("oldest");

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const rows = await fetchPendingBatches(signal);
      if (signal?.aborted) {
        return;
      }
      setBatches(rows);
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

  const visibleBatches = useMemo(() => {
    const filtered =
      priorityFilter === "All"
        ? batches
        : batches.filter((batch) => batch.priority === priorityFilter);

    const sorted = [...filtered].sort((a, b) => {
      const aTime = Date.parse(a.created_at) || 0;
      const bTime = Date.parse(b.created_at) || 0;
      const byTime = sortOrder === "oldest" ? aTime - bTime : bTime - aTime;
      if (byTime !== 0) {
        return byTime;
      }
      const aPriority = PRIORITY_ORDER[a.priority] ?? 0;
      const bPriority = PRIORITY_ORDER[b.priority] ?? 0;
      return bPriority - aPriority;
    });
    return sorted;
  }, [batches, priorityFilter, sortOrder]);

  const handleReview = (batch: HitlPendingBatch) => {
    const firstId = batch.first_pending_document_id;
    if (!firstId) {
      return;
    }
    onSelectDocument(firstId);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border bg-surface px-6 py-3">
        <div>
          <h1 className="text-base font-semibold text-foreground">
            Batch Workbench
          </h1>
          <p className="text-xs text-muted">
            Verification queue · review batches page-by-page in sequence
          </p>
        </div>
        <div className="rounded-md border border-border bg-surface-elevated px-3 py-1.5 font-mono text-[11px] text-muted">
          {visibleBatches.length} batch
          {visibleBatches.length === 1 ? "" : "es"} pending
        </div>
      </header>

      <IncomingDropzone onUploaded={() => void refresh()} />

      <div className="min-h-0 flex-1 overflow-y-auto">
        {loading && batches.length === 0 ? (
          <div className="flex h-full items-center justify-center">
            <div className="flex flex-col items-center gap-3 text-muted">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
              <p className="text-sm">Loading verification queue…</p>
            </div>
          </div>
        ) : error && batches.length === 0 ? (
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
        ) : batches.length === 0 ? (
          <QueueClearState />
        ) : (
          <div className="mx-auto w-full max-w-4xl px-6 py-6">
            <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
              <div>
                <h2 className="text-xs font-semibold uppercase tracking-[0.14em] text-foreground">
                  Pending Batches
                </h2>
                {error && (
                  <p className="mt-1 text-[11px] text-danger">{error}</p>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <label className="flex items-center gap-1.5 text-[11px] text-muted">
                  Priority
                  <select
                    value={priorityFilter}
                    onChange={(event) =>
                      setPriorityFilter(event.target.value as PriorityFilter)
                    }
                    className="rounded-md border border-border bg-surface-elevated px-2 py-1.5 text-xs text-foreground outline-none focus:border-accent"
                  >
                    <option value="All">All</option>
                    <option value="High">High</option>
                    <option value="Medium">Medium</option>
                    <option value="Low">Low</option>
                  </select>
                </label>
                <label className="flex items-center gap-1.5 text-[11px] text-muted">
                  Sort
                  <select
                    value={sortOrder}
                    onChange={(event) =>
                      setSortOrder(event.target.value as SortOrder)
                    }
                    className="rounded-md border border-border bg-surface-elevated px-2 py-1.5 text-xs text-foreground outline-none focus:border-accent"
                  >
                    <option value="oldest">Oldest first</option>
                    <option value="newest">Newest first</option>
                  </select>
                </label>
              </div>
            </div>

            {visibleBatches.length === 0 ? (
              <div className="rounded-xl border border-border bg-surface px-4 py-8 text-center text-sm text-muted">
                No batches match the current filters.
              </div>
            ) : (
              <ul className="overflow-hidden rounded-xl border border-border bg-surface">
                {visibleBatches.map((batch) => {
                  const isHigh = batch.priority === "High";
                  const display =
                    batch.batch_display ||
                    batch.batch_id.replace(/-/g, "").slice(0, 6).toUpperCase();
                  return (
                    <li
                      key={batch.batch_id}
                      className={`border-b border-border last:border-b-0 ${
                        isHigh
                          ? "border-l-4 border-l-amber-500 bg-amber-500/[0.04]"
                          : "border-l-4 border-l-transparent"
                      }`}
                    >
                      <div className="flex w-full items-center gap-4 px-4 py-3.5">
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <p className="truncate font-mono text-sm text-foreground">
                              Batch #{display}
                            </p>
                            <span
                              className={`rounded-md border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${
                                isHigh
                                  ? "border-amber-500/50 bg-amber-500/15 text-amber-400"
                                  : batch.priority === "Low"
                                    ? "border-border bg-background text-muted"
                                    : "border-accent/40 bg-accent/10 text-accent"
                              }`}
                            >
                              {batch.priority}
                            </span>
                          </div>
                          <p className="mt-0.5 truncate text-[11px] text-muted">
                            {batch.name}
                            {" · "}
                            {batch.pending_documents} of {batch.total_documents}{" "}
                            pending
                            {" · "}
                            {new Date(batch.created_at).toLocaleString()}
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={() => handleReview(batch)}
                          disabled={!batch.first_pending_document_id}
                          className="shrink-0 rounded-md border border-accent/40 bg-accent/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-accent transition hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          Review
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
