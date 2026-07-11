"use client";

import { useCallback, useEffect, useState } from "react";
import { DocumentViewer } from "@/components/DocumentViewer";
import { QueueClearState } from "@/components/QueueClearState";
import { getSupabaseClient } from "@/lib/supabase/client";
import {
  buildVerifiedPayload,
  DEFAULT_STORAGE_BUCKET,
  DocumentPipelineRow,
  getFieldValue,
  normalizeExtractedPayload,
  VERIFICATION_FIELD_KEYS,
  VERIFIED_BY,
  VerificationFieldKey,
} from "@/lib/types";

const REJECT_REASON = "UNREADABLE_DOCUMENT";

export function VerificationDashboard() {
  const [queueItem, setQueueItem] = useState<DocumentPipelineRow | null>(null);
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
      const bucket = item.storage_bucket ?? DEFAULT_STORAGE_BUCKET;
      const { data, error } = await supabase.storage
        .from(bucket)
        .createSignedUrl(item.file_name, 3600);

      if (error || !data?.signedUrl) {
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
    setDocumentUrl(null);
    setDocumentError(null);

    try {
      const supabase = getSupabaseClient();
      const { data, error } = await supabase
        .from("document_pipeline")
        .select("*")
        .eq("status", "PENDING_VERIFICATION")
        .order("created_at", { ascending: true })
        .limit(1)
        .maybeSingle();

      if (error) {
        throw error;
      }

      const item = data as DocumentPipelineRow | null;
      setQueueItem(item);

      if (item) {
        populateFields(item);
        await loadDocumentUrl(item);
      }
    } catch (err) {
      setQueueItem(null);
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
          status: "VERIFIED",
          verified_payload: verifiedPayload,
          verified_by: VERIFIED_BY,
          reject_reason: null,
        })
        .eq("id", queueItem.id)
        .eq("status", "PENDING_VERIFICATION");

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
          status: "FAILED",
          reject_reason: REJECT_REASON,
          verified_by: VERIFIED_BY,
        })
        .eq("id", queueItem.id)
        .eq("status", "PENDING_VERIFICATION");

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

  if (queueLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex flex-col items-center gap-3 text-muted">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
          <p className="text-sm">Syncing verification queue…</p>
        </div>
      </div>
    );
  }

  if (!queueItem) {
    return <QueueClearState />;
  }

  const normalized = normalizeExtractedPayload(queueItem.extracted_payload);

  return (
    <div className="flex h-full min-h-0">
      {/* Left panel — 60% document viewport */}
      <section className="flex w-[60%] min-w-0 flex-col border-r border-border bg-surface">
        <DocumentViewer
          fileName={queueItem.file_name}
          documentUrl={documentUrl}
          loading={documentLoading}
          error={documentError}
        />
      </section>

      {/* Right panel — 40% validation */}
      <section className="flex w-[40%] min-w-0 flex-col bg-surface-elevated">
        <header className="border-b border-border px-6 py-4">
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent">
            Data Validation
          </p>
          <h2 className="mt-1 text-lg font-semibold text-foreground">
            Review Extracted Fields
          </h2>
          <p className="mt-1 text-xs text-muted">
            Queue #{queueItem.id} ·{" "}
            {new Date(queueItem.created_at).toLocaleString()}
          </p>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          <div className="space-y-4">
            {VERIFICATION_FIELD_KEYS.map((key) => (
              <label key={key} className="block">
                <span className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-muted">
                  {key}
                </span>
                <input
                  type="text"
                  value={fields[key]}
                  onChange={(event) => handleFieldChange(key, event.target.value)}
                  className="w-full rounded-lg border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
                  placeholder={`Enter ${key.toLowerCase()}…`}
                />
              </label>
            ))}
          </div>

          {normalized.full_text && (
            <div className="mt-6">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">
                Raw Extraction
              </p>
              <pre className="max-h-40 overflow-y-auto rounded-lg border border-border bg-background p-3 text-xs leading-relaxed text-muted">
                {normalized.full_text}
              </pre>
            </div>
          )}

          {actionError && (
            <div className="mt-4 rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">
              {actionError}
            </div>
          )}
        </div>

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
      </section>
    </div>
  );
}
