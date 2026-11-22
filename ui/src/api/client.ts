/**
 * Typed fetch wrapper + react-query hooks for the local backend.
 *
 * The browser only talks to `/api/*` on its own origin; Vite (dev/preview)
 * proxies that to the FastAPI process on 127.0.0.1:8000 with `/api` stripped.
 */
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type Query,
} from "@tanstack/react-query";
import type {
  Artifact,
  ArtifactPage,
  GraphPayload,
  Health,
  ImportAccepted,
  Job,
  JobStatus,
} from "./types";
import { JOB_STATUSES, TERMINAL_JOB_STATUSES } from "./types";
import { withRun } from "../lib/runSelection";
import { authHeaders, notifyAuthStatus } from "./auth";

export const API_BASE = "/api";

/** Multipart field name used for every uploaded file in POST /v1/import. */
export const IMPORT_FIELD = "files";

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export type ApiErrorKind = "network" | "http" | "parse" | "contract" | "aborted";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly path: string;
  readonly body: unknown;

  constructor(opts: {
    kind: ApiErrorKind;
    message: string;
    path: string;
    status?: number | null;
    body?: unknown;
    cause?: unknown;
  }) {
    super(opts.message, opts.cause === undefined ? undefined : { cause: opts.cause });
    this.name = "ApiError";
    this.kind = opts.kind;
    this.status = opts.status ?? null;
    this.path = opts.path;
    this.body = opts.body;
  }

  /** Backend process not reachable (proxy refused / DNS / CORS / offline). */
  get isOffline(): boolean {
    return this.kind === "network" || this.status === 502 || this.status === 503 || this.status === 504;
  }

  /** Route does not exist (yet) on the backend. */
  get isNotImplemented(): boolean {
    return this.status === 404 || this.status === 405 || this.status === 501;
  }

  /**
   * The route exists but the resource does not (B3, audit f450a70): FastAPI answers an unknown route with the
   * generic `{"detail":"Not Found"}`, while task routes raise 404 with their own detail
   * ("campaign not found in this run", "incident not found").
   */
  get isResourceNotFound(): boolean {
    if (this.status !== 404) return false;
    const body = this.body;
    const detail = body && typeof body === "object" ? (body as Record<string, unknown>).detail : undefined;
    if (typeof detail !== "string") return false;
    const d = detail.trim().toLowerCase();
    return d !== "" && d !== "not found" && d !== "unknown api path";
  }

  get isClientError(): boolean {
    return this.status !== null && this.status >= 400 && this.status < 500;
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

/** Pull a human-readable message out of FastAPI / generic JSON error bodies. */
export function extractErrorMessage(body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body.trim().slice(0, 500);
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const detail = record.detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const m = (detail as Record<string, unknown>).message;
      if (typeof m === "string" && m.trim()) return m;
    }
    if (Array.isArray(detail) && detail.length > 0) {
      // FastAPI validation errors: [{loc: [...], msg: "...", type: "..."}]
      return detail
        .map((item) => {
          if (item && typeof item === "object") {
            const entry = item as { msg?: unknown; loc?: unknown };
            const loc = Array.isArray(entry.loc) ? entry.loc.join(".") : "";
            const msg = typeof entry.msg === "string" ? entry.msg : JSON.stringify(item);
            return loc ? `${loc}: ${msg}` : msg;
          }
          return String(item);
        })
        .join("; ");
    }
    for (const key of ["message", "error", "title"]) {
      const value = record[key];
      if (typeof value === "string" && value.trim()) return value;
    }
  }
  return fallback;
}

// ---------------------------------------------------------------------------
// Fetch wrapper
// ---------------------------------------------------------------------------

export interface ApiRequestInit extends Omit<RequestInit, "body"> {
  body?: BodyInit | null;
  /** JSON-serialisable payload; sets Content-Type automatically. */
  json?: unknown;
  /** Accepted status codes. Defaults to any 2xx. */
  expect?: readonly number[];
}

export function apiUrl(path: string): string {
  const normalised = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE}${normalised}`;
}

async function readBody(response: Response, path: string): Promise<unknown> {
  if (response.status === 204 || response.status === 205) return undefined;
  const text = await response.text();
  if (!text) return undefined;
  const contentType = response.headers.get("content-type") ?? "";
  const looksJson = contentType.includes("json") || /^[\s]*[[{"]/.test(text);
  if (!looksJson) return text;
  try {
    return JSON.parse(text) as unknown;
  } catch (cause) {
    if (contentType.includes("json")) {
      throw new ApiError({
        kind: "parse",
        message: `Malformed JSON from ${path}`,
        path,
        status: response.status,
        body: text,
        cause,
      });
    }
    return text;
  }
}

export async function apiFetch<T>(path: string, init: ApiRequestInit = {}): Promise<T> {
  const { json, expect, headers, ...rest } = init;
  const finalHeaders = new Headers(headers);
  if (!finalHeaders.has("Accept")) finalHeaders.set("Accept", "application/json");
  for (const [k, v] of Object.entries(authHeaders(rest.method))) if (!finalHeaders.has(k)) finalHeaders.set(k, v);
  let body = rest.body;
  if (json !== undefined) {
    finalHeaders.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }

  let response: Response;
  try {
    response = await fetch(apiUrl(withRun(path)), { ...rest, body, headers: finalHeaders });
  } catch (cause) {
    const aborted = cause instanceof DOMException && cause.name === "AbortError";
    throw new ApiError({
      kind: aborted ? "aborted" : "network",
      message: aborted ? "Request aborted" : "Backend unreachable",
      path,
      cause,
    });
  }

  const parsed = await readBody(response, path);
  if (response.status === 401 || response.status === 403) notifyAuthStatus(path, response.status, parsed);

  if (!response.ok) {
    const fallback = `${response.status} ${response.statusText || "Request failed"}`.trim();
    throw new ApiError({
      kind: "http",
      message: extractErrorMessage(parsed, fallback),
      path,
      status: response.status,
      body: parsed,
    });
  }

  if (expect && !expect.includes(response.status)) {
    throw new ApiError({
      kind: "contract",
      message: `Unexpected status ${response.status} from ${path} (expected ${expect.join("/")})`,
      path,
      status: response.status,
      body: parsed,
    });
  }

  return parsed as T;
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export function getHealth(signal?: AbortSignal): Promise<Health> {
  return apiFetch<Health>("/health", { signal });
}

/** A file plus the path it should be recorded under (folder drops keep their relative path). */
export interface UploadEntry {
  file: File;
  path: string;
}

export function toUploadEntries(files: Iterable<File>): UploadEntry[] {
  return Array.from(files, (file) => ({
    file,
    path: file.webkitRelativePath || file.name,
  }));
}

export async function importFiles(entries: UploadEntry[], signal?: AbortSignal): Promise<ImportAccepted> {
  if (entries.length === 0) {
    throw new ApiError({ kind: "contract", message: "Nothing to import", path: "/v1/import" });
  }
  const form = new FormData();
  for (const { file, path } of entries) {
    form.append(IMPORT_FIELD, file, path);
  }
  const result = await apiFetch<ImportAccepted>("/v1/import", {
    method: "POST",
    body: form,
    signal,
    expect: [202],
  });
  if (!result || typeof result !== "object" || typeof result.job_id !== "string" || !result.job_id) {
    throw new ApiError({
      kind: "contract",
      message: "Import accepted but no job_id was returned",
      path: "/v1/import",
      status: 202,
      body: result,
    });
  }
  return result;
}

export function isJobStatus(value: unknown): value is JobStatus {
  return typeof value === "string" && (JOB_STATUSES as readonly string[]).includes(value);
}

export function isTerminalStatus(status: unknown): boolean {
  return isJobStatus(status) && TERMINAL_JOB_STATUSES.includes(status);
}

export function getJob(id: string, signal?: AbortSignal): Promise<Job> {
  return apiFetch<Job>(`/v1/jobs/${encodeURIComponent(id)}`, { signal });
}

export interface PageParams {
  limit: number;
  offset: number;
}

export function listArtifacts({ limit, offset }: PageParams, signal?: AbortSignal): Promise<ArtifactPage> {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return apiFetch<ArtifactPage>(`/v1/artifacts?${qs.toString()}`, { signal });
}

/** Convention (not yet in the contract): single artifact metadata. */
export function getArtifact(id: string, signal?: AbortSignal): Promise<Artifact> {
  return apiFetch<Artifact>(`/v1/artifacts/${encodeURIComponent(id)}`, { signal });
}

/** Convention (not yet in the contract): raw artifact bytes, used for previews. */
export function artifactContentUrl(id: string): string {
  return apiUrl(`/v1/artifacts/${encodeURIComponent(id)}/content`);
}

export function getGraph(signal?: AbortSignal): Promise<GraphPayload> {
  return apiFetch<GraphPayload>("/v1/graph", { signal });
}

// ---------------------------------------------------------------------------
// React Query hooks
// ---------------------------------------------------------------------------

export const queryKeys = {
  health: ["health"] as const,
  job: (id: string) => ["jobs", id] as const,
  artifacts: (params: PageParams) => ["artifacts", params] as const,
  artifactsAll: ["artifacts"] as const,
  artifact: (id: string) => ["artifact", id] as const,
  graph: ["graph"] as const,
};

/** Retry transient network failures once; never retry 4xx or contract errors. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isApiError(error)) {
    if (error.kind !== "network" && !(error.status !== null && error.status >= 500)) return false;
  }
  return failureCount < 1;
}

export function useHealth(intervalMs = 5000) {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: ({ signal }) => getHealth(signal),
    refetchInterval: intervalMs,
    refetchIntervalInBackground: false,
    retry: false,
    staleTime: 0,
  });
}

export function useImport() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (entries: UploadEntry[]) => importFiles(entries),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.artifactsAll });
    },
  });
}

/** Poll interval for a job query: stop on terminal status or on a 4xx (unknown job). */
export function jobRefetchInterval(query: Query<Job, Error>, intervalMs: number): number | false {
  const data = query.state.data;
  if (data && isTerminalStatus(data.status)) return false;
  const error = query.state.error;
  if (isApiError(error) && error.isClientError) return false;
  if (isApiError(error) && error.isOffline) return Math.max(intervalMs * 3, 3000);
  return intervalMs;
}

export function useJob(id: string | null | undefined, intervalMs = 1000) {
  return useQuery<Job, Error>({
    queryKey: queryKeys.job(id ?? ""),
    queryFn: ({ signal }) => getJob(id as string, signal),
    enabled: Boolean(id),
    refetchInterval: (query) => jobRefetchInterval(query, intervalMs),
    retry: false,
  });
}

export function useArtifacts(params: PageParams) {
  return useQuery({
    queryKey: queryKeys.artifacts(params),
    queryFn: ({ signal }) => listArtifacts(params, signal),
    placeholderData: keepPreviousData,
    retry: shouldRetry,
  });
}

export function useArtifact(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.artifact(id ?? ""),
    queryFn: ({ signal }) => getArtifact(id as string, signal),
    enabled: Boolean(id),
    retry: shouldRetry,
  });
}

export function useGraph(enabled = true) {
  return useQuery({
    queryKey: queryKeys.graph,
    queryFn: ({ signal }) => getGraph(signal),
    enabled,
    retry: shouldRetry,
    staleTime: 30_000,
  });
}
