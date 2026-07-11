export function QueueClearState() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-6 p-12 text-center">
      <div className="animate-pulse-glow flex h-28 w-28 items-center justify-center rounded-full bg-success/10 ring-2 ring-success/30">
        <svg
          viewBox="0 0 52 52"
          className="h-16 w-16 animate-check-circle"
          aria-hidden
        >
          <circle
            cx="26"
            cy="26"
            r="24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className="text-success/30"
          />
          <path
            d="M14 27l8 8 16-18"
            fill="none"
            stroke="currentColor"
            strokeWidth="3"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="animate-check-path text-success"
          />
        </svg>
      </div>
      <div>
        <h2 className="text-2xl font-semibold tracking-tight text-foreground">
          All operational queues clear. Great job!
        </h2>
        <p className="mt-2 text-sm text-muted">
          No documents are waiting for human verification.
        </p>
      </div>
    </div>
  );
}
