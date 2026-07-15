/**
 * Helpers for the HITL correction form against run_api projections.
 *
 * `extracted_json` may be:
 * - a flat dict of primitive fields
 * - an ExtractedPayload-shaped object with a nested `fields` map
 * - a value wrapped under `extracted_payload`
 */

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function coerceDisplayString(value: unknown): string {
  if (value == null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (isPlainObject(value) && "value" in value) {
    return coerceDisplayString(value.value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** Resolve the operator-editable object root from an API payload. */
export function resolveEditableRoot(
  extracted: unknown,
): Record<string, unknown> {
  if (!isPlainObject(extracted)) {
    return {};
  }

  const nested = extracted.extracted_payload;
  if (isPlainObject(nested)) {
    return nested;
  }

  return extracted;
}

/**
 * Keys the clerk should edit. Prefer nested `fields`; otherwise all top-level
 * keys whose values are editable scalars/field objects (skip raw dumps).
 */
export function editableFieldKeys(extracted: unknown): string[] {
  const root = resolveEditableRoot(extracted);
  const fields = root.fields;

  if (isPlainObject(fields)) {
    return Object.keys(fields).sort();
  }

  const skip = new Set([
    "full_text",
    "summary",
    "field_meta",
    "class_id",
    "form_type",
    "display_name",
    "extracted_payload",
  ]);

  return Object.keys(root)
    .filter((key) => {
      if (skip.has(key)) {
        return false;
      }
      const value = root[key];
      if (value == null) {
        return true;
      }
      if (
        typeof value === "string" ||
        typeof value === "number" ||
        typeof value === "boolean"
      ) {
        return true;
      }
      if (isPlainObject(value) && "value" in value) {
        return true;
      }
      return false;
    })
    .sort();
}

export function getEditableFieldValue(
  extracted: unknown,
  key: string,
): string {
  const root = resolveEditableRoot(extracted);
  if (isPlainObject(root.fields)) {
    return coerceDisplayString(root.fields[key]);
  }
  return coerceDisplayString(root[key]);
}

/**
 * Merge clerk form edits into the original payload shape so confidence/bbox
 * metadata survives string-only corrections.
 */
export function buildCorrectedData(
  original: unknown,
  formValues: Record<string, string>,
): Record<string, unknown> {
  if (!isPlainObject(original)) {
    return { ...formValues };
  }

  // Keep outer envelope if present (class_id + nested extracted_payload).
  if (isPlainObject(original.extracted_payload)) {
    return {
      ...original,
      extracted_payload: buildCorrectedData(
        original.extracted_payload,
        formValues,
      ),
    };
  }

  if (isPlainObject(original.fields)) {
    const nextFields: Record<string, unknown> = { ...original.fields };
    for (const [key, nextValue] of Object.entries(formValues)) {
      const previous = nextFields[key];
      if (isPlainObject(previous) && "value" in previous) {
        nextFields[key] = { ...previous, value: nextValue };
      } else {
        nextFields[key] = nextValue;
      }
    }
    return {
      ...original,
      fields: nextFields,
    };
  }

  const next: Record<string, unknown> = { ...original };
  for (const [key, nextValue] of Object.entries(formValues)) {
    const previous = next[key];
    if (isPlainObject(previous) && "value" in previous) {
      next[key] = { ...previous, value: nextValue };
    } else {
      next[key] = nextValue;
    }
  }
  return next;
}

export function humanizeFieldKey(key: string): string {
  return key.replaceAll("_", " ");
}
