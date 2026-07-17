"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import { REMOTE_PIPELINE_STAGE_ORDER } from "@/lib/remote-pipeline-types";
import type {
  RemotePipelineStatusResponse,
  RemotePipelineStatusSnapshot,
} from "@/lib/remote-pipeline-types";

const ACTIVE_BATCHES_PATH = "/api/v1/ingestion/batches/active";
const REDRIVE_PATH = "/api/v1/quarantine/redrive";
const REMOTE_STAGE_KEYS = new Set<string>(REMOTE_PIPELINE_STAGE_ORDER);
const REMOTE_STAGE_STATES = new Set([
  "pending",
  "queued",
  "running",
  "complete",
  "failed",
  "skipped",
]);

export interface UseRemotePipelineStatusOptions {
  enabled?: boolean;
  pollIntervalMs?: number;
  requestTimeoutMs?: number;
  maxBackoffMs?: number;
}

export interface UseRemotePipelineStatusResult
  extends RemotePipelineStatusSnapshot {
  refresh: () => void;
  redriveBatch: (batchId: string) => Promise<void>;
}

function isRemotePipelineStatusResponse(
  value: unknown,
): value is RemotePipelineStatusResponse {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<RemotePipelineStatusResponse>;
  const cluster = candidate.cluster as
    | Partial<RemotePipelineStatusResponse["cluster"]>
    | undefined;
  return (
    typeof candidate.generated_at === "string" &&
    Boolean(cluster && typeof cluster === "object") &&
    typeof cluster?.active_worker_nodes === "number" &&
    typeof cluster.total_worker_nodes === "number" &&
    typeof cluster.vram_used_gb === "number" &&
    typeof cluster.vram_total_gb === "number" &&
    typeof cluster.ram_used_gb === "number" &&
    typeof cluster.ram_total_gb === "number" &&
    Array.isArray(cluster.workers) &&
    Array.isArray(candidate.batches) &&
    candidate.batches.every(
      (batch) =>
        typeof batch.id === "string" &&
        typeof batch.display_name === "string" &&
        typeof batch.progress_percent === "number" &&
        Array.isArray(batch.assigned_worker_node_ids) &&
        Array.isArray(batch.stages) &&
        batch.stages.every(
          (stage) =>
            REMOTE_STAGE_KEYS.has(stage.key) &&
            REMOTE_STAGE_STATES.has(stage.state),
        ) &&
        Array.isArray(batch.gc_events) &&
        batch.gc_events.every((event) => REMOTE_STAGE_KEYS.has(event.stage)),
    )
  );
}

async function responseError(
  response: Response,
  fallback: string,
): Promise<Error> {
  const detail = await response.text();
  if (!detail) {
    return new Error(fallback);
  }
  try {
    const parsed = JSON.parse(detail) as { detail?: unknown };
    if (typeof parsed.detail === "string") {
      return new Error(parsed.detail);
    }
  } catch {
    // Plain-text errors are useful as-is.
  }
  return new Error(detail);
}

/**
 * Polls remote cluster telemetry without overlapping requests. Last-known-good
 * data remains visible through transient failures while the connection state
 * and staleness flags make the degraded state explicit.
 */
export function useRemotePipelineStatus(
  options: UseRemotePipelineStatusOptions = {},
): UseRemotePipelineStatusResult {
  const {
    enabled = true,
    pollIntervalMs = 3_000,
    requestTimeoutMs = 8_000,
    maxBackoffMs = 30_000,
  } = options;

  const [data, setData] = useState<RemotePipelineStatusResponse | null>(null);
  const [connectionState, setConnectionState] =
    useState<RemotePipelineStatusSnapshot["connectionState"]>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [latencyMs, setLatencyMs] = useState<number | null>(null);
  const [lastSuccessfulPollAt, setLastSuccessfulPollAt] =
    useState<Date | null>(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [isLoading, setIsLoading] = useState(enabled);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isStale, setIsStale] = useState(false);
  const [refreshGeneration, setRefreshGeneration] = useState(0);
  const hasDataRef = useRef(false);

  const refresh = useCallback(() => {
    setRefreshGeneration((generation) => generation + 1);
  }, []);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let activeController: AbortController | null = null;
    let failures = 0;
    let inFlight = false;

    const schedule = (delayMs: number) => {
      if (cancelled) {
        return;
      }
      timer = setTimeout(() => {
        void poll();
      }, delayMs);
    };

    const markFailure = (message: string) => {
      failures += 1;
      setConsecutiveFailures(failures);
      setError(message);
      setIsStale(true);
      setConnectionState(hasDataRef.current ? "degraded" : "offline");
    };

    const poll = async () => {
      if (cancelled) {
        return;
      }
      if (typeof navigator !== "undefined" && !navigator.onLine) {
        markFailure("This station is offline. Waiting for network connectivity.");
        setIsLoading(false);
        setIsRefreshing(false);
        schedule(Math.min(maxBackoffMs, pollIntervalMs * 2 ** failures));
        return;
      }

      const controller = new AbortController();
      activeController = controller;
      inFlight = true;
      let timedOut = false;
      const timeout = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, requestTimeoutMs);
      const startedAt = performance.now();
      setIsRefreshing(hasDataRef.current);

      try {
        const response = await apiFetch(ACTIVE_BATCHES_PATH, {
          method: "GET",
          cache: "no-store",
          signal: controller.signal,
        });
        const measuredLatency = Math.round(performance.now() - startedAt);
        if (!response.ok) {
          throw await responseError(
            response,
            `Remote telemetry request failed (${response.status}).`,
          );
        }
        const payload: unknown = await response.json();
        if (!isRemotePipelineStatusResponse(payload)) {
          throw new Error("Remote telemetry response did not match its contract.");
        }
        if (cancelled) {
          return;
        }

        failures = 0;
        hasDataRef.current = true;
        setData(payload);
        setLatencyMs(measuredLatency);
        setLastSuccessfulPollAt(new Date());
        setConsecutiveFailures(0);
        setError(null);
        setIsStale(false);
        setConnectionState(
          payload.cluster.status === "offline" ? "degraded" : "online",
        );
        schedule(
          document.visibilityState === "hidden"
            ? Math.max(15_000, pollIntervalMs * 4)
            : pollIntervalMs,
        );
      } catch (caught) {
        if (cancelled) {
          return;
        }
        const message = timedOut
          ? `Remote telemetry timed out after ${requestTimeoutMs}ms.`
          : caught instanceof Error
            ? caught.message
            : "Remote telemetry connection failed.";
        markFailure(message);
        const exponentialDelay = pollIntervalMs * 2 ** Math.min(failures, 6);
        const jitter = Math.round(Math.random() * Math.min(1_000, pollIntervalMs));
        schedule(Math.min(maxBackoffMs, exponentialDelay + jitter));
      } finally {
        clearTimeout(timeout);
        if (activeController === controller) {
          activeController = null;
        }
        inFlight = false;
        if (!cancelled) {
          setIsLoading(false);
          setIsRefreshing(false);
        }
      }
    };

    const pollImmediately = () => {
      if (timer) {
        clearTimeout(timer);
      }
      if (!inFlight) {
        void poll();
      }
    };

    window.addEventListener("online", pollImmediately);
    document.addEventListener("visibilitychange", pollImmediately);
    void poll();

    return () => {
      cancelled = true;
      if (timer) {
        clearTimeout(timer);
      }
      activeController?.abort();
      window.removeEventListener("online", pollImmediately);
      document.removeEventListener("visibilitychange", pollImmediately);
    };
  }, [
    enabled,
    maxBackoffMs,
    pollIntervalMs,
    refreshGeneration,
    requestTimeoutMs,
  ]);

  const redriveBatch = useCallback(
    async (batchId: string) => {
      const response = await apiFetch(REDRIVE_PATH, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ batch_id: batchId }),
      });
      if (!response.ok) {
        throw await responseError(
          response,
          `Batch redrive failed (${response.status}).`,
        );
      }
      refresh();
    },
    [refresh],
  );

  return {
    data,
    connectionState,
    error,
    latencyMs,
    lastSuccessfulPollAt,
    consecutiveFailures,
    isLoading: enabled && isLoading,
    isRefreshing: enabled && isRefreshing,
    isStale,
    refresh,
    redriveBatch,
  };
}
