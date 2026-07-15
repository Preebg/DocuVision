"use client";

import { useCallback, useEffect, useState } from "react";
import { BatchWorkbench } from "@/components/BatchWorkbench";
import { VerificationDashboard } from "@/components/VerificationDashboard";

const DOC_QUERY = "doc";

function readDocIdFromUrl(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  const value = new URLSearchParams(window.location.search).get(DOC_QUERY);
  return value?.trim() || null;
}

function writeDocIdToUrl(id: string | null): void {
  if (typeof window === "undefined") {
    return;
  }
  const url = new URL(window.location.href);
  if (id) {
    url.searchParams.set(DOC_QUERY, id);
  } else {
    url.searchParams.delete(DOC_QUERY);
  }
  window.history.pushState({}, "", url.pathname + url.search);
}

/**
 * HITL shell: BatchWorkbench queue ↔ VerificationDashboard detail.
 * Selection is mirrored in `?doc=` so refresh / deep-links work in Tauri + browser.
 */
export default function HomePage() {
  const [documentId, setDocumentId] = useState<string | null>(null);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setDocumentId(readDocIdFromUrl());
    setHydrated(true);

    const onPopState = () => setDocumentId(readDocIdFromUrl());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const openDocument = useCallback((id: string) => {
    writeDocIdToUrl(id);
    setDocumentId(id);
  }, []);

  const backToQueue = useCallback(() => {
    writeDocIdToUrl(null);
    setDocumentId(null);
  }, []);

  if (!hydrated) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted">
        Loading station…
      </div>
    );
  }

  if (documentId) {
    return (
      <VerificationDashboard
        documentId={documentId}
        onReleased={backToQueue}
        onBackToQueue={backToQueue}
      />
    );
  }

  return <BatchWorkbench onSelectDocument={openDocument} />;
}
