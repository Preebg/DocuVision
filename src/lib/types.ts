import pipelineStatusContract from "../../contracts/pipeline-status.json";

type ContractStages = typeof pipelineStatusContract.STAGES;

/** Runtime constant mirrored from `contracts/pipeline-status.json`. */
export const PipelineStatus = pipelineStatusContract.STAGES as {
  readonly [K in keyof ContractStages]: K;
};

/** Compiler type aligned 1:1 with the shared pipeline status contract. */
export type PipelineStatusType = keyof typeof PipelineStatus;

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
  status: PipelineStatusType | null;
  error_message: string | null;
  verified_payload: ExtractedPayload | null;
  verified_by: string | null;
  reject_reason: string | null;
  storage_bucket: string | null;
  processing_device?: string | null;
  processing_started_at?: string | null;
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

/** Statuses shown in the HITL review pane (prefer ready reviews over in-flight scans). */
export const HITL_REVIEW_STATUSES: PipelineStatusType[] = [
  PipelineStatus.PENDING_VERIFICATION,
];

/** Intake statuses still being scanned on the watcher / worker device. */
export const IN_PROGRESS_STATUSES: PipelineStatusType[] = [
  PipelineStatus.QUEUED,
  PipelineStatus.SCANNING,
  PipelineStatus.LAYOUT,
  PipelineStatus.VISION,
  PipelineStatus.STAGING,
];

export const QUEUE_STATUSES: PipelineStatusType[] = [
  ...IN_PROGRESS_STATUSES,
  ...HITL_REVIEW_STATUSES,
];

export const PIPELINE_PHASES = [
  {
    status: PipelineStatus.QUEUED,
    label: "Queued",
    description: "File landed in intake storage",
  },
  {
    status: PipelineStatus.SCANNING,
    label: "Scanning",
    description: "Layout + vision extraction running",
  },
  {
    status: PipelineStatus.PENDING_VERIFICATION,
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

export function phaseIndexForStatus(
  status: PipelineStatusType | null | undefined,
): number {
  if (!status) {
    return -1;
  }

  const index = PIPELINE_PHASES.findIndex((phase) => phase.status === status);
  return index;
}
