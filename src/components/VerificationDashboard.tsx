"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { PdfJsViewer, type PdfFieldHighlight } from "@/components/PdfJsViewer";
import { useOperatorHotkeys } from "@/hooks/useOperatorHotkeys";
import {
  fetchDocumentObjectUrl,
  fetchHitlDocument,
  verifyHitlDocument,
  type HitlDocumentDetail,
} from "@/lib/api";
import {
  buildCorrectedData,
  editableFieldKeys,
  getEditableFieldValue,
  humanizeFieldKey,
} from "@/lib/hitl-payload";
import {
  getFieldBBox,
  getFieldConfidence,
  normalizeExtractedPayload,
  type ExtractedPayload,
} from "@/lib/types";

export type VerificationDashboardProps = {
  /** Document id from the BatchWorkbench selection. */
  documentId: string;
  /** Flip to another document in the same batch (sequence prev/next). */
  onNavigateDocument?: (id: string) => void;
  /** Called after a successful Approve & Release so the app can return to the queue. */
  onReleased?: () => void;
  /** Optional explicit back navigation to BatchWorkbench. */
  onBackToQueue?: () => void;
};

export function VerificationDashboard({
  documentId,
  onNavigateDocument,
  onReleased,
  onBackToQueue,
}: VerificationDashboardProps) {
  const [document, setDocument] = useState<HitlDocumentDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [formValues, setFormValues] = useState<Record<string, string>>({});
  const [activeFieldKey, setActiveFieldKey] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [successFlash, setSuccessFlash] = useState(false);

  const fieldRefs = useRef<Map<string, HTMLInputElement>>(new Map());
  const savingRef = useRef(false);

  const extractedSource = useMemo(() => {
    if (!document) {
      return null;
    }
    // Prefer projected extracted_json; fall back to metadata.extracted_payload.
    if (document.extracted_json != null) {
      return document.extracted_json;
    }
    return document.metadata?.extracted_payload ?? null;
  }, [document]);

  const extractedPayload = useMemo(
    () =>
      normalizeExtractedPayload(
        (extractedSource as ExtractedPayload | null | undefined) ?? null,
      ),
    [extractedSource],
  );

  const fieldKeys = useMemo(
    () => editableFieldKeys(extractedSource),
    [extractedSource],
  );

  const highlights = useMemo((): PdfFieldHighlight[] => {
    const items: PdfFieldHighlight[] = [];
    for (const key of fieldKeys) {
      const bbox = getFieldBBox(extractedPayload, key);
      if (!bbox) {
        continue;
      }
      items.push({
        key,
        bbox,
        confidence: getFieldConfidence(extractedPayload, key),
      });
    }
    return items;
  }, [extractedPayload, fieldKeys]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    setActionError(null);
    setDocument(null);
    setFormValues({});

    void (async () => {
      try {
        const detail = await fetchHitlDocument(documentId, controller.signal);
        if (controller.signal.aborted) {
          return;
        }
        setDocument(detail);
        const source =
          detail.extracted_json ??
          detail.metadata?.extracted_payload ??
          null;
        const keys = editableFieldKeys(source);
        const next: Record<string, string> = {};
        for (const key of keys) {
          next[key] = getEditableFieldValue(source, key);
        }
        setFormValues(next);
        setActiveFieldKey(keys[0] ?? null);
      } catch (err) {
        if (controller.signal.aborted) {
          return;
        }
        setLoadError(
          err instanceof Error ? err.message : "Failed to load document.",
        );
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    })();

    return () => controller.abort();
  }, [documentId]);

  const handleFieldChange = (key: string, value: string) => {
    setFormValues((current) => ({ ...current, [key]: value }));
  };

  const handleApproveAndRelease = useCallback(async () => {
    if (!document || savingRef.current) {
      return;
    }

    savingRef.current = true;
    setSaving(true);
    setActionError(null);

    try {
      const corrected = buildCorrectedData(extractedSource, formValues);
      await verifyHitlDocument(document.id, corrected);
      const nextPendingId = document.next_pending_document_id ?? null;
      setSuccessFlash(true);
      window.setTimeout(() => {
        savingRef.current = false;
        setSaving(false);
        setSuccessFlash(false);
        if (nextPendingId && onNavigateDocument) {
          onNavigateDocument(nextPendingId);
          return;
        }
        // Last pending document in the batch (or standalone) → workbench.
        onReleased?.();
        onBackToQueue?.();
      }, 350);
    } catch (err) {
      setActionError(
        err instanceof Error
          ? err.message
          : "Failed to approve and release document.",
      );
      savingRef.current = false;
      setSaving(false);
    }
  }, [
    document,
    extractedSource,
    formValues,
    onBackToQueue,
    onNavigateDocument,
    onReleased,
  ]);

  const focusFieldAtIndex = useCallback(
    (index: number) => {
      const key = fieldKeys[index];
      if (!key) {
        return;
      }
      setActiveFieldKey(key);
      const node = fieldRefs.current.get(key);
      node?.focus();
      node?.select();
    },
    [fieldKeys],
  );

  useOperatorHotkeys({
    enabled: Boolean(document) && !saving && !loading,
    onApproveRelease: () => void handleApproveAndRelease(),
    onFocusField: focusFieldAtIndex,
    onEscape: onBackToQueue,
  });

  const [documentUrl, setDocumentUrl] = useState<string | null>(null);

  // Authenticated PDF/image bytes → blob URL (X-API-Key; no bare loopback URL).
  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;

    if (!document?.id) {
      queueMicrotask(() => {
        if (!cancelled) {
          setDocumentUrl(null);
        }
      });
      return () => {
        cancelled = true;
      };
    }

    void (async () => {
      try {
        objectUrl = await fetchDocumentObjectUrl(document.id);
        if (!cancelled) {
          setDocumentUrl(objectUrl);
        } else {
          URL.revokeObjectURL(objectUrl);
        }
      } catch {
        if (!cancelled) {
          setDocumentUrl(null);
        }
      }
    })();

    return () => {
      cancelled = true;
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [document?.id]);

  const fileName = document?.filename ?? null;

  const batchId = document?.batch_id ?? null;
  const batchDisplay =
    document?.batch_display ??
    (batchId ? batchId.replace(/-/g, "").slice(0, 6).toUpperCase() : null);
  const sequenceNo = document?.sequence_no ?? null;
  const batchCount = document?.batch_document_count ?? null;
  const inBatch =
    Boolean(batchId) &&
    typeof sequenceNo === "number" &&
    typeof batchCount === "number" &&
    batchCount > 0;
  const prevDocumentId = document?.prev_document_id ?? null;
  const nextDocumentId = document?.next_document_id ?? null;

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex flex-col items-center gap-3 text-muted">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
          <p className="text-sm">Loading document for verification…</p>
        </div>
      </div>
    );
  }

  if (loadError || !document) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-8">
        <div className="max-w-md rounded-xl border border-danger/30 bg-danger/5 p-6 text-center">
          <p className="text-sm font-medium text-danger">
            Unable to load document
          </p>
          <p className="mt-2 text-xs text-muted">
            {loadError ?? "Document not found."}
          </p>
        </div>
        {onBackToQueue && (
          <button
            type="button"
            onClick={onBackToQueue}
            className="rounded-lg border border-border px-4 py-2 text-sm text-muted transition hover:bg-surface-elevated hover:text-foreground"
          >
            Back to Batch Workbench
          </button>
        )}
      </div>
    );
  }

  return (
    <div
      className={`flex h-full min-h-0 flex-col transition-[box-shadow] duration-300 ${
        successFlash
          ? "shadow-[inset_0_0_0_3px_var(--success)]"
          : "shadow-[inset_0_0_0_0_transparent]"
      }`}
    >
      {inBatch && (
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border bg-surface-elevated px-5 py-2.5">
          <p className="min-w-0 truncate text-xs text-foreground">
            Document{" "}
            <span className="font-semibold tabular-nums">{sequenceNo}</span> of{" "}
            <span className="font-semibold tabular-nums">{batchCount}</span> in
            Batch #{batchDisplay}
            {document.batch_name ? (
              <span className="text-muted"> · {document.batch_name}</span>
            ) : null}
          </p>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={() =>
                prevDocumentId && onNavigateDocument?.(prevDocumentId)
              }
              disabled={saving || !prevDocumentId || !onNavigateDocument}
              className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted transition hover:bg-surface hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            >
              Back
            </button>
            <button
              type="button"
              onClick={() =>
                nextDocumentId && onNavigateDocument?.(nextDocumentId)
              }
              disabled={saving || !nextDocumentId || !onNavigateDocument}
              className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted transition hover:bg-surface hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>
      )}

      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-border bg-surface px-5 py-3">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent">
            Split-Screen Verification
          </p>
          <h1 className="mt-0.5 truncate text-base font-semibold text-foreground">
            {fileName}
          </h1>
          <p className="mt-0.5 font-mono text-[11px] text-muted">
            {document.id.slice(0, 8)} · {document.status} ·{" "}
            {new Date(document.created_at).toLocaleString()}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className="hidden text-[11px] text-muted sm:inline">
            Ctrl/⌘+Enter to release
          </span>
          {onBackToQueue && (
            <button
              type="button"
              onClick={onBackToQueue}
              disabled={saving}
              className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted transition hover:bg-surface-elevated hover:text-foreground disabled:opacity-50"
            >
              Queue
            </button>
          )}
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* Left: PDF / document viewer with field bbox overlays */}
        <section className="flex w-[58%] min-w-0 flex-col border-r border-border bg-surface">
          <PdfJsViewer
            fileName={fileName}
            documentUrl={documentUrl}
            loading={false}
            error={null}
            highlights={highlights}
            activeFieldKey={activeFieldKey}
          />
          {document.file_path && (
            <p
              className="truncate border-t border-border px-4 py-1.5 font-mono text-[10px] text-muted"
              title={document.file_path}
            >
              local_path → {document.file_path}
            </p>
          )}
        </section>

        {/* Right: dynamic correction form */}
        <section className="flex w-[42%] min-w-0 flex-col bg-surface-elevated">
          <header className="border-b border-border px-6 py-4">
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent">
              Data Correction
            </p>
            <h2 className="mt-1 text-lg font-semibold text-foreground">
              Review Extracted Fields
            </h2>
            <p className="mt-1 text-xs text-muted">
              Focus a field to highlight its source location on the PDF. Edit
              any value, then Approve &amp; Release.
            </p>
          </header>

          <div className="flex-1 overflow-y-auto px-6 py-5">
            {fieldKeys.length === 0 ? (
              <p className="text-sm text-muted">
                No editable key-value pairs found in{" "}
                <span className="font-mono">extracted_payload</span>.
              </p>
            ) : (
              <div className="space-y-4">
                {fieldKeys.map((key, index) => {
                  const confidence = getFieldConfidence(extractedPayload, key);
                  const hasBox = Boolean(getFieldBBox(extractedPayload, key));
                  return (
                    <label key={key} className="block">
                      <span className="mb-1.5 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted">
                        {index < 9 && (
                          <kbd className="rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10px] text-muted">
                            {index + 1}
                          </kbd>
                        )}
                        {humanizeFieldKey(key)}
                        {confidence != null && (
                          <span className="ml-auto rounded bg-background px-1.5 py-0.5 font-mono text-[10px] normal-case tracking-normal text-muted">
                            {Math.round(confidence * 100)}%
                          </span>
                        )}
                        {hasBox && (
                          <span
                            className="h-2 w-2 shrink-0 rounded-sm border border-blue-500 bg-yellow-300/80"
                            title="Has PDF highlight"
                            aria-hidden
                          />
                        )}
                      </span>
                      <input
                        ref={(node) => {
                          if (node) {
                            fieldRefs.current.set(key, node);
                          } else {
                            fieldRefs.current.delete(key);
                          }
                        }}
                        type="text"
                        value={formValues[key] ?? ""}
                        onChange={(event) =>
                          handleFieldChange(key, event.target.value)
                        }
                        onFocus={() => setActiveFieldKey(key)}
                        className={`w-full rounded-lg border bg-background px-3 py-2.5 text-sm text-foreground outline-none transition focus:ring-2 focus:ring-accent/20 ${
                          activeFieldKey === key
                            ? "border-accent"
                            : "border-border focus:border-accent"
                        }`}
                        placeholder={`Enter ${humanizeFieldKey(key).toLowerCase()}…`}
                      />
                    </label>
                  );
                })}
              </div>
            )}

            {actionError && (
              <div className="mt-4 rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
                {actionError}
              </div>
            )}
          </div>

          <footer className="flex gap-3 border-t border-border px-6 py-4">
            {onBackToQueue && (
              <button
                type="button"
                onClick={onBackToQueue}
                disabled={saving}
                className="rounded-lg border border-border px-4 py-2.5 text-sm font-medium text-muted transition hover:bg-surface disabled:cursor-not-allowed disabled:opacity-50"
              >
                Cancel
              </button>
            )}
            <button
              type="button"
              onClick={() => void handleApproveAndRelease()}
              disabled={saving}
              className="flex-1 rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              {saving ? "Releasing…" : "Approve & Release"}
            </button>
          </footer>
        </section>
      </div>
    </div>
  );
}
