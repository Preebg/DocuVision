export const REMOTE_PIPELINE_STAGE_ORDER = [
  "ingestion",
  "layout_parsing",
  "wave_a_opencv",
  "wave_b_glm_ocr",
  "wave_c_qwen3_vl",
] as const;

export type RemotePipelineStageKey =
  (typeof REMOTE_PIPELINE_STAGE_ORDER)[number];

export type RemotePipelineStageState =
  | "pending"
  | "queued"
  | "running"
  | "complete"
  | "failed"
  | "skipped";

export type RemoteBatchState =
  | "queued"
  | "transferring"
  | "processing"
  | "complete"
  | "failed"
  | "redriving";

export interface RemoteWorkerNode {
  id: string;
  hostname: string;
  state: "online" | "busy" | "draining" | "offline";
  current_batch_id: string | null;
  gpu_name: string | null;
  vram_used_gb: number;
  vram_total_gb: number;
  ram_used_gb: number;
  ram_total_gb: number;
  last_heartbeat_at: string;
}

export interface RemoteClusterHealth {
  status: "healthy" | "degraded" | "offline";
  active_worker_nodes: number;
  total_worker_nodes: number;
  vram_used_gb: number;
  vram_total_gb: number;
  ram_used_gb: number;
  ram_total_gb: number;
  workers: RemoteWorkerNode[];
}

export interface RemoteStageTelemetry {
  key: RemotePipelineStageKey;
  state: RemotePipelineStageState;
  progress_percent: number;
  duration_ms: number | null;
  started_at: string | null;
  completed_at: string | null;
  worker_node_id: string | null;
  vram_allocated_gb: number | null;
  detail: string | null;
}

export interface RemoteGarbageCollectionEvent {
  stage: RemotePipelineStageKey;
  confirmed: boolean;
  collected_objects: number | null;
  occurred_at: string;
}

export interface RemoteBatchError {
  code: "VRAM_OOM" | "NETWORK_INTERRUPTED" | "STAGE_FAILED" | string;
  message: string;
  stage: RemotePipelineStageKey | null;
  retryable: boolean;
  occurred_at: string;
}

export interface RemoteIngestionBatch {
  id: string;
  display_name: string;
  state: RemoteBatchState;
  progress_percent: number;
  files_processed: number;
  files_total: number;
  assigned_worker_node_ids: string[];
  queued_at: string;
  started_at: string | null;
  updated_at: string;
  stages: RemoteStageTelemetry[];
  gc_events: RemoteGarbageCollectionEvent[];
  error: RemoteBatchError | null;
}

/** Response contract for GET /api/v1/ingestion/batches/active. */
export interface RemotePipelineStatusResponse {
  generated_at: string;
  cluster: RemoteClusterHealth;
  batches: RemoteIngestionBatch[];
}

export type RemotePipelineConnectionState =
  | "connecting"
  | "online"
  | "degraded"
  | "offline";

export interface RemotePipelineStatusSnapshot {
  data: RemotePipelineStatusResponse | null;
  connectionState: RemotePipelineConnectionState;
  error: string | null;
  latencyMs: number | null;
  lastSuccessfulPollAt: Date | null;
  consecutiveFailures: number;
  isLoading: boolean;
  isRefreshing: boolean;
  isStale: boolean;
}
