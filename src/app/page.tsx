import { VerificationDashboard } from "@/components/VerificationDashboard";

export default function HomePage() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border bg-surface px-6 py-3">
        <div>
          <h1 className="text-base font-semibold text-foreground">
            Verification Station
          </h1>
          <p className="text-xs text-muted">
            Drop to ingest · live phases · human verification
          </p>
        </div>
        <div className="rounded-md border border-border bg-surface-elevated px-3 py-1.5 font-mono text-[11px] text-muted">
          docuvision.preebg.dev
        </div>
      </header>

      <div className="min-h-0 flex-1">
        <VerificationDashboard />
      </div>
    </div>
  );
}
