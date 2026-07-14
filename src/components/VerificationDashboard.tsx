"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { DocumentViewer } from "@/components/DocumentViewer";
import { IncomingDropzone } from "@/components/IncomingDropzone";
import { PipelinePhaseStepper } from "@/components/PipelinePhaseStepper";
import { QueueClearState } from "@/components/QueueClearState";
import {
  documentFileUrl,
  fetchQueue,
  patchDocument,
} from "@/lib/api";
import {
  buildVerifiedPayload,
  DocumentPipelineRow,
  getFieldValue,
  getOperatorIdentity,
  INGESTION_STEP_LABELS,
  normalizeExtractedPayload,
  PipelineStatus,
  PipelineStatusType,
  VERIFICATION_FIELD_KEYS,
  VerificationFieldKey,
} from "@/lib/types";

const REJECT_REASON = "UNREADABLE_DOCUMENT";
const POLL_MS = 2500;

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
  const [fields, setFields] = useState<Record<VerificationFieldKey, string>>({
    "Applicant Name": "",
    "ID Number": "",
    "Document Date": "",
  });
  const [actionLoading, setActionLoading] = useState<"approve" | "reject" | null>(
    null,
  );
  const [actionError, setActionError] = useState<string | null>(null);
  const pendingRef = useRef(pendingVerification);
  pendingRef.current = pendingVerification;

  const selectedItem =
    pendingVerification.find((row) => row.id === selectedId) ??
    pendingVerification[0] ??
    null;

  const populateFields = useCallback((item: DocumentPipelineRow) => {
    const nextFields = VERIFICATION_FIELD_KEYS.reduce(
      (acc, key) => {
        acc[key] = getFieldValue(item.extracted_payload, key);
        return acc;
      },
      {} as Record<VerificationFieldKey, string>,
    );
    setFields(nextFields);
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
      setDocumentUrl(null);
      setDocumentLoading(false);
      setFields({
        "Applicant Name": "",
        "ID Number": "",
        "Document Date": "",
      });
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

  const handleApprove = async () => {
    if (!selectedItem) {
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
      await refreshQueues();
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to approve document.",
      );
    } finally {
      setActionLoading(null);
    }
  };

  const handleReject = async () => {
    if (!selectedItem) {
      return;
    }

    setActionLoading("reject");
    setActionError(null);

    try {
      const operator = getOperatorIdentity();
      // Include any in-progress field corrections for the audit trail.
      const verifiedPayload = buildVerifiedPayload(
        fields,
        selectedItem.extracted_payload,
      );
      await patchDocument(selectedItem.id, {
        action: "reject",
        reject_reason: REJECT_REASON,
        verified_payload: verifiedPayload,
        verified_by: operator,
        actor: operator,
      });
      await refreshQueues();
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to reject document.",
      );
    } finally {
      setActionLoading(null);
    }
  };

  const isReviewReady =
    selectedItem?.status === PipelineStatus.PENDING_VERIFICATION;
  const stepperStatus =
    selectedItem?.status ?? activeIngestion[0]?.status ?? null;
  const stepperFileName =
    selectedItem?.file_name ?? activeIngestion[0]?.file_name ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col">
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
          subtitle="Awaiting clerk review"
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
                  active
                    ? "bg-accent/10"
                    : "hover:bg-surface-elevated"
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
        <div className="flex min-h-0 flex-1">
          <section className="flex w-[60%] min-w-0 flex-col border-r border-border bg-surface">
            <DocumentViewer
              fileName={selectedItem.file_name}
              documentUrl={documentUrl}
              loading={documentLoading}
              error={null}
            />
          </section>

          <section className="flex w-[40%] min-w-0 flex-col bg-surface-elevated">
            <header className="border-b border-border px-6 py-4">
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
            </header>

            <div className="flex-1 overflow-y-auto px-6 py-5">
              {isReviewReady && (
                <>
                  <div className="space-y-4">
                    {VERIFICATION_FIELD_KEYS.map((key) => (
                      <label key={key} className="block">
                        <span className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-muted">
                          {key}
                        </span>
                        <input
                          type="text"
                          value={fields[key]}
                          onChange={(event) =>
                            handleFieldChange(key, event.target.value)
                          }
                          className="w-full rounded-lg border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
                          placeholder={`Enter ${key.toLowerCase()}…`}
                        />
                      </label>
                    ))}
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
                  onClick={() => void handleReject()}
                  disabled={actionLoading !== null}
                  className="flex-1 rounded-lg border border-danger/40 bg-danger/10 px-4 py-2.5 text-sm font-medium text-danger transition hover:bg-danger/20 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {actionLoading === "reject" ? "Rejecting…" : "Reject"}
                </button>
                <button
                  type="button"
                  onClick={() => void handleApprove()}
                  disabled={actionLoading !== null}
                  className="flex-[1.4] rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {actionLoading === "approve" ? "Releasing…" : "Approve & Release"}
                </button>
              </footer>
            )}
          </section>
        </div>
      )}
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
