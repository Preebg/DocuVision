"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { IncomingDropzone } from "@/components/IncomingDropzone";
import { PdfJsViewer, type PdfFieldHighlight } from "@/components/PdfJsViewer";
import { PipelinePhaseStepper } from "@/components/PipelinePhaseStepper";
import { QueueClearState } from "@/components/QueueClearState";
import { useOperatorHotkeys } from "@/hooks/useOperatorHotkeys";
import {
  documentFileUrl,
  fetchQueue,
  patchDocument,
} from "@/lib/api";
import {
  buildVerifiedPayload,
  CONFIDENCE_OK_THRESHOLD,
  DocumentPipelineRow,
  draftStorageKey,
  emptyVerificationFields,
  fieldKeysForPayload,
  getFieldBBoxes,
  getFieldConfidence,
  getFieldValue,
  getOperatorIdentity,
  INGESTION_STEP_LABELS,
  normalizeExtractedPayload,
  PipelineStatus,
  PipelineStatusType,
  REJECT_REASONS,
  type RejectReasonValue,
  VerificationFieldKey,
} from "@/lib/types";

const POLL_MS = 2500;
const SUCCESS_FLASH_MS = 700;

function stepLabel(status: PipelineStatusType | null | undefined): string {
  if (status && status in INGESTION_STEP_LABELS) {
    return INGESTION_STEP_LABELS[status] ?? status;
  }
  return status ?? "Processing";
}

export function VerificationDashboard() {
  const [activeIngestion, setActiveIngestion] = useState<DocumentPipelineRow[]>(
    [],
  );
  const [pendingVerification, setPendingVerification] = useState<
    DocumentPipelineRow[]
  >([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [queueLoading, setQueueLoading] = useState(true);
  const [documentUrl, setDocumentUrl] = useState<string | null>(null);
  const [documentLoading, setDocumentLoading] = useState(false);
  const [fields, setFields] = useState<Record<VerificationFieldKey, string>>(
    emptyVerificationFields,
  );
  const [activeFieldKey, setActiveFieldKey] =
    useState<VerificationFieldKey | null>(null);
  const [actionLoading, setActionLoading] = useState<"approve" | "reject" | null>(
    null,
  );
  const [actionError, setActionError] = useState<string | null>(null);
  const [draftSavedAt, setDraftSavedAt] = useState<number | null>(null);
  const [successFlash, setSuccessFlash] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] =
    useState<RejectReasonValue>("UNREADABLE_DOCUMENT");

  const pendingRef = useRef(pendingVerification);
  const fieldRefs = useRef<Map<VerificationFieldKey, HTMLInputElement>>(
    new Map(),
  );
  const reviewPaneRef = useRef<HTMLElement>(null);

  useEffect(() => {
    pendingRef.current = pendingVerification;
  }, [pendingVerification]);

  const selectedItem =
    pendingVerification.find((row) => row.id === selectedId) ??
    pendingVerification[0] ??
    null;

  const visibleFieldKeys = useMemo(
    () =>
      selectedItem
        ? fieldKeysForPayload(selectedItem.extracted_payload)
        : [],
    [selectedItem],
  );

  const highlights: PdfFieldHighlight[] = useMemo(() => {
    if (!selectedItem) {
      return [];
    }
    const boxes = getFieldBBoxes(selectedItem.extracted_payload);
    return Object.entries(boxes).map(([key, bbox]) => ({
      key,
      bbox: bbox!,
    }));
  }, [selectedItem]);

  const populateFields = useCallback((item: DocumentPipelineRow) => {
    const keys = fieldKeysForPayload(item.extracted_payload);
    const nextFields = emptyVerificationFields();
    for (const key of keys) {
      nextFields[key] = getFieldValue(item.extracted_payload, key);
    }

    // Restore local draft if the clerk previously hit Ctrl+S on this job.
    try {
      const raw = window.localStorage.getItem(draftStorageKey(item.id));
      if (raw) {
        const draft = JSON.parse(raw) as Partial<
          Record<VerificationFieldKey, string>
        >;
        for (const key of keys) {
          if (typeof draft[key] === "string") {
            nextFields[key] = draft[key]!;
          }
        }
      }
    } catch {
      // Ignore corrupt drafts.
    }

    setFields(nextFields);
    setActiveFieldKey(keys[0] ?? null);
    setDraftSavedAt(null);
  }, []);

  const refreshQueues = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const payload = await fetchQueue(signal);
        if (signal?.aborted) {
          return;
        }
        setActiveIngestion(payload.active_ingestion);
        setPendingVerification(payload.pending_verification);
        setActionError(null);

        setSelectedId((current) => {
          if (
            current &&
            payload.pending_verification.some((row) => row.id === current)
          ) {
            return current;
          }
          return payload.pending_verification[0]?.id ?? null;
        });
      } catch (err) {
        if (signal?.aborted) {
          return;
        }
        setActionError(
          err instanceof Error ? err.message : "Failed to load local queue.",
        );
      } finally {
        if (!signal?.aborted) {
          setQueueLoading(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    const controller = new AbortController();
    // Queue poll is intentional external sync for the HITL station.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch on mount + interval
    void refreshQueues(controller.signal);
    const poll = window.setInterval(() => {
      void refreshQueues();
    }, POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(poll);
    };
  }, [refreshQueues]);

  useEffect(() => {
    if (!selectedId) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- reset review pane when queue empties
      setDocumentUrl(null);
      setDocumentLoading(false);
      setFields(emptyVerificationFields());
      setActiveFieldKey(null);
      return;
    }

    const item = pendingRef.current.find((row) => row.id === selectedId);
    if (!item) {
      return;
    }

    populateFields(item);
    setDocumentLoading(false);
    setDocumentUrl(documentFileUrl(item.id));
  }, [selectedId, populateFields]);

  const handleFieldChange = (key: VerificationFieldKey, value: string) => {
    setFields((current) => ({ ...current, [key]: value }));
  };

  const focusFieldAtIndex = useCallback(
    (index: number) => {
      const key = visibleFieldKeys[index];
      if (!key) {
        return;
      }
      setActiveFieldKey(key);
      const node = fieldRefs.current.get(key);
      node?.focus();
      node?.select();
    },
    [visibleFieldKeys],
  );

  const navigateDocument = useCallback(
    (delta: number) => {
      const list = pendingRef.current;
      if (list.length === 0) {
        return;
      }
      const currentIndex = list.findIndex((row) => row.id === selectedId);
      const base = currentIndex >= 0 ? currentIndex : 0;
      const nextIndex = Math.max(0, Math.min(list.length - 1, base + delta));
      setSelectedId(list[nextIndex]!.id);
    },
    [selectedId],
  );

  const flashSuccessAndAdvance = useCallback(
    async (opts?: { flash?: boolean }) => {
      if (opts?.flash !== false) {
        setSuccessFlash(true);
        window.setTimeout(() => setSuccessFlash(false), SUCCESS_FLASH_MS);
      }
      const releasedId = selectedId;
      await refreshQueues();
      // Prefer the next pending item rather than re-selecting the released job.
      setSelectedId((current) => {
        const list = pendingRef.current.filter((row) => row.id !== releasedId);
        if (list.length === 0) {
          return null;
        }
        if (current && list.some((row) => row.id === current)) {
          return current;
        }
        return list[0]!.id;
      });
    },
    [refreshQueues, selectedId],
  );

  const handleApprove = useCallback(async () => {
    if (!selectedItem || actionLoading) {
      return;
    }

    setActionLoading("approve");
    setActionError(null);

    try {
      const operator = getOperatorIdentity();
      const verifiedPayload = buildVerifiedPayload(
        fields,
        selectedItem.extracted_payload,
      );
      await patchDocument(selectedItem.id, {
        action: "approve",
        verified_payload: verifiedPayload,
        verified_by: operator,
        actor: operator,
      });
      try {
        window.localStorage.removeItem(draftStorageKey(selectedItem.id));
      } catch {
        // ignore
      }
      await flashSuccessAndAdvance();
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to approve document.",
      );
    } finally {
      setActionLoading(null);
    }
  }, [actionLoading, fields, flashSuccessAndAdvance, selectedItem]);

  const handleRejectConfirm = useCallback(async () => {
    if (!selectedItem || actionLoading) {
      return;
    }

    setActionLoading("reject");
    setActionError(null);

    try {
      const operator = getOperatorIdentity();
      const verifiedPayload = buildVerifiedPayload(
        fields,
        selectedItem.extracted_payload,
      );
      await patchDocument(selectedItem.id, {
        action: "reject",
        reject_reason: rejectReason,
        verified_payload: verifiedPayload,
        verified_by: operator,
        actor: operator,
      });
      try {
        window.localStorage.removeItem(draftStorageKey(selectedItem.id));
      } catch {
        // ignore
      }
      setRejectOpen(false);
      await flashSuccessAndAdvance({ flash: false });
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to reject document.",
      );
    } finally {
      setActionLoading(null);
    }
  }, [
    actionLoading,
    fields,
    flashSuccessAndAdvance,
    rejectReason,
    selectedItem,
  ]);

  const handleSaveDraft = useCallback(() => {
    if (!selectedItem) {
      return;
    }
    try {
      const keys = fieldKeysForPayload(selectedItem.extracted_payload);
      const draft: Partial<Record<VerificationFieldKey, string>> = {};
      for (const key of keys) {
        draft[key] = fields[key] ?? "";
      }
      window.localStorage.setItem(
        draftStorageKey(selectedItem.id),
        JSON.stringify(draft),
      );
      setDraftSavedAt(Date.now());
      setActionError(null);
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to save local draft.",
      );
    }
  }, [fields, selectedItem]);

  const handleEscape = useCallback(() => {
    if (rejectOpen) {
      setRejectOpen(false);
      return;
    }
    const active = document.activeElement;
    if (active instanceof HTMLElement) {
      active.blur();
    }
    setActiveFieldKey(null);
  }, [rejectOpen]);

  const isReviewReady =
    selectedItem?.status === PipelineStatus.PENDING_VERIFICATION;

  useOperatorHotkeys({
    enabled: (isReviewReady && actionLoading === null) || rejectOpen,
    onApproveRelease: rejectOpen ? undefined : () => void handleApprove(),
    onReject: rejectOpen ? undefined : () => setRejectOpen(true),
    onNextDocument: rejectOpen ? undefined : () => navigateDocument(1),
    onPrevDocument: rejectOpen ? undefined : () => navigateDocument(-1),
    onFocusField: rejectOpen ? undefined : focusFieldAtIndex,
    onSaveDraft: rejectOpen ? undefined : handleSaveDraft,
    onEscape: handleEscape,
  });

  const stepperStatus =
    selectedItem?.status ?? activeIngestion[0]?.status ?? null;
  const stepperFileName =
    selectedItem?.file_name ?? activeIngestion[0]?.file_name ?? null;

  return (
    <div
      className={`flex h-full min-h-0 flex-col transition-[box-shadow] duration-300 ${
        successFlash
          ? "shadow-[inset_0_0_0_3px_var(--success)]"
          : "shadow-[inset_0_0_0_0_transparent]"
      }`}
    >
      <IncomingDropzone onUploaded={() => void refreshQueues()} />
      <PipelinePhaseStepper status={stepperStatus} fileName={stepperFileName} />

      <div className="grid shrink-0 grid-cols-1 gap-0 border-b border-border lg:grid-cols-2">
        <QueuePanel
          title="Active Ingestion Queue"
          subtitle="Machine pipeline in flight"
          emptyLabel="No documents scanning, extracting layout, or running vision."
          loading={queueLoading && activeIngestion.length === 0}
        >
          {activeIngestion.map((item) => (
            <div
              key={item.id}
              className="flex items-center gap-3 border-b border-border/60 px-4 py-3 last:border-b-0"
            >
              <span
                className="h-2.5 w-2.5 shrink-0 animate-pulse rounded-full bg-accent"
                aria-hidden
              />
              <div className="min-w-0 flex-1">
                <p className="truncate font-mono text-xs text-foreground">
                  {item.file_name ?? item.id}
                </p>
                <p className="mt-0.5 text-[11px] text-muted">
                  {new Date(item.created_at).toLocaleString()}
                </p>
              </div>
              <span className="shrink-0 rounded-md border border-accent/40 bg-accent/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-accent">
                {stepLabel(item.status)}
              </span>
            </div>
          ))}
        </QueuePanel>

        <QueuePanel
          title="Pending Verification Queue"
          subtitle="J/K navigate · Ctrl+Enter release"
          emptyLabel="No documents waiting for verification."
          loading={queueLoading && pendingVerification.length === 0}
          className="border-t border-border lg:border-l lg:border-t-0"
        >
          {pendingVerification.map((item) => {
            const active = item.id === selectedItem?.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setSelectedId(item.id)}
                className={`flex w-full items-center gap-3 border-b border-border/60 px-4 py-3 text-left last:border-b-0 transition ${
                  active ? "bg-accent/10" : "hover:bg-surface-elevated"
                }`}
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate font-mono text-xs text-foreground">
                    {item.file_name ?? item.id}
                  </p>
                  <p className="mt-0.5 text-[11px] text-muted">
                    {new Date(item.created_at).toLocaleString()}
                  </p>
                </div>
                <span className="shrink-0 rounded-md border border-border bg-background px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
                  Review
                </span>
              </button>
            );
          })}
        </QueuePanel>
      </div>

      {queueLoading && !selectedItem && activeIngestion.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <div className="flex flex-col items-center gap-3 text-muted">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
            <p className="text-sm">Syncing local queues…</p>
          </div>
        </div>
      ) : !selectedItem ? (
        <div className="min-h-0 flex-1">
          <QueueClearState />
        </div>
      ) : (
        <div
          className={`flex min-h-0 flex-1 transition-[outline] duration-300 ${
            successFlash ? "outline outline-3 outline-success" : "outline-none"
          }`}
        >
          <section className="flex w-[60%] min-w-0 flex-col border-r border-border bg-surface">
            <PdfJsViewer
              fileName={selectedItem.file_name}
              documentUrl={documentUrl}
              loading={documentLoading}
              error={null}
              highlights={highlights}
              activeFieldKey={activeFieldKey}
            />
          </section>

          <section
            ref={reviewPaneRef}
            className="flex w-[40%] min-w-0 flex-col bg-surface-elevated"
          >
            <header className="border-b border-border px-6 py-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent">
                    Data Validation
                  </p>
                  <h2 className="mt-1 text-lg font-semibold text-foreground">
                    Review Extracted Fields
                  </h2>
                  <p className="mt-1 text-xs text-muted">
                    Job {selectedItem.id.slice(0, 8)} ·{" "}
                    {new Date(selectedItem.created_at).toLocaleString()}
                  </p>
                </div>
                {draftSavedAt && (
                  <span className="shrink-0 rounded-md border border-success/40 bg-success/10 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-success">
                    Draft saved
                  </span>
                )}
              </div>
              <p className="mt-2 text-[11px] text-muted">
                Tab / 1–9 focus fields · Ctrl+S draft · Ctrl+Enter release ·
                Ctrl+Backspace reject
              </p>
            </header>

            <div className="flex-1 overflow-y-auto px-6 py-5">
              {isReviewReady && (
                <>
                  <div className="space-y-4">
                    {visibleFieldKeys.map((key, index) => {
                      const confidence = getFieldConfidence(
                        selectedItem.extracted_payload,
                        key,
                      );
                      return (
                        <label key={key} className="block">
                          <span className="mb-1.5 flex items-center justify-between gap-2 text-xs font-medium uppercase tracking-wide text-muted">
                            <span className="flex items-center gap-2">
                              {index < 9 && (
                                <kbd className="rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10px] text-muted">
                                  {index + 1}
                                </kbd>
                              )}
                              {key.replaceAll("_", " ")}
                            </span>
                            <ConfidenceBadge confidence={confidence} />
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
                            value={fields[key] ?? ""}
                            onChange={(event) =>
                              handleFieldChange(key, event.target.value)
                            }
                            onFocus={() => setActiveFieldKey(key)}
                            className={`w-full rounded-lg border bg-background px-3 py-2.5 text-sm text-foreground outline-none transition focus:ring-2 focus:ring-accent/20 ${
                              activeFieldKey === key
                                ? "border-accent"
                                : "border-border focus:border-accent"
                            }`}
                            placeholder={`Enter ${key.replaceAll("_", " ").toLowerCase()}…`}
                          />
                        </label>
                      );
                    })}
                  </div>

                  {normalizeExtractedPayload(selectedItem.extracted_payload)
                    .full_text && (
                    <div className="mt-6">
                      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">
                        Raw Extraction
                      </p>
                      <pre className="max-h-40 overflow-y-auto rounded-lg border border-border bg-background p-3 text-xs leading-relaxed text-muted">
                        {
                          normalizeExtractedPayload(
                            selectedItem.extracted_payload,
                          ).full_text
                        }
                      </pre>
                    </div>
                  )}
                </>
              )}

              {actionError && (
                <div className="mt-4 rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
                  {actionError}
                </div>
              )}
            </div>

            {isReviewReady && (
              <footer className="flex gap-3 border-t border-border px-6 py-4">
                <button
                  type="button"
                  onClick={() => setRejectOpen(true)}
                  disabled={actionLoading !== null}
                  className="flex-1 rounded-lg border border-danger/40 bg-danger/10 px-4 py-2.5 text-sm font-medium text-danger transition hover:bg-danger/20 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Reject
                </button>
                <button
                  type="button"
                  onClick={() => void handleApprove()}
                  disabled={actionLoading !== null}
                  className="flex-[1.4] rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {actionLoading === "approve"
                    ? "Releasing…"
                    : "Approve & Release"}
                </button>
              </footer>
            )}
          </section>
        </div>
      )}

      {rejectOpen && (
        <RejectModal
          reason={rejectReason}
          onReasonChange={setRejectReason}
          loading={actionLoading === "reject"}
          onCancel={() => setRejectOpen(false)}
          onConfirm={() => void handleRejectConfirm()}
        />
      )}
    </div>
  );
}

function ConfidenceBadge({ confidence }: { confidence: number | null }) {
  if (confidence == null) {
    return null;
  }
  const pct = Math.round(confidence * 100);
  const ok = confidence >= CONFIDENCE_OK_THRESHOLD;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${
        ok
          ? "bg-success/15 text-success"
          : "bg-amber-500/15 text-amber-400"
      }`}
      title={`Extraction confidence ${pct}%`}
    >
      {ok ? (
        <svg
          viewBox="0 0 16 16"
          className="h-3 w-3"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          aria-hidden
        >
          <path d="M3.5 8.5 6.5 11.5 12.5 4.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : (
        <svg
          viewBox="0 0 16 16"
          className="h-3 w-3"
          fill="currentColor"
          aria-hidden
        >
          <path d="M8 1.5 14.5 13.5H1.5L8 1.5Z" opacity="0.9" />
          <path d="M8 6v3.5" stroke="#0a0b0f" strokeWidth="1.5" strokeLinecap="round" fill="none" />
          <circle cx="8" cy="11.2" r="0.7" fill="#0a0b0f" />
        </svg>
      )}
      {pct}%
    </span>
  );
}

function RejectModal({
  reason,
  onReasonChange,
  loading,
  onCancel,
  onConfirm,
}: {
  reason: RejectReasonValue;
  onReasonChange: (value: RejectReasonValue) => void;
  loading: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="reject-modal-title"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onCancel();
        }
      }}
    >
      <div className="w-full max-w-md rounded-xl border border-border bg-surface-elevated p-5 shadow-2xl">
        <h3
          id="reject-modal-title"
          className="text-base font-semibold text-foreground"
        >
          Reject document
        </h3>
        <p className="mt-1 text-xs text-muted">
          Select a reason. This is recorded on the audit trail.
        </p>
        <label className="mt-4 block">
          <span className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-muted">
            Reason
          </span>
          <select
            autoFocus
            value={reason}
            onChange={(event) =>
              onReasonChange(event.target.value as RejectReasonValue)
            }
            className="w-full rounded-lg border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
          >
            {REJECT_REASONS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <div className="mt-5 flex gap-3">
          <button
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="flex-1 rounded-lg border border-border px-4 py-2.5 text-sm font-medium text-muted transition hover:bg-surface disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={loading}
            className="flex-1 rounded-lg border border-danger/40 bg-danger/10 px-4 py-2.5 text-sm font-semibold text-danger transition hover:bg-danger/20 disabled:opacity-50"
          >
            {loading ? "Rejecting…" : "Confirm Reject"}
          </button>
        </div>
      </div>
    </div>
  );
}

function QueuePanel({
  title,
  subtitle,
  emptyLabel,
  loading,
  children,
  className = "",
}: {
  title: string;
  subtitle: string;
  emptyLabel: string;
  loading: boolean;
  children: ReactNode;
  className?: string;
}) {
  const hasChildren = Array.isArray(children)
    ? children.length > 0
    : Boolean(children);

  return (
    <section className={`flex min-h-[9.5rem] flex-col bg-surface ${className}`}>
      <header className="flex items-baseline justify-between gap-3 border-b border-border px-4 py-2.5">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-[0.14em] text-foreground">
            {title}
          </h3>
          <p className="text-[11px] text-muted">{subtitle}</p>
        </div>
      </header>
      <div className="max-h-40 min-h-0 flex-1 overflow-y-auto">
        {loading ? (
          <p className="px-4 py-6 text-xs text-muted">Loading…</p>
        ) : !hasChildren ? (
          <p className="px-4 py-6 text-xs text-muted">{emptyLabel}</p>
        ) : (
          children
        )}
      </div>
    </section>
  );
}
