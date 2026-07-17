"use client";

import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import {
  getActiveOperator,
  signInOperator,
  signOutOperator,
  type OperatorIdentity,
} from "@/lib/operator-session";

export function OperatorSessionGate({ children }: { children: ReactNode }) {
  const [operator, setOperator] = useState<OperatorIdentity | null>(null);
  const [checking, setChecking] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getActiveOperator()
      .then((identity) => {
        if (!cancelled) {
          setOperator(identity);
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "Sign-in failed.");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setChecking(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSignIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    const form = new FormData(event.currentTarget);
    try {
      const identity = await signInOperator(
        String(form.get("username") ?? "").trim(),
        String(form.get("password") ?? ""),
      );
      setOperator(identity);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign-in failed.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSignOut() {
    setSubmitting(true);
    setError(null);
    try {
      await signOutOperator();
      setOperator(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign-out failed.");
    } finally {
      setSubmitting(false);
    }
  }

  if (checking) {
    return (
      <div className="flex h-full min-h-screen items-center justify-center bg-background text-sm text-muted">
        Restoring secure operator session…
      </div>
    );
  }

  if (!operator) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background p-6">
        <form
          className="w-full max-w-sm rounded-xl border border-border bg-surface p-6"
          onSubmit={handleSignIn}
        >
          <h1 className="text-lg font-semibold text-foreground">
            Operator sign in
          </h1>
          <p className="mt-1 text-xs text-muted">
            Authenticate before reviewing or releasing documents.
          </p>
          <label className="mt-5 block text-xs font-medium text-muted">
            Username
            <input
              name="username"
              type="text"
              autoComplete="username"
              required
              className="mt-1.5 w-full rounded-lg border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none focus:border-accent"
            />
          </label>
          <label className="mt-4 block text-xs font-medium text-muted">
            Password
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              required
              className="mt-1.5 w-full rounded-lg border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none focus:border-accent"
            />
          </label>
          {error ? (
            <p role="alert" className="mt-4 text-xs text-danger">
              {error}
            </p>
          ) : null}
          <button
            type="submit"
            disabled={submitting}
            className="mt-5 w-full rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50"
          >
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </main>
    );
  }

  return (
    <div className="relative h-full">
      <div className="absolute right-4 top-3 z-50 flex items-center gap-2 rounded-lg border border-border bg-surface px-3 py-1.5 text-xs">
        <span className="max-w-48 truncate text-muted">
          {operator.display_name ?? operator.username}
        </span>
        <button
          type="button"
          disabled={submitting}
          onClick={() => void handleSignOut()}
          className="font-medium text-foreground hover:text-accent disabled:opacity-50"
        >
          Sign out
        </button>
      </div>
      {error ? (
        <p
          role="alert"
          className="absolute right-4 top-12 z-50 rounded bg-danger px-3 py-2 text-xs text-white"
        >
          {error}
        </p>
      ) : null}
      {children}
    </div>
  );
}
