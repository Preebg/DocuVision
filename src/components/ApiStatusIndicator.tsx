"use client";

import { useEffect, useState } from "react";
import { fetchHealth } from "@/lib/api";

type ConnectionState = "checking" | "connected" | "disconnected";

export function ApiStatusIndicator() {
  const [state, setState] = useState<ConnectionState>("checking");
  const [repository, setRepository] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function checkConnection() {
      try {
        const health = await fetchHealth();
        if (!cancelled) {
          setState(health.status === "ok" ? "connected" : "disconnected");
          setRepository(health.repository);
        }
      } catch {
        if (!cancelled) {
          setState("disconnected");
          setRepository(null);
        }
      }
    }

    void checkConnection();
    const interval = setInterval(checkConnection, 30_000);

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const config = {
    checking: {
      label: "Connecting…",
      dot: "bg-amber-400 animate-pulse",
      text: "text-amber-300",
    },
    connected: {
      label: repository ? `API Online · ${repository}` : "API Online",
      dot: "bg-success",
      text: "text-success",
    },
    disconnected: {
      label: "API Offline",
      dot: "bg-danger",
      text: "text-danger",
    },
  }[state];

  return (
    <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-elevated px-3 py-2">
      <span className={`h-2 w-2 rounded-full ${config.dot}`} aria-hidden />
      <span className={`text-xs font-medium tracking-wide ${config.text}`}>
        {config.label}
      </span>
    </div>
  );
}
