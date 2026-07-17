"use client";

import { useCallback, useEffect, useState } from "react";

export type ScannerDriver = "twain" | "isis";
export type ScannerPixelType = "blackAndWhite" | "grayscale" | "rgb";
export type ScannerErrorKind =
  | "disconnected"
  | "paperJam"
  | "missingDriver"
  | "busy"
  | "unsupported"
  | "unknown";

export type ScanConfig = {
  scannerName: string;
  driver?: ScannerDriver;
  dpi?: number;
  duplex?: boolean;
  pixelType?: ScannerPixelType;
  maxPages?: number;
};

export type ScanPage = {
  index: number;
  mimeType: "image/png";
  absolutePath: string;
  fileUrl: string;
  assetUrl: string;
  byteLength: number;
};

export type ScanResult = {
  scannerName: string;
  pages: ScanPage[];
};

export type ScannerError = {
  kind: ScannerErrorKind;
  message: string;
};

type NativeScanPage = Omit<ScanPage, "assetUrl">;
type NativeScanResult = Omit<ScanResult, "pages"> & {
  pages: NativeScanPage[];
};

type PageAcquiredEvent = {
  pageIndex: number;
};

type ScanErrorEvent = {
  message: string;
};

function isTauriRuntime(): boolean {
  return (
    typeof window !== "undefined" &&
    ("__TAURI_INTERNALS__" in window || "__TAURI__" in window)
  );
}

function scannerError(error: unknown): ScannerError {
  const message =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : "The scanner operation failed.";
  const normalized = message.toLowerCase();
  let kind: ScannerErrorKind = "unknown";
  if (normalized.includes("jam") || normalized.includes("paper")) {
    kind = "paperJam";
  } else if (
    normalized.includes("no longer available") ||
    normalized.includes("disconnected") ||
    normalized.includes("offline")
  ) {
    kind = "disconnected";
  } else if (
    normalized.includes("capability unsupported") ||
    normalized.includes("unsupported capability") ||
    normalized.includes("only for windows")
  ) {
    kind = "unsupported";
  } else if (
    normalized.includes("driver") ||
    normalized.includes("data source manager") ||
    normalized.includes("dsm") ||
    normalized.includes("licensed vendor isis")
  ) {
    kind = "missingDriver";
  } else if (normalized.includes("already in progress") || normalized.includes("busy")) {
    kind = "busy";
  } else if (normalized.includes("unsupported")) {
    kind = "unsupported";
  }
  return { kind, message };
}

/**
 * Owns native scanner discovery, acquisition state, and Tauri progress events.
 *
 * Pages are returned as scoped asset-protocol URLs while the absolute path and
 * file URL remain available for upload/native handoff.
 */
export function useScannerIntegration() {
  const [scannerList, setScannerList] = useState<string[]>([]);
  const [selectedScanner, setSelectedScanner] = useState<string | null>(null);
  const [isScanning, setIsScanning] = useState(false);
  const [currentPageCount, setCurrentPageCount] = useState(0);
  const [lastScan, setLastScan] = useState<ScanResult | null>(null);
  const [error, setError] = useState<ScannerError | null>(null);
  const isAvailable = isTauriRuntime();

  const refreshScanners = useCallback(async (): Promise<string[]> => {
    if (!isTauriRuntime()) {
      const unavailable = scannerError(
        "Native scanner integration is available only in the DocuVision desktop app.",
      );
      setError(unavailable);
      setScannerList([]);
      return [];
    }
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const scanners = await invoke<string[]>("list_scanners");
      setScannerList(scanners);
      setSelectedScanner((current) =>
        current && scanners.includes(current) ? current : scanners[0] ?? null,
      );
      setError(null);
      return scanners;
    } catch (cause) {
      setScannerList([]);
      setSelectedScanner(null);
      setError(scannerError(cause));
      return [];
    }
  }, []);

  const acquireScan = useCallback(
    async (options: Omit<ScanConfig, "scannerName"> = {}): Promise<ScanResult> => {
      if (!selectedScanner) {
        const missing = scannerError("Select a connected scanner before starting.");
        setError(missing);
        throw new Error(missing.message);
      }
      if (!isTauriRuntime()) {
        const unavailable = scannerError(
          "Native scanner integration is available only in the DocuVision desktop app.",
        );
        setError(unavailable);
        throw new Error(unavailable.message);
      }

      setIsScanning(true);
      setCurrentPageCount(0);
      setLastScan(null);
      setError(null);
      try {
        const { convertFileSrc, invoke } = await import("@tauri-apps/api/core");
        const nativeResult = await invoke<NativeScanResult>("acquire_scan", {
          config: {
            scannerName: selectedScanner,
            driver: options.driver ?? "twain",
            dpi: options.dpi ?? 300,
            duplex: options.duplex ?? false,
            pixelType: options.pixelType ?? "grayscale",
            maxPages: options.maxPages ?? 100,
          },
        });
        const result: ScanResult = {
          ...nativeResult,
          pages: nativeResult.pages.map((page) => ({
            ...page,
            assetUrl: convertFileSrc(page.absolutePath),
          })),
        };
        setCurrentPageCount(result.pages.length);
        setLastScan(result);
        return result;
      } catch (cause) {
        const failure = scannerError(cause);
        setError(failure);
        throw new Error(failure.message, { cause });
      } finally {
        setIsScanning(false);
      }
    },
    [selectedScanner],
  );

  useEffect(() => {
    if (!isAvailable) {
      return;
    }
    let disposed = false;
    const unlisteners: Array<() => void> = [];

    void import("@tauri-apps/api/event").then(async ({ listen }) => {
      const pageUnlisten = await listen<PageAcquiredEvent>(
        "scan:page-acquired",
        ({ payload }) => {
          if (!disposed) {
            setCurrentPageCount(payload.pageIndex);
          }
        },
      );
      if (disposed) {
        pageUnlisten();
        return;
      }
      unlisteners.push(pageUnlisten);

      const errorUnlisten = await listen<ScanErrorEvent>("scan:error", ({ payload }) => {
        if (!disposed) {
          setError(scannerError(payload.message));
          setIsScanning(false);
        }
      });
      if (disposed) {
        errorUnlisten();
      } else {
        unlisteners.push(errorUnlisten);
      }
    });

    const refreshTimer = window.setTimeout(() => {
      void refreshScanners();
    }, 0);
    return () => {
      disposed = true;
      window.clearTimeout(refreshTimer);
      unlisteners.forEach((unlisten) => unlisten());
    };
  }, [isAvailable, refreshScanners]);

  return {
    isAvailable,
    isScanning,
    currentPageCount,
    scannerList,
    selectedScanner,
    setSelectedScanner,
    refreshScanners,
    acquireScan,
    lastScan,
    error,
    clearError: () => setError(null),
  };
}
