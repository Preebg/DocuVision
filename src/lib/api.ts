import { getOperatorIdentity } from "@/lib/types";

/** Base URL for the local FastAPI service (static SPA / Tauri). */
export function getApiBaseUrl(): string {
  const raw = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (raw) {
    return raw.replace(/\/$/, "");
  }
  return "http://localhost:8000";
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
