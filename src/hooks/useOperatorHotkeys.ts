"use client";

import { useEffect } from "react";

export type OperatorHotkeyHandlers = {
  /** Ctrl+Enter — approve & release current document. */
  onApproveRelease?: () => void;
  /** Ctrl+Backspace — open reject modal. */
  onReject?: () => void;
  /** J — next document in queue. */
  onNextDocument?: () => void;
  /** K — previous document in queue. */
  onPrevDocument?: () => void;
  /** Digit 1–9 — focus field at zero-based index (digit - 1). */
  onFocusField?: (index: number) => void;
  /** Ctrl+S — save local draft without release. */
  onSaveDraft?: () => void;
  /** Esc — clear focus / close modals. */
  onEscape?: () => void;
};

export type UseOperatorHotkeysOptions = OperatorHotkeyHandlers & {
  /** When false, bindings are inactive (e.g. while typing in a free-text modal). */
  enabled?: boolean;
};

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
    return true;
  }
  return target.isContentEditable;
}

/**
 * Global keyboard shortcuts for the Verification Station operator.
 *
 * Digit / J / K shortcuts are suppressed while an editable control has focus
 * so operators can still type field values. Modifier chords (Ctrl+Enter, etc.)
 * always fire so approve/reject/save remain one keystroke away from any field.
 */
export function useOperatorHotkeys({
  enabled = true,
  onApproveRelease,
  onReject,
  onNextDocument,
  onPrevDocument,
  onFocusField,
  onSaveDraft,
  onEscape,
}: UseOperatorHotkeysOptions): void {
  useEffect(() => {
    if (!enabled) {
      return;
    }

    const onKeyDown = (event: KeyboardEvent) => {
      const key = event.key;
      const ctrl = event.ctrlKey || event.metaKey;
      const editing = isEditableTarget(event.target);

      if (key === "Escape") {
        event.preventDefault();
        onEscape?.();
        return;
      }

      if (ctrl && key === "Enter") {
        event.preventDefault();
        onApproveRelease?.();
        return;
      }

      if (ctrl && (key === "Backspace" || key === "Delete")) {
        event.preventDefault();
        onReject?.();
        return;
      }

      if (ctrl && (key === "s" || key === "S")) {
        event.preventDefault();
        onSaveDraft?.();
        return;
      }

      // Plain letter / digit navigation must not hijack typing in inputs.
      if (editing || ctrl || event.altKey) {
        return;
      }

      if (key === "j" || key === "J") {
        event.preventDefault();
        onNextDocument?.();
        return;
      }

      if (key === "k" || key === "K") {
        event.preventDefault();
        onPrevDocument?.();
        return;
      }

      if (/^[1-9]$/.test(key)) {
        event.preventDefault();
        onFocusField?.(Number(key) - 1);
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    enabled,
    onApproveRelease,
    onReject,
    onNextDocument,
    onPrevDocument,
    onFocusField,
    onSaveDraft,
    onEscape,
  ]);
}
