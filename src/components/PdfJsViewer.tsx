"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  RenderTask,
} from "pdfjs-dist";
import type { FieldBBox } from "@/lib/types";
import { isPdfFileName } from "@/lib/types";

export type PdfFieldHighlight = {
  key: string;
  bbox: FieldBBox;
};

interface PdfJsViewerProps {
  fileName: string | null;
  documentUrl: string | null;
  loading?: boolean;
  error?: string | null;
  /** All known field bounding boxes for overlay rendering. */
  highlights?: PdfFieldHighlight[];
  /** Field key currently focused in the form sidebar. */
  activeFieldKey?: string | null;
}

type PageMetrics = {
  pageNumber: number;
  width: number;
  height: number;
  /** CSS pixel width after scale. */
  cssWidth: number;
  /** CSS pixel height after scale. */
  cssHeight: number;
};

function resolveBoxRect(
  bbox: FieldBBox,
  pageWidth: number,
  pageHeight: number,
): { left: number; top: number; width: number; height: number } {
  const fractional =
    Math.max(Math.abs(bbox.x), Math.abs(bbox.y), Math.abs(bbox.w), Math.abs(bbox.h)) <=
    1.0001;

  if (fractional) {
    return {
      left: bbox.x * pageWidth,
      top: bbox.y * pageHeight,
      width: bbox.w * pageWidth,
      height: bbox.h * pageHeight,
    };
  }

  // Absolute PDF user-space units (origin top-left for vision extractors).
  return {
    left: bbox.x,
    top: bbox.y,
    width: bbox.w,
    height: bbox.h,
  };
}

async function ensurePdfjs() {
  const pdfjs = await import("pdfjs-dist");
  // Local worker shipped in /public — no CDN dependency (Tauri air-gap safe).
  pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
  return pdfjs;
}

export function PdfJsViewer({
  fileName,
  documentUrl,
  loading = false,
  error = null,
  highlights = [],
  activeFieldKey = null,
}: PdfJsViewerProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const canvasRefs = useRef<Map<number, HTMLCanvasElement>>(new Map());
  const renderTasks = useRef<Map<number, RenderTask>>(new Map());
  const pdfRef = useRef<PDFDocumentProxy | null>(null);

  const [pdfDoc, setPdfDoc] = useState<PDFDocumentProxy | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [pageMetrics, setPageMetrics] = useState<PageMetrics[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isRendering, setIsRendering] = useState(false);
  const [fitScale, setFitScale] = useState(1);
  const [userZoom, setUserZoom] = useState(1);
  const [containerWidth, setContainerWidth] = useState(0);

  const isPdf = isPdfFileName(fileName);
  const scale = fitScale * userZoom;

  // Observe pane width for zoom-to-fit.
  useEffect(() => {
    const node = scrollRef.current;
    if (!node) {
      return;
    }
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      setContainerWidth(width);
    });
    observer.observe(node);
    setContainerWidth(node.clientWidth);
    return () => observer.disconnect();
  }, [documentUrl, isPdf]);

  // Load PDF document.
  useEffect(() => {
    let cancelled = false;

    const clearPdf = () => {
      for (const task of renderTasks.current.values()) {
        task.cancel();
      }
      renderTasks.current.clear();
      if (pdfRef.current) {
        void pdfRef.current.cleanup();
        pdfRef.current = null;
      }
    };

    if (!documentUrl || !isPdf) {
      clearPdf();
      queueMicrotask(() => {
        if (cancelled) {
          return;
        }
        setPdfDoc(null);
        setPageCount(0);
        setPageMetrics([]);
        setLoadError(null);
        setIsRendering(false);
      });
      return () => {
        cancelled = true;
      };
    }

    clearPdf();
    let loadingTask: PDFDocumentLoadingTask | null = null;

    void (async () => {
      setIsRendering(true);
      setLoadError(null);
      setPdfDoc(null);
      setPageCount(0);
      setPageMetrics([]);
      try {
        const pdfjs = await ensurePdfjs();
        loadingTask = pdfjs.getDocument({ url: documentUrl });
        const doc = await loadingTask.promise;
        if (cancelled) {
          void doc.cleanup();
          return;
        }
        pdfRef.current = doc;
        setPdfDoc(doc);
        setPageCount(doc.numPages);
        setUserZoom(1);
      } catch (err) {
        if (!cancelled) {
          setLoadError(
            err instanceof Error ? err.message : "Failed to load PDF document.",
          );
          setIsRendering(false);
        }
      }
    })();

    return () => {
      cancelled = true;
      void loadingTask?.destroy();
      clearPdf();
    };
  }, [documentUrl, isPdf]);

  // Recompute fit scale when the first page / container width changes.
  useEffect(() => {
    if (!pdfDoc || containerWidth <= 0) {
      return;
    }
    let cancelled = false;
    void (async () => {
      const page = await pdfDoc.getPage(1);
      if (cancelled) {
        return;
      }
      const viewport = page.getViewport({ scale: 1 });
      const padding = 32;
      const nextFit = Math.max(0.25, (containerWidth - padding) / viewport.width);
      setFitScale(nextFit);
    })();
    return () => {
      cancelled = true;
    };
  }, [pdfDoc, containerWidth]);

  // Render all pages at the current scale.
  useEffect(() => {
    if (!pdfDoc || pageCount === 0 || scale <= 0) {
      return;
    }

    let cancelled = false;
    const tasks = renderTasks.current;

    void (async () => {
      setIsRendering(true);
      const metrics: PageMetrics[] = [];

      for (let pageNumber = 1; pageNumber <= pageCount; pageNumber += 1) {
        if (cancelled) {
          return;
        }
        const page = await pdfDoc.getPage(pageNumber);
        const viewport = page.getViewport({ scale });
        const canvas = canvasRefs.current.get(pageNumber);
        if (!canvas) {
          continue;
        }

        const context = canvas.getContext("2d");
        if (!context) {
          continue;
        }

        const previous = tasks.get(pageNumber);
        previous?.cancel();

        const outputScale = window.devicePixelRatio || 1;
        canvas.width = Math.floor(viewport.width * outputScale);
        canvas.height = Math.floor(viewport.height * outputScale);
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;
        context.setTransform(outputScale, 0, 0, outputScale, 0, 0);

        const task = page.render({
          canvasContext: context,
          viewport,
          canvas,
        });
        tasks.set(pageNumber, task);

        try {
          await task.promise;
        } catch (err) {
          if (
            cancelled ||
            (err instanceof Error && err.name === "RenderingCancelledException")
          ) {
            continue;
          }
          throw err;
        }

        metrics.push({
          pageNumber,
          width: viewport.width / scale,
          height: viewport.height / scale,
          cssWidth: viewport.width,
          cssHeight: viewport.height,
        });
      }

      if (!cancelled) {
        setPageMetrics(metrics);
        setIsRendering(false);
      }
    })();

    return () => {
      cancelled = true;
      for (const task of tasks.values()) {
        task.cancel();
      }
      tasks.clear();
    };
  }, [pdfDoc, pageCount, scale]);

  const activeHighlight = useMemo(
    () => highlights.find((item) => item.key === activeFieldKey) ?? null,
    [highlights, activeFieldKey],
  );

  // Scroll active bbox into view when focus changes.
  useEffect(() => {
    if (!activeHighlight || !scrollRef.current) {
      return;
    }
    const target = scrollRef.current.querySelector(
      `[data-bbox-key="${CSS.escape(activeHighlight.key)}"]`,
    );
    if (target instanceof HTMLElement) {
      target.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
    }
  }, [activeHighlight, pageMetrics]);

  const setCanvasRef = useCallback(
    (pageNumber: number) => (node: HTMLCanvasElement | null) => {
      if (node) {
        canvasRefs.current.set(pageNumber, node);
      } else {
        canvasRefs.current.delete(pageNumber);
      }
    },
    [],
  );

  const zoomToFit = () => setUserZoom(1);
  const zoomIn = () => setUserZoom((z) => Math.min(4, Number((z + 0.15).toFixed(2))));
  const zoomOut = () => setUserZoom((z) => Math.max(0.4, Number((z - 0.15).toFixed(2))));

  if (loading) {
    return (
      <ViewerShell>
        <CenteredStatus>
          <Spinner />
          <p className="text-sm">Loading document preview…</p>
        </CenteredStatus>
      </ViewerShell>
    );
  }

  if (error || loadError) {
    return (
      <ViewerShell>
        <div className="flex h-full items-center justify-center p-8">
          <div className="max-w-md rounded-xl border border-danger/30 bg-danger/5 p-6 text-center">
            <p className="text-sm font-medium text-danger">Preview unavailable</p>
            <p className="mt-2 text-xs text-muted">{error ?? loadError}</p>
          </div>
        </div>
      </ViewerShell>
    );
  }

  if (!documentUrl || !fileName) {
    return (
      <ViewerShell>
        <CenteredStatus>
          <p className="text-sm text-muted">No document attached to this queue item.</p>
        </CenteredStatus>
      </ViewerShell>
    );
  }

  // Non-PDF fallback — keep image preview with optional bbox overlay when present.
  if (!isPdf) {
    return (
      <ViewerShell
        fileName={fileName}
        badge="Image"
        toolbar={
          <ZoomControls
            zoomPercent={Math.round(userZoom * 100)}
            onFit={zoomToFit}
            onIn={zoomIn}
            onOut={zoomOut}
          />
        }
      >
        <div
          ref={scrollRef}
          className="relative min-h-0 flex-1 overflow-auto bg-[#08090c] p-4"
        >
          <div
            className="relative mx-auto inline-block"
            style={{ transform: `scale(${userZoom})`, transformOrigin: "top center" }}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={documentUrl}
              alt={`Document preview: ${fileName}`}
              className="max-w-full object-contain"
              id="docuvision-image-preview"
            />
            <ImageBBoxOverlay
              highlights={highlights}
              activeFieldKey={activeFieldKey}
            />
          </div>
        </div>
      </ViewerShell>
    );
  }

  return (
    <ViewerShell
      fileName={fileName}
      badge="PDF"
      toolbar={
        <ZoomControls
          zoomPercent={Math.round(userZoom * 100)}
          onFit={zoomToFit}
          onIn={zoomIn}
          onOut={zoomOut}
        />
      }
    >
      <div
        ref={scrollRef}
        className="relative min-h-0 flex-1 overflow-auto bg-[#08090c]"
      >
        {isRendering && pageMetrics.length === 0 && (
          <CenteredStatus>
            <Spinner />
            <p className="text-sm">Rendering pages…</p>
          </CenteredStatus>
        )}
        <div className="flex flex-col items-center gap-4 px-4 py-4">
          {Array.from({ length: pageCount }, (_, index) => {
            const pageNumber = index + 1;
            const metrics = pageMetrics.find((m) => m.pageNumber === pageNumber);
            const pageHighlights = highlights.filter(
              (h) => h.bbox.page === pageNumber,
            );

            return (
              <div
                key={pageNumber}
                className="relative shadow-lg shadow-black/40"
                data-page={pageNumber}
                style={
                  metrics
                    ? { width: metrics.cssWidth, height: metrics.cssHeight }
                    : { minHeight: 240, width: "100%", maxWidth: 720 }
                }
              >
                <canvas
                  ref={setCanvasRef(pageNumber)}
                  className="block bg-white"
                  aria-label={`Page ${pageNumber}`}
                />
                {metrics &&
                  pageHighlights.map((item) => {
                    const rect = resolveBoxRect(
                      item.bbox,
                      metrics.width,
                      metrics.height,
                    );
                    const active = item.key === activeFieldKey;
                    return (
                      <div
                        key={item.key}
                        data-bbox-key={item.key}
                        className={`pointer-events-none absolute rounded-sm border-2 transition-all duration-200 ${
                          active
                            ? "z-20 border-accent bg-accent/25 shadow-[0_0_0_2px_rgba(59,130,246,0.35)]"
                            : "z-10 border-success/70 bg-success/10"
                        }`}
                        style={{
                          left: rect.left * scale,
                          top: rect.top * scale,
                          width: Math.max(2, rect.width * scale),
                          height: Math.max(2, rect.height * scale),
                        }}
                      />
                    );
                  })}
                <span className="absolute bottom-2 right-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-medium text-white/80">
                  {pageNumber}/{pageCount}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </ViewerShell>
  );
}

function ImageBBoxOverlay({
  highlights,
  activeFieldKey,
}: {
  highlights: PdfFieldHighlight[];
  activeFieldKey: string | null;
}) {
  // Image previews treat bbox as fractions of natural image size via % positioning.
  return (
    <>
      {highlights.map((item) => {
        const { bbox } = item;
        const fractional =
          Math.max(
            Math.abs(bbox.x),
            Math.abs(bbox.y),
            Math.abs(bbox.w),
            Math.abs(bbox.h),
          ) <= 1.0001;
        if (!fractional) {
          return null;
        }
        const active = item.key === activeFieldKey;
        return (
          <div
            key={item.key}
            data-bbox-key={item.key}
            className={`pointer-events-none absolute rounded-sm border-2 transition-all duration-200 ${
              active
                ? "z-20 border-accent bg-accent/25"
                : "z-10 border-success/70 bg-success/10"
            }`}
            style={{
              left: `${bbox.x * 100}%`,
              top: `${bbox.y * 100}%`,
              width: `${bbox.w * 100}%`,
              height: `${bbox.h * 100}%`,
            }}
          />
        );
      })}
    </>
  );
}

function ViewerShell({
  children,
  fileName,
  badge,
  toolbar,
}: {
  children: ReactNode;
  fileName?: string;
  badge?: string;
  toolbar?: ReactNode;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      {(fileName || toolbar) && (
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            {fileName && (
              <p className="truncate font-mono text-xs text-muted">{fileName}</p>
            )}
            {badge && (
              <span className="shrink-0 rounded-md bg-surface-elevated px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted">
                {badge}
              </span>
            )}
          </div>
          {toolbar}
        </div>
      )}
      <div className="relative min-h-0 flex-1 overflow-hidden">{children}</div>
    </div>
  );
}

function ZoomControls({
  zoomPercent,
  onFit,
  onIn,
  onOut,
}: {
  zoomPercent: number;
  onFit: () => void;
  onIn: () => void;
  onOut: () => void;
}) {
  return (
    <div className="flex shrink-0 items-center gap-1">
      <button
        type="button"
        onClick={onOut}
        className="rounded border border-border px-2 py-0.5 text-xs text-muted transition hover:bg-surface-elevated hover:text-foreground"
        aria-label="Zoom out"
      >
        −
      </button>
      <button
        type="button"
        onClick={onFit}
        className="min-w-[3.5rem] rounded border border-border px-2 py-0.5 text-[11px] tabular-nums text-muted transition hover:bg-surface-elevated hover:text-foreground"
        title="Zoom to fit"
      >
        {zoomPercent}%
      </button>
      <button
        type="button"
        onClick={onIn}
        className="rounded border border-border px-2 py-0.5 text-xs text-muted transition hover:bg-surface-elevated hover:text-foreground"
        aria-label="Zoom in"
      >
        +
      </button>
      <button
        type="button"
        onClick={onFit}
        className="ml-1 rounded border border-border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted transition hover:bg-surface-elevated hover:text-foreground"
      >
        Fit
      </button>
    </div>
  );
}

function CenteredStatus({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 text-muted">
      {children}
    </div>
  );
}

function Spinner() {
  return (
    <div className="h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
  );
}
