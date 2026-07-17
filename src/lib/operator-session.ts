"use client";

const STORAGE_KEY = "docuvision.operator-session.v1";
const EXPIRY_SKEW_SECONDS = 30;

export type OperatorIdentity = {
  id: string;
  username: string;
  display_name: string | null;
  role: string;
};

type OperatorSession = {
  access_token: string;
  access_expires_at: number;
  refresh_token: string;
  session_id: string;
  operator: OperatorIdentity;
};

let activeSession: OperatorSession | null = null;
let refreshPromise: Promise<OperatorSession> | null = null;

function isTauriRuntime(): boolean {
  return (
    typeof window !== "undefined" &&
    ("__TAURI_INTERNALS__" in window || "__TAURI__" in window)
  );
}

async function readPersistedSession(): Promise<string | null> {
  if (isTauriRuntime()) {
    const { invoke } = await import("@tauri-apps/api/core");
    return invoke<string | null>("read_operator_session");
  }
  return typeof window === "undefined"
    ? null
    : window.sessionStorage.getItem(STORAGE_KEY);
}

async function persistSession(session: OperatorSession): Promise<void> {
  const serialized = JSON.stringify(session);
  if (isTauriRuntime()) {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("write_operator_session", { session: serialized });
    return;
  }
  window.sessionStorage.setItem(STORAGE_KEY, serialized);
}

async function deletePersistedSession(): Promise<void> {
  if (isTauriRuntime()) {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("delete_operator_session");
    return;
  }
  window.sessionStorage.removeItem(STORAGE_KEY);
}

function isOperatorSession(value: unknown): value is OperatorSession {
  if (!value || typeof value !== "object") {
    return false;
  }
  const session = value as Partial<OperatorSession>;
  return (
    typeof session.access_token === "string" &&
    typeof session.access_expires_at === "number" &&
    typeof session.refresh_token === "string" &&
    typeof session.session_id === "string" &&
    Boolean(session.operator) &&
    typeof session.operator?.id === "string" &&
    typeof session.operator?.username === "string" &&
    typeof session.operator?.role === "string"
  );
}

async function localAuthFetch(
  path: string,
  init: RequestInit,
): Promise<Response> {
  // Dynamic import avoids a module cycle: api.ts consumes getOperatorAccessToken.
  const { apiUrl, getLocalApiKey } = await import("@/lib/api");
  const localToken = await getLocalApiKey();
  const headers = new Headers(init.headers);
  headers.set("X-API-Key", localToken);
  return fetch(apiUrl(path), { ...init, headers });
}

async function refreshOperatorSession(): Promise<OperatorSession> {
  if (!activeSession) {
    throw new Error("No operator session is available.");
  }
  if (refreshPromise) {
    return refreshPromise;
  }
  const current = activeSession;
  refreshPromise = (async () => {
    const response = await localAuthFetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: current.session_id,
        refresh_token: current.refresh_token,
      }),
    });
    if (!response.ok) {
      activeSession = null;
      await deletePersistedSession();
      throw new Error("Operator session expired. Sign in again.");
    }
    const session = (await response.json()) as unknown;
    if (!isOperatorSession(session)) {
      throw new Error("Local authentication service returned an invalid session.");
    }
    activeSession = session;
    await persistSession(session);
    return session;
  })();
  try {
    return await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}

/**
 * Restore, replace, or clear the active cryptographic operator session.
 *
 * Tauri persists refresh material only in the OS credential manager. Browser
 * development uses tab-scoped sessionStorage and never localStorage.
 */
export async function setOperatorIdentity(
  session?: OperatorSession | null,
): Promise<OperatorIdentity | null> {
  if (session === null) {
    activeSession = null;
    await deletePersistedSession();
    return null;
  }
  if (session) {
    if (!isOperatorSession(session)) {
      throw new Error("Refusing to store an invalid operator session.");
    }
    activeSession = session;
    await persistSession(session);
    return session.operator;
  }
  if (!activeSession) {
    const serialized = await readPersistedSession();
    if (!serialized) {
      return null;
    }
    try {
      const restored = JSON.parse(serialized) as unknown;
      if (!isOperatorSession(restored)) {
        throw new Error("Invalid stored session.");
      }
      activeSession = restored;
    } catch {
      await deletePersistedSession();
      return null;
    }
  }
  if (activeSession.access_expires_at <= Date.now() / 1000 + EXPIRY_SKEW_SECONDS) {
    await refreshOperatorSession();
  }
  return activeSession?.operator ?? null;
}

export async function signInOperator(
  username: string,
  password: string,
): Promise<OperatorIdentity> {
  const response = await localAuthFetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    throw new Error(
      response.status === 401
        ? "Invalid username or password."
        : `Local sign-in failed (${response.status}).`,
    );
  }
  const session = (await response.json()) as unknown;
  if (!isOperatorSession(session)) {
    throw new Error("Local authentication service returned an invalid session.");
  }
  await setOperatorIdentity(session);
  return session.operator;
}

export async function signOutOperator(): Promise<void> {
  const current = activeSession;
  try {
    if (current) {
      await localAuthFetch("/api/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: current.session_id,
          refresh_token: current.refresh_token,
        }),
      });
    }
  } finally {
    await setOperatorIdentity(null);
  }
}

export async function getOperatorAccessToken(): Promise<string> {
  await setOperatorIdentity();
  if (!activeSession) {
    throw new Error("No valid operator session. Sign in before continuing.");
  }
  return activeSession.access_token;
}

export async function getActiveOperator(): Promise<OperatorIdentity | null> {
  return setOperatorIdentity();
}
