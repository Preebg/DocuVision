export type PipelineStatus =
  | "IMPORTED"
  | "SCANNING"
  | "PENDING_VERIFICATION"
  | "VERIFIED"
  | "FAILED"
  | "COMPLETED"
  | "processed";

export interface ExtractedFields {
  "Applicant Name"?: string;
  "ID Number"?: string;
  "Document Date"?: string;
  [key: string]: string | undefined;
}

export interface ExtractedPayload {
  full_text?: string;
  summary?: string;
  fields?: ExtractedFields;
  extracted_payload?: ExtractedPayload;
}

export interface DocumentPipelineRow {
  id: number;
  created_at: string;
  file_name: string | null;
  extracted_payload: ExtractedPayload | null;
  status: PipelineStatus | null;
  error_message: string | null;
  verified_payload: ExtractedPayload | null;
  verified_by: string | null;
  reject_reason: string | null;
  storage_bucket: string | null;
}

export const VERIFICATION_FIELD_KEYS = [
  "Applicant Name",
  "ID Number",
  "Document Date",
] as const;

export type VerificationFieldKey = (typeof VERIFICATION_FIELD_KEYS)[number];

export const VERIFIED_BY = "Web_Operator_V1";
export const DEFAULT_STORAGE_BUCKET = "verification_queue";
export const INCOMING_STORAGE_BUCKET = "incoming_forms";

export const QUEUE_STATUSES: PipelineStatus[] = [
  "IMPORTED",
  "SCANNING",
  "PENDING_VERIFICATION",
];

export const PIPELINE_PHASES = [
  {
    status: "IMPORTED" as const,
    label: "Imported",
    description: "File landed in intake storage",
  },
  {
    status: "SCANNING" as const,
    label: "Scanning",
    description: "Layout + vision extraction running",
  },
  {
    status: "PENDING_VERIFICATION" as const,
    label: "Data Retrieved",
    description: "Ready for human verification",
  },
] as const;

export function normalizeExtractedPayload(
  raw: ExtractedPayload | null | undefined,
): ExtractedPayload {
  if (!raw) {
    return { fields: {} };
  }

  const nested = raw.extracted_payload;
  if (nested && typeof nested === "object") {
    return {
      ...nested,
      fields: {
        ...(nested.fields ?? {}),
        ...(raw.fields ?? {}),
      },
    };
  }

  return {
    ...raw,
    fields: raw.fields ?? {},
  };
}

export function getFieldValue(
  payload: ExtractedPayload | null | undefined,
  key: VerificationFieldKey,
): string {
  const normalized = normalizeExtractedPayload(payload);
  const value = normalized.fields?.[key];
  return typeof value === "string" ? value : "";
}

export function buildVerifiedPayload(
  fields: Record<VerificationFieldKey, string>,
  sourcePayload: ExtractedPayload | null | undefined,
): ExtractedPayload {
  const normalized = normalizeExtractedPayload(sourcePayload);

  return {
    ...normalized,
    fields: {
      ...(normalized.fields ?? {}),
      ...fields,
    },
  };
}

export function isPdfFileName(fileName: string | null | undefined): boolean {
  return Boolean(fileName?.toLowerCase().endsWith(".pdf"));
}

export function phaseIndexForStatus(status: PipelineStatus | null | undefined): number {
  if (!status) {
    return -1;
  }

  const index = PIPELINE_PHASES.findIndex((phase) => phase.status === status);
  return index;
}
