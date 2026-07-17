"use client";

import { useState } from "react";
import { useRemotePipelineStatus } from "@/hooks/useRemotePipelineStatus";
import {
  REMOTE_PIPELINE_STAGE_ORDER,
  type RemoteIngestionBatch,
  type RemotePipelineStageKey,
  type RemotePipelineStageState,
  type RemoteStageTelemetry,
} from "@/lib/remote-pipeline-types";

const STAGE_DETAILS: Record<
  RemotePipelineStageKey,
  { label: string; description: string; shortLabel: string }
> = {
  ingestion: {
    label: "Ingestion Wave",
    shortLabel: "INGEST",
    description: "File transfer to server",
  },
  layout_parsing: {
    label: "Layout Parsing",
    shortLabel: "DOCLING",
    description: "Server CPU/GPU structure pass",
  },
  wave_a_opencv: {
    label: "Wave A: OpenCV",
    shortLabel: "WAVE A",
    description: "Fast pixel analysis",
  },
  wave_b_glm_ocr: {
    label: "Wave B: GLM-OCR",
    shortLabel: "WAVE B",
    description: "VRAM text extraction",
  },
  wave_c_qwen3_vl: {
    label: "Wave C: Qwen3-VL",
    shortLabel: "WAVE C",
    description: "Serial extraction escalation",
  },
};

const STAGE_STATE_STYLES: Record<RemotePipelineStageState, string> = {
  pending: "border-border bg-background text-muted",
  queued: "border-amber-500/30 bg-amber-500/10 text-amber-300",
  running: "border-accent/50 bg-accent/10 text-blue-300",
  complete: "border-success/35 bg-success/10 text-green-300",
  failed: "border-danger/50 bg-danger/10 text-red-300",
  skipped: "border-border bg-surface-elevated text-muted",
};

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
}

function formatGb(value: number): string {
  return `${value.toFixed(1)} GB`;
}

function formatDuration(durationMs: number | null): string {
  if (durationMs === null) {
    return "—";
  }
  if (durationMs < 1_000) {
    return `${Math.round(durationMs)} ms`;
  }
  const seconds = durationMs / 1_000;
  if (seconds < 60) {
    return `${seconds.toFixed(1)} s`;
  }
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString();
}

function stageMap(
  batch: RemoteIngestionBatch,
): Map<RemotePipelineStageKey, RemoteStageTelemetry> {
  return new Map(batch.stages.map((stage) => [stage.key, stage]));
}

function Metric({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: "success" | "danger" | "warning";
}) {
  const valueColor =
    accent === "success"
      ? "text-success"
      : accent === "danger"
        ? "text-danger"
        : accent === "warning"
          ? "text-amber-300"
          : "text-foreground";

  return (
    <div className="min-w-0 border-l border-border pl-4 first:border-l-0 first:pl-0">
      <dt className="truncate text-[9px] font-semibold uppercase tracking-[0.16em] text-muted">
        {label}
      </dt>
      <dd className={`mt-1 truncate font-mono text-sm ${valueColor}`}>
        {value}
      </dd>
    </div>
  );
}

function StageCard({
  stageKey,
  stage,
}: {
  stageKey: RemotePipelineStageKey;
  stage: RemoteStageTelemetry | undefined;
}) {
  const details = STAGE_DETAILS[stageKey];
  const state = stage?.state ?? "pending";
  const progress = clampPercent(stage?.progress_percent ?? 0);

  return (
    <li
      className={`relative min-w-0 rounded-lg border p-3 ${STAGE_STATE_STYLES[state]}`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-mono text-[9px] font-bold tracking-[0.18em] opacity-70">
            {details.shortLabel}
          </p>
          <p className="mt-1 truncate text-[11px] font-semibold text-foreground">
            {details.label}
          </p>
        </div>
        <span
          className={`mt-0.5 h-2 w-2 shrink-0 rounded-full ${
            state === "running"
              ? "animate-pulse bg-accent"
              : state === "complete"
                ? "bg-success"
                : state === "failed"
                  ? "bg-danger"
                  : "bg-muted"
          }`}
          aria-hidden
        />
      </div>
      <p className="mt-1 min-h-8 text-[10px] leading-4 text-muted">
        {details.description}
      </p>
      <div className="mt-2 h-1 overflow-hidden rounded-full bg-black/30">
        <div
          className={`h-full transition-[width] duration-500 ${
            state === "failed"
              ? "bg-danger"
              : state === "complete"
                ? "bg-success"
                : "bg-accent"
          }`}
          style={{ width: `${progress}%` }}
        />
      </div>
      <div className="mt-2 flex justify-between gap-2 font-mono text-[9px] uppercase">
        <span>{state}</span>
        <span>{progress}%</span>
      </div>
      {stage?.worker_node_id && (
        <p className="mt-1 truncate font-mono text-[9px] text-muted">
          node:{stage.worker_node_id}
        </p>
      )}
    </li>
  );
}

function BatchTelemetry({
  batch,
  isRedriving,
  redriveError,
  onRedrive,
}: {
  batch: RemoteIngestionBatch;
  isRedriving: boolean;
  redriveError: string | null;
  onRedrive: (batchId: string) => void;
}) {
  const stages = stageMap(batch);
  const isOom = batch.error?.code === "VRAM_OOM";
  const canRedrive = batch.state === "failed" || batch.error?.retryable;

  return (
    <details className="group border-b border-border last:border-b-0" open>
      <summary className="flex cursor-pointer list-none items-center gap-4 px-4 py-3.5 transition hover:bg-surface-elevated/60 [&::-webkit-details-marker]:hidden">
        <span
          className={`h-2.5 w-2.5 shrink-0 rounded-full ${
            batch.state === "failed"
              ? "bg-danger"
              : batch.state === "processing" || batch.state === "transferring"
                ? "animate-pulse bg-accent"
                : batch.state === "complete"
                  ? "bg-success"
                  : "bg-amber-400"
          }`}
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h3 className="truncate font-mono text-xs font-semibold text-foreground">
              {batch.display_name || `Batch ${batch.id.slice(0, 8)}`}
            </h3>
            <span className="rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-muted">
              {batch.state}
            </span>
            {isOom && (
              <span className="rounded border border-danger/50 bg-danger/10 px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-wider text-danger">
                VRAM OOM
              </span>
            )}
          </div>
          <p className="mt-1 truncate font-mono text-[10px] text-muted">
            {batch.files_processed}/{batch.files_total} files · workers{" "}
            {batch.assigned_worker_node_ids.join(", ") || "awaiting allocation"} ·
            updated {formatTimestamp(batch.updated_at)}
          </p>
        </div>
        <div className="hidden w-32 shrink-0 sm:block">
          <div className="mb-1 flex justify-between font-mono text-[9px] text-muted">
            <span>PROGRESS</span>
            <span>{clampPercent(batch.progress_percent)}%</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-background">
            <div
              className="h-full bg-accent transition-[width] duration-500"
              style={{ width: `${clampPercent(batch.progress_percent)}%` }}
            />
          </div>
        </div>
        <span className="font-mono text-muted transition group-open:rotate-90">
          ›
        </span>
      </summary>

      <div className="border-t border-border bg-background/50 px-4 py-4">
        {batch.error && (
          <div
            className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2.5"
            role="alert"
          >
            <div>
              <p className="font-mono text-[10px] font-bold uppercase tracking-wider text-danger">
                {batch.error.code} · {batch.error.stage ?? "pipeline"}
              </p>
              <p className="mt-1 text-[11px] text-red-200">
                {batch.error.message}
              </p>
              {redriveError && (
                <p className="mt-1 text-[10px] text-danger">{redriveError}</p>
              )}
            </div>
            {canRedrive && (
              <button
                type="button"
                disabled={isRedriving}
                onClick={() => onRedrive(batch.id)}
                className="rounded-md border border-danger/50 bg-danger/15 px-3 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-red-100 transition hover:bg-danger/25 disabled:cursor-wait disabled:opacity-60"
              >
                {isRedriving ? "Redriving…" : "Retry / Redrive Batch"}
              </button>
            )}
          </div>
        )}

        <ol className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-5">
          {REMOTE_PIPELINE_STAGE_ORDER.map((key) => (
            <StageCard key={key} stageKey={key} stage={stages.get(key)} />
          ))}
        </ol>

        <div className="mt-4 grid gap-3 xl:grid-cols-[1.2fr_1fr]">
          <section className="rounded-lg border border-border bg-surface p-3">
            <h4 className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-muted">
              Stage execution durations
            </h4>
            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
              {REMOTE_PIPELINE_STAGE_ORDER.map((key) => {
                const stage = stages.get(key);
                return (
                  <div key={key} className="flex items-baseline justify-between gap-2">
                    <dt className="truncate text-[10px] text-muted">
                      {STAGE_DETAILS[key].shortLabel}
                    </dt>
                    <dd className="shrink-0 font-mono text-[10px] text-foreground">
                      {formatDuration(stage?.duration_ms ?? null)}
                    </dd>
                  </div>
                );
              })}
            </dl>
          </section>

          <section className="rounded-lg border border-border bg-surface p-3">
            <div className="flex items-center justify-between gap-3">
              <h4 className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-muted">
                Server memory GC confirmations
              </h4>
              <span className="font-mono text-[9px] text-muted">
                {batch.gc_events.filter((event) => event.confirmed).length}/
                {batch.gc_events.length} confirmed
              </span>
            </div>
            {batch.gc_events.length === 0 ? (
              <p className="mt-3 font-mono text-[10px] text-muted">
                No gc.collect() events reported.
              </p>
            ) : (
              <ul className="mt-2 max-h-24 space-y-1 overflow-y-auto">
                {batch.gc_events.map((event, index) => (
                  <li
                    key={`${event.stage}-${event.occurred_at}-${index}`}
                    className="flex items-center justify-between gap-2 font-mono text-[9px]"
                  >
                    <span
                      className={
                        event.confirmed ? "text-success" : "text-amber-300"
                      }
                    >
                      {event.confirmed ? "✓ CONFIRMED" : "… PENDING"} ·{" "}
                      {STAGE_DETAILS[event.stage].shortLabel}
                    </span>
                    <span className="text-muted">
                      {event.collected_objects ?? "—"} objects ·{" "}
                      {formatTimestamp(event.occurred_at)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>
    </details>
  );
}

export function IngestionStatusPanel() {
  const {
    data,
    connectionState,
    error,
    latencyMs,
    lastSuccessfulPollAt,
    consecutiveFailures,
    isLoading,
    isRefreshing,
    isStale,
    refresh,
    redriveBatch,
  } = useRemotePipelineStatus();
  const [redriving, setRedriving] = useState<Record<string, boolean>>({});
  const [redriveErrors, setRedriveErrors] = useState<Record<string, string>>({});

  const handleRedrive = async (batchId: string) => {
    setRedriving((current) => ({ ...current, [batchId]: true }));
    setRedriveErrors((current) => ({ ...current, [batchId]: "" }));
    try {
      await redriveBatch(batchId);
    } catch (caught) {
      setRedriveErrors((current) => ({
        ...current,
        [batchId]:
          caught instanceof Error ? caught.message : "Batch redrive failed.",
      }));
    } finally {
      setRedriving((current) => ({ ...current, [batchId]: false }));
    }
  };

  const cluster = data?.cluster;
  const connectionColor =
    connectionState === "online"
      ? "success"
      : connectionState === "connecting"
        ? "warning"
        : "danger";

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <header className="shrink-0 border-b border-border bg-surface px-4 py-3 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="font-mono text-[9px] font-bold uppercase tracking-[0.22em] text-accent">
              Remote server control center
            </p>
            <h1 className="mt-1 text-base font-semibold text-foreground">
              Active Ingestion Telemetry
            </h1>
          </div>
          <div className="flex items-center gap-2 font-mono text-[10px] text-muted">
            {isRefreshing && <span className="animate-pulse">SYNCING</span>}
            <span
              className={`h-2 w-2 rounded-full ${
                connectionColor === "success"
                  ? "bg-success"
                  : connectionColor === "warning"
                    ? "animate-pulse bg-amber-400"
                    : "bg-danger"
              }`}
              aria-hidden
            />
            <span className="uppercase">{connectionState}</span>
          </div>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        <section
          className={`rounded-xl border bg-surface px-4 py-4 ${
            connectionState === "online"
              ? "border-border"
              : "border-danger/40"
          }`}
          aria-label="Server cluster health"
        >
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span
                className={`h-2 w-2 rounded-full ${
                  cluster?.status === "healthy"
                    ? "animate-pulse bg-success"
                    : cluster?.status === "degraded"
                      ? "bg-amber-400"
                      : "bg-danger"
                }`}
                aria-hidden
              />
              <h2 className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-foreground">
                Server Cluster Health
              </h2>
            </div>
            <p className="font-mono text-[9px] text-muted">
              LAST RX{" "}
              {lastSuccessfulPollAt
                ? lastSuccessfulPollAt.toLocaleTimeString()
                : "NEVER"}
            </p>
          </div>

          <dl className="grid grid-cols-2 gap-x-4 gap-y-4 sm:grid-cols-4">
            <Metric
              label="Worker Nodes Active"
              value={
                cluster
                  ? `${cluster.active_worker_nodes}/${cluster.total_worker_nodes}`
                  : "—/—"
              }
              accent={
                cluster &&
                cluster.active_worker_nodes === cluster.total_worker_nodes
                  ? "success"
                  : "warning"
              }
            />
            <Metric
              label="Network Latency"
              value={latencyMs === null ? "— ms" : `${latencyMs} ms`}
              accent={
                latencyMs === null ? undefined : latencyMs > 1_000 ? "danger" : undefined
              }
            />
            <Metric
              label="Server VRAM Usage"
              value={
                cluster
                  ? `${formatGb(cluster.vram_used_gb)} / ${formatGb(cluster.vram_total_gb)}`
                  : "— GB / — GB"
              }
            />
            <Metric
              label="Server RAM Usage"
              value={
                cluster
                  ? `${formatGb(cluster.ram_used_gb)} / ${formatGb(cluster.ram_total_gb)}`
                  : "— GB / — GB"
              }
            />
          </dl>

          {(error || isStale) && (
            <div
              className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-danger/30 pt-3"
              role="alert"
              aria-live="polite"
            >
              <p className="text-[11px] text-red-200">
                <span className="font-mono font-bold text-danger">
                  TELEMETRY LINK INTERRUPTED
                </span>
                {" · "}
                {error ?? "Showing last-known server state."}
                {consecutiveFailures > 0 &&
                  ` (${consecutiveFailures} consecutive failure${
                    consecutiveFailures === 1 ? "" : "s"
                  })`}
              </p>
              <button
                type="button"
                onClick={refresh}
                className="rounded-md border border-danger/50 bg-danger/10 px-3 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-wider text-red-100 transition hover:bg-danger/20"
              >
                Retry connection
              </button>
            </div>
          )}
        </section>

        <section className="mt-4 overflow-hidden rounded-xl border border-border bg-surface">
          <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
            <div>
              <h2 className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-foreground">
                Active Job Queues
              </h2>
              <p className="mt-1 text-[10px] text-muted">
                Worker allocation updates are applied by stable batch ID.
              </p>
            </div>
            <span className="rounded-md border border-border bg-background px-2 py-1 font-mono text-[10px] text-muted">
              {data?.batches.length ?? 0} ACTIVE
            </span>
          </div>

          {isLoading && !data ? (
            <div className="flex items-center justify-center gap-3 px-4 py-16 text-sm text-muted">
              <span className="h-5 w-5 animate-spin rounded-full border-2 border-border border-t-accent" />
              Establishing secure telemetry link…
            </div>
          ) : !data ? (
            <div className="px-4 py-16 text-center">
              <p className="font-mono text-xs font-semibold text-danger">
                REMOTE CLUSTER UNAVAILABLE
              </p>
              <p className="mx-auto mt-2 max-w-lg text-[11px] text-muted">
                No server snapshot has been received. Check the network route and
                remote API, then retry the connection.
              </p>
              <button
                type="button"
                onClick={refresh}
                className="mt-4 rounded-md border border-accent/50 bg-accent/10 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-blue-200 transition hover:bg-accent/20"
              >
                Retry connection
              </button>
            </div>
          ) : data.batches.length === 0 ? (
            <div className="px-4 py-16 text-center">
              <p className="font-mono text-xs text-success">QUEUE CLEAR</p>
              <p className="mt-2 text-[11px] text-muted">
                The remote cluster reports no active ingestion batches.
              </p>
            </div>
          ) : (
            data.batches.map((batch) => (
              <BatchTelemetry
                key={batch.id}
                batch={batch}
                isRedriving={Boolean(redriving[batch.id])}
                redriveError={redriveErrors[batch.id] || null}
                onRedrive={(batchId) => void handleRedrive(batchId)}
              />
            ))
          )}
        </section>
      </div>
    </div>
  );
}
