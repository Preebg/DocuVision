import { ApiStatusIndicator } from "@/components/ApiStatusIndicator";

function LogoMark() {
  return (
    <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-accent to-indigo-600 shadow-lg shadow-accent/20">
      <svg
        viewBox="0 0 24 24"
        fill="none"
        className="h-5 w-5 text-white"
        aria-hidden
      >
        <path
          d="M6 4h12v16H6V4z"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinejoin="round"
        />
        <path
          d="M9 8h6M9 12h6M9 16h4"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
        />
      </svg>
    </div>
  );
}

export function Sidebar() {
  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-border bg-surface">
      <div className="flex items-center gap-3 border-b border-border px-5 py-5">
        <LogoMark />
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-muted">
            DocuVision
          </p>
          <h1 className="text-sm font-semibold leading-tight text-foreground">
            Control Center
          </h1>
        </div>
      </div>

      <nav className="flex flex-1 flex-col gap-1 px-3 py-4">
        <a
          href="/"
          className="flex items-center gap-2 rounded-lg bg-surface-elevated px-3 py-2.5 text-sm font-medium text-foreground ring-1 ring-border"
        >
          <span className="text-accent">◆</span>
          Batch Workbench
        </a>
        <p className="mt-4 px-3 text-[11px] uppercase tracking-wider text-muted">
          Station
        </p>
        <div className="px-3 py-1 text-xs text-muted">
          docuvision.preebg.dev
        </div>
      </nav>

      <div className="border-t border-border p-4">
        <ApiStatusIndicator />
      </div>
    </aside>
  );
}
