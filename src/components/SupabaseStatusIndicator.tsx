"use client";

import { useEffect, useState } from "react";
import { getSupabaseClient, isSupabaseConfigured } from "@/lib/supabase/client";

type ConnectionState = "checking" | "connected" | "disconnected" | "unconfigured";

export function SupabaseStatusIndicator() {
  const [state, setState] = useState<ConnectionState>("checking");

  useEffect(() => {
    if (!isSupabaseConfigured()) {
      setState("unconfigured");
      return;
    }

    let cancelled = false;

    async function checkConnection() {
      try {
        const supabase = getSupabaseClient();
        const { error } = await supabase
          .from("document_pipeline")
          .select("id")
          .limit(1);

        if (!cancelled) {
          setState(error ? "disconnected" : "connected");
        }
      } catch {
        if (!cancelled) {
          setState("disconnected");
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
      label: "Supabase Online",
      dot: "bg-success",
      text: "text-success",
    },
    disconnected: {
      label: "Supabase Offline",
      dot: "bg-danger",
      text: "text-danger",
    },
    unconfigured: {
      label: "Not Configured",
      dot: "bg-muted",
      text: "text-muted",
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
