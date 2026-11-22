/**
 * How the API console authenticates its requests: the browser session (cookie + X-CSRF-Token, the default) or a
 * pasted personal access token sent as `Authorization: Bearer <token>` without cookies. The token lives only in
 * memory of this tab (never localStorage, never the request history) and is gone after a reload.
 */
import { useSyncExternalStore } from "react";
import { authHeaders } from "../../../api/auth";

export interface ConsoleAuth {
  mode: "session" | "token";
  token: string;
}

let state: ConsoleAuth = { mode: "session", token: "" };
const listeners = new Set<() => void>();

export const consoleAuth = {
  get: (): ConsoleAuth => state,
  set(next: Partial<ConsoleAuth>): void {
    state = { ...state, ...next };
    for (const l of listeners) l();
  },
  subscribe(l: () => void): () => void {
    listeners.add(l);
    return () => listeners.delete(l);
  },
};

export function useConsoleAuth(): ConsoleAuth {
  return useSyncExternalStore(consoleAuth.subscribe, consoleAuth.get, consoleAuth.get);
}

/** Headers + credentials mode for one console request. */
export function consoleRequestAuth(method: string, auth: ConsoleAuth = state): { headers: Record<string, string>; credentials: RequestCredentials } {
  const token = auth.token.trim();
  if (auth.mode === "token" && token) return { headers: { Authorization: `Bearer ${token}` }, credentials: "omit" };
  return { headers: authHeaders(method), credentials: "same-origin" };
}
