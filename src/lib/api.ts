import { getOperatorIdentity } from "@/lib/types";

/** Base URL for the local FastAPI service (static SPA / Tauri). */
export function getApiBaseUrl(): string {
  const raw = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (raw) {
    return raw.replace(/\/$/, "");
  }
  return "http://127.0.0.1:8000";
}

export function apiUrl(path: string): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${getApiBaseUrl()}${normalized}`;
}

export type QueuePayload = {
  active_ingestion: import("@/lib/types").DocumentPipelineRow[];
  pending_verification: import("@/lib/types").DocumentPipelineRow[];
};

export async function fetchQueue(signal?: AbortSignal): Promise<QueuePayload> {
  const response = await fetch(apiUrl("/api/queue"), {
    method: "GET",
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new Error(`Queue request failed (${response.status})`);
  }
  return (await response.json()) as QueuePayload;
}

export async function ingestDocument(file: File): Promise<{
  id: string;
  file_name: string;
  status: string;
}> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(apiUrl("/api/documents/ingest"), {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Ingest failed (${response.status})`);
  }
  return (await response.json()) as {
    id: string;
    file_name: string;
    status: string;
  };
}

export type DocumentPatchBody = {
  action: "approve" | "reject";
  verified_payload?: import("@/lib/types").ExtractedPayload | null;
  /** Clerk / operator identity (LAN username, machine id, or email). */
  verified_by?: string;
  actor?: string;
  reject_reason?: string | null;
};

const OPERATOR_HEADER = "X-Operator-Identity";

export async function patchDocument(
  id: string,
  body: DocumentPatchBody,
): Promise<import("@/lib/types").DocumentPipelineRow> {
  const operator =
    body.verified_by?.trim() ||
    body.actor?.trim() ||
    getOperatorIdentity();

  const payload: DocumentPatchBody = {
    ...body,
    verified_by: operator,
    actor: operator,
  };

  const response = await fetch(apiUrl(`/api/documents/${encodeURIComponent(id)}`), {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      [OPERATOR_HEADER]: operator,
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Update failed (${response.status})`);
  }
  return (await response.json()) as import("@/lib/types").DocumentPipelineRow;
}

export function documentFileUrl(id: string): string {
  return apiUrl(`/api/documents/${encodeURIComponent(id)}/file`);
}

/* ---------------------------------------------------------------------------
 * HITL queue / detail / verify (run_api.py projections)
 * metadata.local_path → file_path
 * metadata.extracted_payload → extracted_json
 * --------------------------------------------------------------------------- */

export type HitlDocumentListItem = {
  id: string;
  filename: string;
  file_path: string | null;
  status: string;
  created_at: string;
};

export type HitlBatchPriority = "High" | "Medium" | "Low";

export type HitlPendingBatch = {
  batch_id: string;
  name: string;
  priority: HitlBatchPriority | string;
  total_documents: number;
  pending_documents: number;
  created_at: string;
  /** First PENDING_VERIFICATION document in sequence order. */
  first_pending_document_id: string | null;
  /** Compact operator-facing tag (e.g. A1B2C3). */
  batch_display: string;
};

export type HitlDocumentDetail = {
  id: string;
  filename: string;
  status: string;
  retry_count: number;
  metadata: Record<string, unknown>;
  created_at: string;
  file_path: string | null;
  /** Projected from metadata.extracted_payload (object, not double-encoded). */
  extracted_json: unknown;
  /** Batch membership + sequence navigation (null when not in a batch). */
  batch_id?: string | null;
  batch_name?: string | null;
  batch_display?: string | null;
  sequence_no?: number | null;
  batch_document_count?: number | null;
  prev_document_id?: string | null;
  next_document_id?: string | null;
  /** Next PENDING_VERIFICATION document after this one in batch sequence. */
  next_pending_document_id?: string | null;
};

export type HitlVerifyResponse = {
  status: string;
  id: string;
  document_status: string;
};

export async function fetchHitlDocuments(
  status: string = "VERIFYING",
  signal?: AbortSignal,
): Promise<HitlDocumentListItem[]> {
  const params = new URLSearchParams({ status });
  const response = await fetch(apiUrl(`/api/documents?${params}`), {
    method: "GET",
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new Error(`Documents list failed (${response.status})`);
  }
  return (await response.json()) as HitlDocumentListItem[];
}

export async function fetchPendingBatches(
  signal?: AbortSignal,
): Promise<HitlPendingBatch[]> {
  const response = await fetch(apiUrl("/api/batches/pending"), {
    method: "GET",
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new Error(`Pending batches failed (${response.status})`);
  }
  const payload = (await response.json()) as
    | HitlPendingBatch[]
    | { batches: HitlPendingBatch[] };
  if (Array.isArray(payload)) {
    return payload;
  }
  return payload.batches ?? [];
}

export async function fetchHitlDocument(
  id: string,
  signal?: AbortSignal,
): Promise<HitlDocumentDetail> {
  const response = await fetch(
    apiUrl(`/api/documents/${encodeURIComponent(id)}`),
    {
      method: "GET",
      cache: "no-store",
      signal,
    },
  );
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Document detail failed (${response.status})`);
  }
  return (await response.json()) as HitlDocumentDetail;
}

export async function verifyHitlDocument(
  id: string,
  correctedData: Record<string, unknown>,
): Promise<HitlVerifyResponse> {
  const response = await fetch(
    apiUrl(`/api/documents/${encodeURIComponent(id)}/verify`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ corrected_data: correctedData }),
    },
  );
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Verify failed (${response.status})`);
  }
  return (await response.json()) as HitlVerifyResponse;
}

export async function fetchHealth(signal?: AbortSignal): Promise<{
  status: string;
  repository: string;
}> {
  const response = await fetch(apiUrl("/api/health"), {
    method: "GET",
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new Error(`Health check failed (${response.status})`);
  }
  return (await response.json()) as { status: string; repository: string };
}
