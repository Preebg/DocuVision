"use client";

import {
  memo,
  useCallback,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  buildSplitPayload,
  calculatePageRange,
  moveItem,
  type DocumentPage,
  type OriginalDocument,
  type PendingDocument,
  type SplitSubmissionPayload,
} from "@/lib/document-pages";

export interface DocumentSplitStitchProps {
  initialDocument: OriginalDocument;
  onSubmit?: (payload: SplitSubmissionPayload) => Promise<void> | void;
}

type SubmitState = "idle" | "submitting" | "submitted" | "error";

let pendingId = 0;

function createPendingId(): string {
  pendingId += 1;
  return `pending-${Date.now()}-${pendingId}`;
}

function pageSelectionKey(documentId: string, pageId: string): string {
  return `${documentId}:${pageId}`;
}

function documentLabel(index: number): string {
  return index < 26 ? String.fromCharCode(65 + index) : `${index + 1}`;
}

function cloneDocument(document: OriginalDocument): OriginalDocument {
  return { ...document, pages: document.pages.map((page) => ({ ...page })) };
}

/** Mock integration point; replace this body with the extraction API request. */
export async function handleSubmitSplits(
  payload: SplitSubmissionPayload,
): Promise<void> {
  await Promise.resolve();
  console.info("Submitting split plan", payload);
}

function Icon({
  children,
  className = "h-4 w-4",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
    >
      {children}
    </svg>
  );
}

const icons = {
  extract: (
    <Icon>
      <path d="M4 12h11M11 7l5 5-5 5" />
      <path d="M19 5v14" />
    </Icon>
  ),
  merge: (
    <Icon>
      <path d="M5 5v3c0 2.2 1.8 4 4 4h10" />
      <path d="M5 19v-3c0-2.2 1.8-4 4-4" />
      <path d="m16 9 3 3-3 3" />
    </Icon>
  ),
  break: (
    <Icon>
      <path d="M4 7h5M15 7h5M4 17h5M15 17h5" />
      <path d="m11 4 2 3-2 3 2 3-2 3 2 4" />
    </Icon>
  ),
  omit: (
    <Icon>
      <path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13" />
      <path d="M10 11v5M14 11v5" />
    </Icon>
  ),
  reset: (
    <Icon>
      <path d="M4 4v6h6" />
      <path d="M5.5 15a7 7 0 1 0 .8-7.8L4 10" />
    </Icon>
  ),
  up: (
    <Icon className="h-3.5 w-3.5">
      <path d="m7 14 5-5 5 5" />
    </Icon>
  ),
  down: (
    <Icon className="h-3.5 w-3.5">
      <path d="m7 10 5 5 5-5" />
    </Icon>
  ),
  grip: (
    <Icon className="h-3.5 w-3.5">
      <circle cx="9" cy="7" r=".7" fill="currentColor" />
      <circle cx="15" cy="7" r=".7" fill="currentColor" />
      <circle cx="9" cy="12" r=".7" fill="currentColor" />
      <circle cx="15" cy="12" r=".7" fill="currentColor" />
      <circle cx="9" cy="17" r=".7" fill="currentColor" />
      <circle cx="15" cy="17" r=".7" fill="currentColor" />
    </Icon>
  ),
};

interface SourcePageCardProps {
  page: DocumentPage;
  selected: boolean;
  assigned: boolean;
  omitted: boolean;
  onToggle: (pageNumber: number) => void;
}

const SourcePageCard = memo(function SourcePageCard({
  page,
  selected,
  assigned,
  omitted,
  onToggle,
}: SourcePageCardProps) {
  const disabled = assigned || omitted;
  return (
    <button
      type="button"
      onClick={() => onToggle(page.pageNumber)}
      disabled={disabled}
      aria-pressed={selected}
      aria-label={`Page ${page.pageNumber}${assigned ? ", assigned" : ""}${omitted ? ", omitted" : ""}`}
      className={`group relative aspect-[0.72] overflow-hidden rounded border text-left transition ${
        selected
          ? "border-accent bg-accent/10 ring-1 ring-accent"
          : "border-border bg-[#e6e7ea] hover:border-accent/70"
      } ${disabled ? "cursor-not-allowed opacity-45" : "cursor-pointer"}`}
    >
      {page.thumbnailUrl ? (
        <span
          className="absolute inset-0 bg-cover bg-center"
          style={{ backgroundImage: `url("${page.thumbnailUrl}")` }}
        />
      ) : (
        <span className="absolute inset-2 space-y-1.5 pt-2 opacity-45">
          <span className="block h-1 w-4/5 rounded bg-slate-500" />
          <span className="block h-1 w-full rounded bg-slate-400" />
          <span className="block h-1 w-3/4 rounded bg-slate-400" />
          <span className="mt-3 block h-7 rounded border border-slate-400 bg-slate-300" />
          <span className="block h-1 w-full rounded bg-slate-400" />
          <span className="block h-1 w-5/6 rounded bg-slate-400" />
        </span>
      )}
      <span
        className={`absolute left-1.5 top-1.5 grid h-4 w-4 place-items-center rounded border ${
          selected
            ? "border-accent bg-accent text-white"
            : "border-slate-500 bg-black/35 text-transparent group-hover:text-white"
        }`}
      >
        <span className="text-[10px] leading-none">✓</span>
      </span>
      <span className="absolute inset-x-0 bottom-0 flex items-center justify-between bg-black/75 px-1.5 py-1 font-mono text-[9px] text-white">
        <span>PAGE {page.pageNumber}</span>
        <span className={omitted ? "text-danger" : "text-slate-300"}>
          {omitted ? "OMIT" : assigned ? "USED" : ""}
        </span>
      </span>
    </button>
  );
});

function ActionButton({
  icon,
  children,
  disabled,
  danger = false,
  onClick,
}: {
  icon: ReactNode;
  children: ReactNode;
  disabled?: boolean;
  danger?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left text-[11px] font-medium transition disabled:cursor-not-allowed disabled:opacity-35 ${
        danger
          ? "border-danger/30 bg-danger/5 text-danger hover:bg-danger/10"
          : "border-border bg-surface-elevated text-foreground hover:border-accent/50 hover:bg-accent/10"
      }`}
    >
      <span className="shrink-0">{icon}</span>
      <span>{children}</span>
    </button>
  );
}

export function DocumentSplitStitch(props: DocumentSplitStitchProps) {
  const documentVersion = `${props.initialDocument.id}:${props.initialDocument.pages
    .map((page) => page.id)
    .join("|")}`;
  return <DocumentSplitStitchWorkbench key={documentVersion} {...props} />;
}

function DocumentSplitStitchWorkbench({
  initialDocument,
  onSubmit = handleSubmitSplits,
}: DocumentSplitStitchProps) {
  const [originalDocument, setOriginalDocument] = useState(() =>
    cloneDocument(initialDocument),
  );
  const [pendingDocuments, setPendingDocuments] = useState<PendingDocument[]>(
    [],
  );
  const [selectedSourcePages, setSelectedSourcePages] = useState<Set<number>>(
    () => new Set(),
  );
  const [selectedPendingPages, setSelectedPendingPages] = useState<Set<string>>(
    () => new Set(),
  );
  const [selectedPendingDocuments, setSelectedPendingDocuments] = useState<
    Set<string>
  >(() => new Set());
  const [omittedPages, setOmittedPages] = useState<Set<number>>(
    () => new Set(),
  );
  const [submitState, setSubmitState] = useState<SubmitState>("idle");

  const assignedPages = useMemo(
    () =>
      new Set(
        pendingDocuments.flatMap((document) =>
          document.pages.map((page) => page.pageNumber),
        ),
      ),
    [pendingDocuments],
  );

  const toggleSourcePage = useCallback((pageNumber: number) => {
    setSelectedSourcePages((current) => {
      const next = new Set(current);
      if (next.has(pageNumber)) {
        next.delete(pageNumber);
      } else {
        next.add(pageNumber);
      }
      return next;
    });
  }, []);

  const togglePendingPage = useCallback((key: string) => {
    setSelectedPendingPages((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }, []);

  const togglePendingDocument = useCallback((documentId: string) => {
    setSelectedPendingDocuments((current) => {
      const next = new Set(current);
      if (next.has(documentId)) {
        next.delete(documentId);
      } else {
        next.add(documentId);
      }
      return next;
    });
  }, []);

  const extractSelected = useCallback(() => {
    const pages = originalDocument.pages.filter(
      (page) =>
        selectedSourcePages.has(page.pageNumber) &&
        !assignedPages.has(page.pageNumber) &&
        !omittedPages.has(page.pageNumber),
    );
    if (pages.length === 0) {
      return;
    }
    setPendingDocuments((current) => [
      ...current,
      {
        id: createPendingId(),
        name: `Document ${documentLabel(current.length)}`,
        pages,
      },
    ]);
    setSelectedSourcePages(new Set());
    setSubmitState("idle");
  }, [assignedPages, omittedPages, originalDocument.pages, selectedSourcePages]);

  const mergeSelected = useCallback(() => {
    setPendingDocuments((current) => {
      if (selectedPendingDocuments.size >= 2) {
        const selected = current.filter((document) =>
          selectedPendingDocuments.has(document.id),
        );
        const insertionIndex = current.findIndex((document) =>
          selectedPendingDocuments.has(document.id),
        );
        const remaining = current.filter(
          (document) => !selectedPendingDocuments.has(document.id),
        );
        const merged: PendingDocument = {
          id: createPendingId(),
          name: `Stitched ${documentLabel(insertionIndex)}`,
          pages: selected.flatMap((document) => document.pages),
        };
        remaining.splice(insertionIndex, 0, merged);
        return remaining;
      }

      if (selectedPendingPages.size >= 2) {
        const pages = current.flatMap((document) =>
          document.pages.filter((page) =>
            selectedPendingPages.has(
              pageSelectionKey(document.id, page.id),
            ),
          ),
        );
        const remaining = current
          .map((document) => ({
            ...document,
            pages: document.pages.filter(
              (page) =>
                !selectedPendingPages.has(
                  pageSelectionKey(document.id, page.id),
                ),
            ),
          }))
          .filter((document) => document.pages.length > 0);
        return [
          ...remaining,
          {
            id: createPendingId(),
            name: `Stitched ${documentLabel(remaining.length)}`,
            pages,
          },
        ];
      }
      return current;
    });
    setSelectedPendingDocuments(new Set());
    setSelectedPendingPages(new Set());
    setSubmitState("idle");
  }, [selectedPendingDocuments, selectedPendingPages]);

  const insertBatchBreak = useCallback(() => {
    if (selectedPendingPages.size !== 1) {
      return;
    }
    const [selectedKey] = selectedPendingPages;
    setPendingDocuments((current) => {
      const documentIndex = current.findIndex((document) =>
        document.pages.some(
          (page) => pageSelectionKey(document.id, page.id) === selectedKey,
        ),
      );
      if (documentIndex < 0) {
        return current;
      }
      const document = current[documentIndex];
      const pageIndex = document.pages.findIndex(
        (page) => pageSelectionKey(document.id, page.id) === selectedKey,
      );
      if (pageIndex < 0 || pageIndex === document.pages.length - 1) {
        return current;
      }
      const next = [...current];
      next.splice(
        documentIndex,
        1,
        { ...document, pages: document.pages.slice(0, pageIndex + 1) },
        {
          id: createPendingId(),
          name: `${document.name} · B`,
          pages: document.pages.slice(pageIndex + 1),
        },
      );
      return next;
    });
    setSelectedPendingPages(new Set());
    setSubmitState("idle");
  }, [selectedPendingPages]);

  const omitSelected = useCallback(() => {
    const pageNumbers = new Set(selectedSourcePages);
    for (const document of pendingDocuments) {
      for (const page of document.pages) {
        if (
          selectedPendingPages.has(pageSelectionKey(document.id, page.id))
        ) {
          pageNumbers.add(page.pageNumber);
        }
      }
    }
    if (pageNumbers.size === 0) {
      return;
    }
    setOmittedPages((current) => new Set([...current, ...pageNumbers]));
    setPendingDocuments((current) =>
      current
        .map((document) => ({
          ...document,
          pages: document.pages.filter(
            (page) => !pageNumbers.has(page.pageNumber),
          ),
        }))
        .filter((document) => document.pages.length > 0),
    );
    setSelectedSourcePages(new Set());
    setSelectedPendingPages(new Set());
    setSelectedPendingDocuments(new Set());
    setSubmitState("idle");
  }, [pendingDocuments, selectedPendingPages, selectedSourcePages]);

  const movePage = useCallback(
    (documentId: string, pageIndex: number, direction: -1 | 1) => {
      setPendingDocuments((current) =>
        current.map((document) =>
          document.id === documentId
            ? {
                ...document,
                pages: moveItem(
                  document.pages,
                  pageIndex,
                  pageIndex + direction,
                ),
              }
            : document,
        ),
      );
      setSubmitState("idle");
    },
    [],
  );

  const reset = useCallback(() => {
    setOriginalDocument(cloneDocument(initialDocument));
    setPendingDocuments([]);
    setSelectedSourcePages(new Set());
    setSelectedPendingPages(new Set());
    setSelectedPendingDocuments(new Set());
    setOmittedPages(new Set());
    setSubmitState("idle");
  }, [initialDocument]);

  const submit = useCallback(async () => {
    if (pendingDocuments.length === 0) {
      return;
    }
    setSubmitState("submitting");
    try {
      await onSubmit(buildSplitPayload(originalDocument.id, pendingDocuments));
      setSubmitState("submitted");
    } catch {
      setSubmitState("error");
    }
  }, [onSubmit, originalDocument.id, pendingDocuments]);

  const hasOmitSelection =
    selectedSourcePages.size > 0 || selectedPendingPages.size > 0;
  const canMerge =
    selectedPendingDocuments.size >= 2 || selectedPendingPages.size >= 2;

  return (
    <section className="flex h-full min-h-[560px] flex-col overflow-hidden rounded-lg border border-border bg-background text-foreground">
      <header className="flex shrink-0 items-center justify-between border-b border-border bg-surface px-4 py-2.5">
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold">
            Document Split / Stitch
          </h1>
          <p className="truncate font-mono text-[10px] text-muted">
            {originalDocument.name} · {originalDocument.pages.length} pages ·{" "}
            {omittedPages.size} omitted
          </p>
        </div>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={
            pendingDocuments.length === 0 || submitState === "submitting"
          }
          className="rounded-md bg-accent px-3 py-1.5 text-[11px] font-semibold text-white transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitState === "submitting"
            ? "Submitting…"
            : submitState === "submitted"
              ? "Plan Submitted"
              : "Submit Split Plan"}
        </button>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(300px,1fr)_180px_minmax(360px,1.2fr)]">
        <div className="flex min-w-0 flex-col border-r border-border">
          <div className="flex h-9 shrink-0 items-center justify-between border-b border-border bg-surface px-3">
            <span className="text-[10px] font-semibold uppercase tracking-[0.13em] text-muted">
              Source document
            </span>
            <span className="font-mono text-[9px] text-muted">
              {selectedSourcePages.size} selected
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2.5">
            <div className="grid grid-cols-[repeat(auto-fill,minmax(76px,1fr))] gap-2">
              {originalDocument.pages.map((page) => (
                <SourcePageCard
                  key={page.id}
                  page={page}
                  selected={selectedSourcePages.has(page.pageNumber)}
                  assigned={assignedPages.has(page.pageNumber)}
                  omitted={omittedPages.has(page.pageNumber)}
                  onToggle={toggleSourcePage}
                />
              ))}
            </div>
          </div>
        </div>

        <aside className="flex flex-col border-r border-border bg-surface/60 p-2.5">
          <p className="mb-2 text-[9px] font-semibold uppercase tracking-[0.14em] text-muted">
            Operations
          </p>
          <div className="space-y-1.5">
            <ActionButton
              icon={icons.extract}
              onClick={extractSelected}
              disabled={selectedSourcePages.size === 0}
            >
              Extract Selected to New Doc
            </ActionButton>
            <ActionButton
              icon={icons.merge}
              onClick={mergeSelected}
              disabled={!canMerge}
            >
              Merge Selected
            </ActionButton>
            <ActionButton
              icon={icons.break}
              onClick={insertBatchBreak}
              disabled={selectedPendingPages.size !== 1}
            >
              Insert Batch Break
            </ActionButton>
            <ActionButton
              icon={icons.omit}
              onClick={omitSelected}
              disabled={!hasOmitSelection}
              danger
            >
              Delete / Omit Pages
            </ActionButton>
          </div>
          <div className="mt-auto border-t border-border pt-2">
            <ActionButton icon={icons.reset} onClick={reset}>
              Reset
            </ActionButton>
            <p className="mt-2 px-1 text-[9px] leading-4 text-muted">
              Select two documents or two target pages to stitch. Select one
              target page to break after it.
            </p>
          </div>
        </aside>

        <div className="flex min-w-0 flex-col">
          <div className="flex h-9 shrink-0 items-center justify-between border-b border-border bg-surface px-3">
            <span className="text-[10px] font-semibold uppercase tracking-[0.13em] text-muted">
              Target batches
            </span>
            <span className="font-mono text-[9px] text-muted">
              {pendingDocuments.length} documents
            </span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2.5">
            {pendingDocuments.length === 0 ? (
              <div className="grid h-full min-h-48 place-items-center rounded-md border border-dashed border-border bg-surface/30 p-8 text-center">
                <div>
                  <div className="mx-auto mb-2 grid h-8 w-8 place-items-center rounded border border-border text-muted">
                    {icons.extract}
                  </div>
                  <p className="text-xs text-foreground">
                    No target documents
                  </p>
                  <p className="mt-1 max-w-56 text-[10px] leading-4 text-muted">
                    Select source pages, then extract them into a new document.
                  </p>
                </div>
              </div>
            ) : (
              <div className="space-y-2">
                {pendingDocuments.map((document, documentIndex) => {
                  const documentSelected = selectedPendingDocuments.has(
                    document.id,
                  );
                  return (
                    <article
                      key={document.id}
                      className={`overflow-hidden rounded-md border ${
                        documentSelected
                          ? "border-accent ring-1 ring-accent/50"
                          : "border-border"
                      } bg-surface`}
                    >
                      <div className="flex items-center gap-2 border-b border-border bg-surface-elevated px-2.5 py-2">
                        <button
                          type="button"
                          onClick={() =>
                            togglePendingDocument(document.id)
                          }
                          aria-pressed={documentSelected}
                          aria-label={`Select ${document.name}`}
                          className={`grid h-4 w-4 shrink-0 place-items-center rounded border text-[10px] ${
                            documentSelected
                              ? "border-accent bg-accent text-white"
                              : "border-muted text-transparent hover:text-muted"
                          }`}
                        >
                          ✓
                        </button>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-[11px] font-semibold">
                            Doc {documentLabel(documentIndex)}
                            <span className="ml-1.5 font-normal text-muted">
                              {document.pages.length} pages
                            </span>
                          </p>
                          <p className="truncate font-mono text-[9px] text-muted">
                            Pages {calculatePageRange(document.pages)}
                          </p>
                        </div>
                      </div>
                      <ol className="divide-y divide-border">
                        {document.pages.map((page, pageIndex) => {
                          const key = pageSelectionKey(document.id, page.id);
                          const pageSelected =
                            selectedPendingPages.has(key);
                          return (
                            <li
                              key={page.id}
                              className={`flex h-8 items-center gap-2 px-2 ${
                                pageSelected ? "bg-accent/10" : ""
                              }`}
                            >
                              <span className="text-muted">{icons.grip}</span>
                              <button
                                type="button"
                                onClick={() => togglePendingPage(key)}
                                aria-pressed={pageSelected}
                                className="flex min-w-0 flex-1 items-center gap-2 text-left"
                              >
                                <span
                                  className={`grid h-3.5 w-3.5 place-items-center rounded-sm border text-[9px] ${
                                    pageSelected
                                      ? "border-accent bg-accent text-white"
                                      : "border-muted text-transparent"
                                  }`}
                                >
                                  ✓
                                </span>
                                <span className="font-mono text-[10px]">
                                  Page {page.pageNumber}
                                </span>
                                <span className="truncate text-[9px] text-muted">
                                  {page.id}
                                </span>
                              </button>
                              <div className="flex shrink-0 items-center">
                                <button
                                  type="button"
                                  onClick={() =>
                                    movePage(document.id, pageIndex, -1)
                                  }
                                  disabled={pageIndex === 0}
                                  aria-label={`Move page ${page.pageNumber} up`}
                                  className="grid h-6 w-6 place-items-center rounded text-muted hover:bg-background hover:text-foreground disabled:opacity-20"
                                >
                                  {icons.up}
                                </button>
                                <button
                                  type="button"
                                  onClick={() =>
                                    movePage(document.id, pageIndex, 1)
                                  }
                                  disabled={
                                    pageIndex === document.pages.length - 1
                                  }
                                  aria-label={`Move page ${page.pageNumber} down`}
                                  className="grid h-6 w-6 place-items-center rounded text-muted hover:bg-background hover:text-foreground disabled:opacity-20"
                                >
                                  {icons.down}
                                </button>
                              </div>
                            </li>
                          );
                        })}
                      </ol>
                    </article>
                  );
                })}
              </div>
            )}
          </div>
          {submitState === "error" && (
            <p className="border-t border-danger/30 bg-danger/5 px-3 py-2 text-[10px] text-danger">
              The split plan could not be submitted. Try again.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
