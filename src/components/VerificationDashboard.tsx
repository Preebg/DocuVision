"use client";

import { useCallback, useEffect, useState } from "react";
import { DocumentViewer } from "@/components/DocumentViewer";
import { IncomingDropzone } from "@/components/IncomingDropzone";
import { PipelinePhaseStepper } from "@/components/PipelinePhaseStepper";
import { QueueClearState } from "@/components/QueueClearState";
import { getSupabaseClient } from "@/lib/supabase/client";
import {
  buildVerifiedPayload,
  DEFAULT_STORAGE_BUCKET,
  DocumentPipelineRow,
  getFieldValue,
  HITL_REVIEW_STATUSES,
  INCOMING_STORAGE_BUCKET,
  IN_PROGRESS_STATUSES,
  normalizeExtractedPayload,
  PipelineStatus,
  VERIFICATION_FIELD_KEYS,
  VERIFIED_BY,
  VerificationFieldKey,
} from "@/lib/types";

const REJECT_REASON = "UNREADABLE_DOCUMENT";

export function VerificationDashboard() {
  const [queueItem, setQueueItem] = useState<DocumentPipelineRow | null>(null);
  const [inProgressItem, setInProgressItem] =
    useState<DocumentPipelineRow | null>(null);
  const [queueLoading, setQueueLoading] = useState(true);
  const [documentUrl, setDocumentUrl] = useState<string | null>(null);
  const [documentLoading, setDocumentLoading] = useState(false);
  const [documentError, setDocumentError] = useState<string | null>(null);
  const [fields, setFields] = useState<Record<VerificationFieldKey, string>>({
    "Applicant Name": "",
    "ID Number": "",
    "Document Date": "",
  });
  const [actionLoading, setActionLoading] = useState<"approve" | "reject" | null>(
    null,
  );
  const [actionError, setActionError] = useState<string | null>(null);

  const loadDocumentUrl = useCallback(async (item: DocumentPipelineRow) => {
    if (!item.file_name) {
      setDocumentUrl(null);
      setDocumentError("This queue item has no associated file name.");
      return;
    }

    setDocumentLoading(true);
    setDocumentError(null);

    try {
      const supabase = getSupabaseClient();
      const bucket =
        item.storage_bucket ??
        (item.status === PipelineStatus.PENDING_VERIFICATION
          ? DEFAULT_STORAGE_BUCKET
          : INCOMING_STORAGE_BUCKET);

      const { data, error } = await supabase.storage
        .from(bucket)
        .createSignedUrl(item.file_name, 3600);

      if (error || !data?.signedUrl) {
        if (
          item.status === PipelineStatus.QUEUED ||
          item.status === PipelineStatus.SCANNING
        ) {
          setDocumentUrl(null);
          setDocumentError(null);
          return;
        }
        throw new Error(
          error?.message ??
            "Could not generate a signed URL for the verification document.",
        );
      }

      setDocumentUrl(data.signedUrl);
    } catch (err) {
      setDocumentUrl(null);
      setDocumentError(
        err instanceof Error ? err.message : "Failed to load document preview.",
      );
    } finally {
      setDocumentLoading(false);
    }
  }, []);

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

  const fetchNextPending = useCallback(async () => {
    setQueueLoading(true);
    setActionError(null);

    try {
      const supabase = getSupabaseClient();

      // Ready for human review first — extraction already done; watcher may be
      // scanning the next incoming_forms object on the device in parallel.
      const { data: readyData, error: readyError } = await supabase
        .from("document_pipeline")
        .select("*")
        .in("status", HITL_REVIEW_STATUSES)
        .order("created_at", { ascending: true })
        .limit(1)
        .maybeSingle();

      if (readyError) {
        throw readyError;
      }

      const { data: scanningData, error: scanningError } = await supabase
        .from("document_pipeline")
        .select("*")
        .in("status", IN_PROGRESS_STATUSES)
        .order("created_at", { ascending: true })
        .limit(1)
        .maybeSingle();

      if (scanningError) {
        throw scanningError;
      }

      const readyItem = readyData as DocumentPipelineRow | null;
      const scanningItem = scanningData as DocumentPipelineRow | null;
      const item = readyItem ?? scanningItem;

      setQueueItem(item);
      setInProgressItem(
        readyItem && scanningItem && scanningItem.id !== readyItem.id
          ? scanningItem
          : null,
      );

      if (item) {
        if (item.status === PipelineStatus.PENDING_VERIFICATION) {
          populateFields(item);
        } else {
          setFields({
            "Applicant Name": "",
            "ID Number": "",
            "Document Date": "",
          });
        }
        await loadDocumentUrl(item);
      } else {
        setDocumentUrl(null);
        setDocumentError(null);
      }
    } catch (err) {
      setQueueItem(null);
      setInProgressItem(null);
      setActionError(
        err instanceof Error ? err.message : "Failed to load verification queue.",
      );
    } finally {
      setQueueLoading(false);
    }
  }, [loadDocumentUrl, populateFields]);

  useEffect(() => {
    void fetchNextPending();
  }, [fetchNextPending]);

  useEffect(() => {
    const supabase = getSupabaseClient();
    const channel = supabase
      .channel("document_pipeline_live")
      .on(
        "postgres_changes",
        { event: "*", schema: "public", table: "document_pipeline" },
        () => {
          void fetchNextPending();
        },
      )
      .subscribe();

    const poll = window.setInterval(() => {
      void fetchNextPending();
    }, 2500);

    return () => {
      window.clearInterval(poll);
      void supabase.removeChannel(channel);
    };
  }, [fetchNextPending]);

  const handleFieldChange = (key: VerificationFieldKey, value: string) => {
    setFields((current) => ({ ...current, [key]: value }));
  };

  const handleApprove = async () => {
    if (!queueItem) {
      return;
    }

    setActionLoading("approve");
    setActionError(null);

    try {
      const supabase = getSupabaseClient();
      const verifiedPayload = buildVerifiedPayload(
        fields,
        queueItem.extracted_payload,
      );

      const { error } = await supabase
        .from("document_pipeline")
        .update({
          status: PipelineStatus.VERIFIED,
          verified_payload: verifiedPayload,
          verified_by: VERIFIED_BY,
          reject_reason: null,
        })
        .eq("id", queueItem.id)
        .eq("status", PipelineStatus.PENDING_VERIFICATION);

      if (error) {
        throw error;
      }

      await fetchNextPending();
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to approve document.",
      );
    } finally {
      setActionLoading(null);
    }
  };

  const handleReject = async () => {
    if (!queueItem) {
      return;
    }

    setActionLoading("reject");
    setActionError(null);

    try {
      const supabase = getSupabaseClient();
      const { error } = await supabase
        .from("document_pipeline")
        .update({
          status: PipelineStatus.FAILED,
          reject_reason: REJECT_REASON,
          verified_by: VERIFIED_BY,
        })
        .eq("id", queueItem.id)
        .eq("status", PipelineStatus.PENDING_VERIFICATION);

      if (error) {
        throw error;
      }

      await fetchNextPending();
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to reject document.",
      );
    } finally {
      setActionLoading(null);
    }
  };

  const isReviewReady =
    queueItem?.status === PipelineStatus.PENDING_VERIFICATION;
  const isProcessing =
    queueItem?.status === PipelineStatus.QUEUED ||
    queueItem?.status === PipelineStatus.SCANNING;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <IncomingDropzone onUploaded={() => void fetchNextPending()} />
      <PipelinePhaseStepper
        status={queueItem?.status ?? null}
        fileName={queueItem?.file_name}
      />

      {inProgressItem && isReviewReady && (
        <div className="border-b border-accent/25 bg-accent/5 px-6 py-2.5 text-xs text-muted">
          Device is scanning{" "}
          <span className="font-mono text-foreground">
            {inProgressItem.file_name}
          </span>
          {inProgressItem.processing_device
            ? ` on ${inProgressItem.processing_device}`
            : ""}
          {" · "}
          you can approve or reject the document below meanwhile.
        </div>
      )}

      {queueLoading && !queueItem ? (
        <div className="flex flex-1 items-center justify-center">
          <div className="flex flex-col items-center gap-3 text-muted">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
            <p className="text-sm">Syncing verification queue…</p>
          </div>
        </div>
      ) : !queueItem ? (
        <div className="min-h-0 flex-1">
          <QueueClearState />
        </div>
      ) : (
        <div className="flex min-h-0 flex-1">
          <section className="flex w-[60%] min-w-0 flex-col border-r border-border bg-surface">
            <DocumentViewer
              fileName={queueItem.file_name}
              documentUrl={documentUrl}
              loading={documentLoading}
              error={
                documentError ??
                (isProcessing
                  ? "Preview may appear after the file is staged for verification."
                  : null)
              }
            />
          </section>

          <section className="flex w-[40%] min-w-0 flex-col bg-surface-elevated">
            <header className="border-b border-border px-6 py-4">
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent">
                {isReviewReady ? "Data Validation" : "In Progress"}
              </p>
              <h2 className="mt-1 text-lg font-semibold text-foreground">
                {isReviewReady
                  ? "Review Extracted Fields"
                  : queueItem.status === PipelineStatus.QUEUED
                    ? "Queued — waiting for scanner"
                    : "Scanning document…"}
              </h2>
              <p className="mt-1 text-xs text-muted">
                Queue #{queueItem.id} ·{" "}
                {new Date(queueItem.created_at).toLocaleString()}
              </p>
            </header>

            <div className="flex-1 overflow-y-auto px-6 py-5">
              {isProcessing && (
                <div className="rounded-lg border border-accent/30 bg-accent/5 px-4 py-3 text-sm text-muted">
                  The watcher is processing this file one-at-a-time on{" "}
                  <span className="font-mono text-foreground">
                    {queueItem.processing_device ?? "the local device"}
                  </span>
                  . When extraction finishes it moves here for approve/reject,
                  and the next{" "}
                  <span className="font-mono text-foreground">incoming_forms</span>{" "}
                  file starts scanning.
                </div>
              )}

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

                  {normalizeExtractedPayload(queueItem.extracted_payload)
                    .full_text && (
                    <div className="mt-6">
                      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">
                        Raw Extraction
                      </p>
                      <pre className="max-h-40 overflow-y-auto rounded-lg border border-border bg-background p-3 text-xs leading-relaxed text-muted">
                        {
                          normalizeExtractedPayload(queueItem.extracted_payload)
                            .full_text
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
