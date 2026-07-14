"use client";

import {
  PIPELINE_PHASES,
  PipelineStatus,
  phaseIndexForStatus,
} from "@/lib/types";

const PHASE_ORDER: PipelineStatus[] = [
  "IMPORTED",
  "SCANNING",
  "PENDING_VERIFICATION",
];

type PipelinePhaseStepperProps = {
  status: PipelineStatus | null;
  fileName?: string | null;
};

export function PipelinePhaseStepper({
  status,
  fileName,
}: PipelinePhaseStepperProps) {
  const activeIndex = phaseIndexForStatus(status);
  const failed = status === "FAILED";
  const completedReview = status === "VERIFIED";

  return (
    <div className="border-b border-border bg-surface-elevated px-6 py-4">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent">
            Pipeline Phase
          </p>
          <p className="mt-1 text-sm text-muted">
            {fileName ? (
              <span className="font-medium text-foreground">{fileName}</span>
            ) : (
              "Waiting for intake…"
            )}
          </p>
        </div>
        <p className="text-xs text-muted">
          {failed
            ? "Failed"
            : completedReview
              ? "Verified"
              : status
                ? PIPELINE_PHASES[Math.max(activeIndex, 0)]?.label
                : "Idle"}
        </p>
      </div>

      <ol className="grid grid-cols-3 gap-3">
        {PIPELINE_PHASES.map((phase, index) => {
          const reached =
            !failed &&
            (completedReview ||
              (activeIndex >= 0 && index <= activeIndex) ||
              (status !== null &&
                PHASE_ORDER.indexOf(status as (typeof PHASE_ORDER)[number]) >
                  index));
          const current = !failed && !completedReview && index === activeIndex;

          return (
            <li
              key={phase.status}
              className={`rounded-lg border px-3 py-2.5 transition ${
                current
                  ? "border-accent/50 bg-accent/10"
                  : reached
                    ? "border-success/30 bg-success/5"
                    : "border-border bg-background"
              }`}
            >
              <div className="flex items-center gap-2">
                <span
                  className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-semibold ${
                    current
                      ? "bg-accent text-white"
                      : reached
                        ? "bg-success text-white"
                        : "bg-border text-muted"
                  }`}
                >
                  {reached && !current ? "✓" : index + 1}
                </span>
                <span
                  className={`text-xs font-semibold ${
                    current || reached ? "text-foreground" : "text-muted"
                  }`}
                >
                  {phase.label}
                </span>
              </div>
              <p className="mt-1.5 text-[11px] leading-snug text-muted">
                {phase.description}
              </p>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
