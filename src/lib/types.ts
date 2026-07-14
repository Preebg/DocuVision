import pipelineStatusContract from "../../contracts/pipeline-status.json";
import {
  ALL_FORM_FIELD_KEYS,
  DOCUMENT_CLASS_SCHEMAS,
  SCHEMA_FIELD_KEYS,
  VERIFICATION_FIELD_KEYS,
  type DocumentClassId,
  type FormFieldKey,
  type VerificationFieldKey,
} from "./form-types.gen";

export {
  ALL_FORM_FIELD_KEYS,
  DOCUMENT_CLASS_SCHEMAS,
  SCHEMA_FIELD_KEYS,
  VERIFICATION_FIELD_KEYS,
};
export type { DocumentClassId, FormFieldKey, VerificationFieldKey };

type ContractStages = typeof pipelineStatusContract.STAGES;

/** Runtime constant mirrored from `contracts/pipeline-status.json`. */
export const PipelineStatus = pipelineStatusContract.STAGES as {
  readonly [K in keyof ContractStages]: K;
};

/** Compiler type aligned 1:1 with the shared pipeline status contract. */
export type PipelineStatusType = keyof typeof PipelineStatus;

export type ExtractedFields = Partial<Record<FormFieldKey, string>> & {
  [key: string]: string | undefined;
};

export interface ExtractedPayload {
  full_text?: string;
  summary?: string;
  fields?: ExtractedFields;
  extracted_payload?: ExtractedPayload;
  class_id?: DocumentClassId | string;
  form_type?: string;
  display_name?: string;
}

export interface DocumentPipelineRow {
  id: string;
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

export const VERIFIED_BY = "Web_Operator_V1";
export const OPERATOR_IDENTITY_STORAGE_KEY = "docuvision_operator_identity";
export const DEFAULT_STORAGE_BUCKET = "verification_queue";
export const INCOMING_STORAGE_BUCKET = "incoming_forms";

/** Empty HITL form state keyed by the generated semantic field registry. */
export function emptyVerificationFields(): Record<VerificationFieldKey, string> {
  return VERIFICATION_FIELD_KEYS.reduce(
    (acc, key) => {
      acc[key] = "";
      return acc;
    },
    {} as Record<VerificationFieldKey, string>,
  );
}

/** Field keys the clerk should review for a given extracted payload / class. */
export function fieldKeysForPayload(
  payload: ExtractedPayload | null | undefined,
): readonly VerificationFieldKey[] {
  const normalized = normalizeExtractedPayload(payload);
  const classId = (normalized.class_id ??
    (payload as ExtractedPayload | null | undefined)?.class_id ??
    (payload as ExtractedPayload | null | undefined)?.form_type) as
    | DocumentClassId
    | undefined;

  if (classId && classId in SCHEMA_FIELD_KEYS) {
    return SCHEMA_FIELD_KEYS[classId] as readonly VerificationFieldKey[];
  }

  const present = Object.keys(normalized.fields ?? {}).filter((key) =>
    (VERIFICATION_FIELD_KEYS as readonly string[]).includes(key),
  ) as VerificationFieldKey[];
  return present.length > 0 ? present : VERIFICATION_FIELD_KEYS;
}

/**
 * Resolve the clerk / operator identity for non-repudiation on PATCH.
 * Priority: localStorage → NEXT_PUBLIC_OPERATOR_IDENTITY → VERIFIED_BY fallback.
 */
export function getOperatorIdentity(): string {
  if (typeof window !== "undefined") {
    const stored = window.localStorage
      .getItem(OPERATOR_IDENTITY_STORAGE_KEY)
      ?.trim();
    if (stored) {
      return stored;
    }
  }
  const fromEnv = process.env.NEXT_PUBLIC_OPERATOR_IDENTITY?.trim();
  if (fromEnv) {
    return fromEnv;
  }
  return VERIFIED_BY;
}

export function setOperatorIdentity(identity: string): void {
  if (typeof window === "undefined") {
    return;
  }
  const cleaned = identity.trim();
  if (cleaned) {
    window.localStorage.setItem(OPERATOR_IDENTITY_STORAGE_KEY, cleaned);
  }
}
/** Statuses shown in the HITL review pane. */
export const HITL_REVIEW_STATUSES: PipelineStatusType[] = [
  PipelineStatus.PENDING_VERIFICATION,
];

/**
 * Machine stages rendered in the Active Ingestion Queue
 * (AI layout / vision work in flight).
 */
export const ACTIVE_INGESTION_STATUSES: PipelineStatusType[] = [
  PipelineStatus.SCANNING,
  PipelineStatus.LAYOUT,
  PipelineStatus.VISION,
];

/** Broader intake statuses including queueing / staging. */
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

export const INGESTION_STEP_LABELS: Partial<
  Record<PipelineStatusType, string>
> = {
  SCANNING: "Scanning",
  LAYOUT: "Layout",
  VISION: "Vision",
};

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
      class_id: raw.class_id ?? nested.class_id,
      form_type: raw.form_type ?? nested.form_type,
      display_name: raw.display_name ?? nested.display_name,
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
  fields: Partial<Record<VerificationFieldKey, string>>,
  sourcePayload: ExtractedPayload | null | undefined,
): ExtractedPayload {
  const normalized = normalizeExtractedPayload(sourcePayload);
  const keys = fieldKeysForPayload(sourcePayload);
  const updates: ExtractedFields = {};
  for (const key of keys) {
    updates[key] = fields[key] ?? "";
  }

  return {
    ...normalized,
    fields: {
      ...(normalized.fields ?? {}),
      ...updates,
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
