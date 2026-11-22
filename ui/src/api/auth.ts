/**
 * Panel login (AUTH-LOGIN): session cookie + CSRF double-submit, personal API tokens, admin user management.
 *
 * - The server sets `hydra_session` (HttpOnly) and `hydra_csrf` (readable). Every state-changing request to /api
 *   sends `X-CSRF-Token: <hydra_csrf>` (`authHeaders`); apiFetch adds it centrally, raw fetch call sites use the helper.
 * - Any 401 from the API (outside the login/password endpoints) calls `notifyUnauthorized`: the route guard
 *   (pages/auth/AuthGuard.tsx) refetches the session and sends the browser to /login?next=<page>.
 * - GET /v1/auth/session always answers 200. A failed or non-conforming answer (older backend, unit tests without a
 *   session mock) is read as "auth disabled", so the panel keeps working; a later 401 still redirects to login.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, isApiError } from "./client";

export const CSRF_COOKIE = "hydra_csrf";
export const CSRF_HEADER = "X-CSRF-Token";
export const UNAUTHORIZED_EVENT = "hydra:auth-required";
export const PASSWORD_CHANGE_EVENT = "hydra:password-change-required";

export type Role = "admin" | "analyst" | "viewer";

export const ROLE_LABEL: Record<Role, string> = { admin: "Administrator", analyst: "Analityk", viewer: "Obserwator" };
export const ROLES: Role[] = ["admin", "analyst", "viewer"];

export interface User {
  id: number;
  username: string;
  role: Role;
  must_change_password: boolean;
  disabled: boolean;
  created_at: string;
  last_login: string | null;
  locked_until: string | null;
  failed_attempts: number;
}

export interface CredentialsHint {
  file: string;
  docker: string;
  env: string;
  cli?: string;
}

export interface Session {
  auth_enabled: boolean;
  authenticated: boolean;
  user?: User;
  csrf_token?: string;
  expires_at?: string;
  idle_expires_at?: string;
  credentials_hint?: CredentialsHint;
}

export interface ApiToken {
  id: number;
  name: string;
  prefix: string;
  username: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

// ---------------------------------------------------------------------------
// CSRF
// ---------------------------------------------------------------------------

/** Value of a readable cookie (document.cookie), or null. */
export function readCookie(name: string): string | null {
  // defensive: non-browser runners (node harness with a stub document) have no cookie jar
  const jar: unknown = typeof document === "undefined" ? "" : document.cookie;
  if (typeof jar !== "string" || !jar) return null;
  for (const part of jar.split(";")) {
    const i = part.indexOf("=");
    if (i < 0) continue;
    if (part.slice(0, i).trim() === name) {
      const raw = part.slice(i + 1).trim();
      try {
        return decodeURIComponent(raw);
      } catch {
        return raw;
      }
    }
  }
  return null;
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);

export function isUnsafeMethod(method: string | undefined): boolean {
  return !SAFE_METHODS.has((method ?? "GET").toUpperCase());
}

/** Headers a request with `method` needs for the session: the CSRF token on POST/PUT/PATCH/DELETE, nothing otherwise. */
export function authHeaders(method: string | undefined = "GET"): Record<string, string> {
  if (!isUnsafeMethod(method)) return {};
  const token = readCookie(CSRF_COOKIE);
  return token ? { [CSRF_HEADER]: token } : {};
}

// ---------------------------------------------------------------------------
// 401 / forced password change signals
// ---------------------------------------------------------------------------

/** Paths whose 401 means "wrong credentials", not "session gone". */
const AUTH_FORM_PATHS = ["/v1/auth/login", "/v1/auth/password", "/v1/auth/session"];

function errorCode(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const code = (detail as { code?: unknown }).code;
    if (typeof code === "string") return code;
  }
  return null;
}

/** Called by every fetch path after a response: signals the guard on 401 or a pending forced password change. */
export function notifyAuthStatus(path: string, status: number, body?: unknown): void {
  if (typeof window === "undefined") return;
  const bare = path.split("?")[0] ?? path;
  if (status === 401 && !AUTH_FORM_PATHS.some((p) => bare.startsWith(p))) {
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT, { detail: { path } }));
  } else if (status === 403 && errorCode(body) === "password_change_required") {
    window.dispatchEvent(new CustomEvent(PASSWORD_CHANGE_EVENT, { detail: { path } }));
  }
}

/** `code` from a FastAPI `{detail: {code, message, retry_after_s?}}` error. */
export function apiErrorCode(error: unknown): string | null {
  return isApiError(error) ? errorCode(error.body) : null;
}

export function apiRetryAfter(error: unknown): number | null {
  if (!isApiError(error) || !error.body || typeof error.body !== "object") return null;
  const detail = (error.body as { detail?: unknown }).detail;
  if (detail && typeof detail === "object") {
    const s = (detail as { retry_after_s?: unknown }).retry_after_s;
    if (typeof s === "number" && Number.isFinite(s) && s > 0) return Math.ceil(s);
  }
  return null;
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

export const DISABLED_SESSION: Session = { auth_enabled: false, authenticated: false };

/** Shape check: anything that is not a real session answer means "auth disabled" (see module doc). */
export function parseSession(raw: unknown): Session {
  if (!raw || typeof raw !== "object") return DISABLED_SESSION;
  const r = raw as Record<string, unknown>;
  if (typeof r.auth_enabled !== "boolean") return DISABLED_SESSION;
  const user = r.user && typeof r.user === "object" && typeof (r.user as User).username === "string" ? (r.user as User) : undefined;
  return {
    auth_enabled: r.auth_enabled,
    authenticated: r.authenticated === true && Boolean(user),
    user,
    csrf_token: typeof r.csrf_token === "string" ? r.csrf_token : undefined,
    expires_at: typeof r.expires_at === "string" ? r.expires_at : undefined,
    idle_expires_at: typeof r.idle_expires_at === "string" ? r.idle_expires_at : undefined,
    credentials_hint: r.credentials_hint && typeof r.credentials_hint === "object" ? (r.credentials_hint as CredentialsHint) : undefined,
  };
}

export async function getSession(signal?: AbortSignal): Promise<Session> {
  try {
    return parseSession(await apiFetch<unknown>("/v1/auth/session", { signal, cache: "no-store" }));
  } catch (err) {
    if (isApiError(err) && err.kind === "aborted") throw err;
    return DISABLED_SESSION;
  }
}

export const sessionKey = ["auth", "session"] as const;

export function useSession() {
  return useQuery({
    queryKey: sessionKey,
    queryFn: ({ signal }) => getSession(signal),
    staleTime: 60_000,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/** The signed-in user when auth is enabled, else null (menu, read-only hints). */
export function useCurrentUser(): User | null {
  const s = useSession().data;
  return s?.auth_enabled && s.authenticated && s.user ? s.user : null;
}

/** Tooltip on write controls disabled for a viewer (B10, audit f450a70). */
export const READ_ONLY_TIP = "Brak uprawnień (rola: przeglądający)";

/** Viewers get read-only controls; with auth disabled everybody may write. */
export function useCanWrite(): boolean {
  const s = useSession().data;
  if (!s?.auth_enabled) return true;
  return s.user?.role === "admin" || s.user?.role === "analyst";
}

// ---------------------------------------------------------------------------
// Login, logout, password
// ---------------------------------------------------------------------------

export interface LoginResult {
  user: User;
  csrf_token: string;
  expires_at: string;
  idle_expires_at: string;
}

export function login(body: { username: string; password: string; remember: boolean }): Promise<LoginResult> {
  return apiFetch<LoginResult>("/v1/auth/login", { method: "POST", json: body, credentials: "same-origin" });
}

export function logout(): Promise<void> {
  return apiFetch<void>("/v1/auth/logout", { method: "POST", credentials: "same-origin" });
}

export function changePassword(body: { current_password: string; new_password: string }): Promise<{ user: User }> {
  return apiFetch<{ user: User }>("/v1/auth/password", { method: "POST", json: body, credentials: "same-origin" });
}

/** Safe in-app redirect target: an absolute path on this origin, never back to /login. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return "/";
  if (next === "/login" || next.startsWith("/login/") || next.startsWith("/login?")) return "/";
  return next;
}

export function loginPath(next: string): string {
  const target = safeNext(next);
  return target === "/" ? "/login" : `/login?next=${encodeURIComponent(target)}`;
}

// ---------------------------------------------------------------------------
// Tokens and users
// ---------------------------------------------------------------------------

export const tokensKey = (all: boolean) => ["auth", "tokens", all] as const;
export const usersKey = ["auth", "users"] as const;

export function useTokens(all: boolean) {
  return useQuery({
    queryKey: tokensKey(all),
    queryFn: ({ signal }) => apiFetch<{ items: ApiToken[] }>(`/v1/auth/tokens${all ? "?all=1" : ""}`, { signal }),
    retry: false,
  });
}

export function useCreateToken() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; expires_days?: number | null }) =>
      apiFetch<{ token: string; item: ApiToken }>("/v1/auth/tokens", { method: "POST", json: body }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["auth", "tokens"] }),
  });
}

export function useRevokeToken() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiFetch<void>(`/v1/auth/tokens/${id}`, { method: "DELETE" }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["auth", "tokens"] }),
  });
}

export function useUsers(enabled = true) {
  return useQuery({
    queryKey: usersKey,
    queryFn: ({ signal }) => apiFetch<{ items: User[] }>("/v1/auth/users", { signal }),
    enabled,
    retry: false,
  });
}

export function useCreateUser() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { username: string; role: Role }) => apiFetch<{ user: User; password: string }>("/v1/auth/users", { method: "POST", json: body }),
    onSuccess: () => void client.invalidateQueries({ queryKey: usersKey }),
  });
}

export function useUpdateUser() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number; role?: Role; disabled?: boolean }) =>
      apiFetch<{ user: User }>(`/v1/auth/users/${id}`, { method: "PATCH", json: body }),
    onSuccess: () => void client.invalidateQueries({ queryKey: usersKey }),
  });
}

export function useResetPassword() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiFetch<{ user: User; password: string }>(`/v1/auth/users/${id}/reset-password`, { method: "POST" }),
    onSuccess: () => void client.invalidateQueries({ queryKey: usersKey }),
  });
}
