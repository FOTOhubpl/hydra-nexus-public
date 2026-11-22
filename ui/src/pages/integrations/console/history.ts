/**
 * API console request history (localStorage, this browser only, cap 50).
 *
 * Stored: method, path, redacted query, status, ms, timestamp, operationId and — only when provably harmless — the
 * JSON body for replay. Never stored: headers (no auth header exists, and none is kept even if one is added), any
 * body on secret/connect/webhook endpoints, any body containing a field named like a secret, any query value of a
 * secret-like parameter (replaced by "•••").
 */
import { readJson, removeKey, writeJson } from "../../../lib/storage";

export const HISTORY_KEY = "integrations.console.history";
export const HISTORY_CAP = 50;
const BODY_CAP = 4096;

export interface HistoryEntry {
  method: string;
  path: string;
  query: string;
  status: number | null;
  ms: number;
  at: string;
  op?: string;
  body?: string;
}

/** Field / parameter names that may carry a credential. `key` matches api_key, x-api-key, private_key, … */
export const SENSITIVE_NAME = /secret|token|passw|pwd|key|auth|credential|cookie|signature|bearer|session/i;
/** Endpoints whose bodies are never stored (secret store, one-click connect, webhook config with its secret). */
export const SENSITIVE_PATH = /secret|connect|webhook|token|credential/i;

export function containsSensitiveField(value: unknown, depth = 0): boolean {
  if (depth > 20 || value === null || typeof value !== "object") return false;
  if (Array.isArray(value)) return value.some((v) => containsSensitiveField(v, depth + 1));
  return Object.entries(value as Record<string, unknown>).some(([k, v]) => SENSITIVE_NAME.test(k) || containsSensitiveField(v, depth + 1));
}

export function redactQuery(query: string): string {
  if (!query) return "";
  const qs = new URLSearchParams(query.startsWith("?") ? query.slice(1) : query);
  const out = new URLSearchParams();
  for (const [k, v] of qs) out.append(k, SENSITIVE_NAME.test(k) ? "•••" : v);
  return out.toString();
}

/** Body text that is safe to keep, or undefined. */
export function storableBody(path: string, body: string | undefined): string | undefined {
  if (!body || !body.trim() || body.length > BODY_CAP) return undefined;
  if (SENSITIVE_PATH.test(path)) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return undefined; // non-JSON bodies cannot be inspected: not stored
  }
  if (containsSensitiveField(parsed)) return undefined;
  return body;
}

export interface RecordInput {
  method: string;
  path: string;
  query: string;
  status: number | null;
  ms: number;
  op?: string;
  body?: string;
  headers?: Record<string, string>;
  at?: Date;
}

/** Pure: the entry as it will be persisted (headers dropped, query/body redacted). */
export function toHistoryEntry(input: RecordInput): HistoryEntry {
  const entry: HistoryEntry = {
    method: input.method.toUpperCase(),
    path: input.path,
    query: redactQuery(input.query),
    status: input.status,
    ms: Math.round(input.ms),
    at: (input.at ?? new Date()).toISOString(),
  };
  if (input.op) entry.op = input.op;
  const body = storableBody(input.path, input.body);
  if (body !== undefined) entry.body = body;
  return entry;
}

function sanitize(raw: unknown): HistoryEntry[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((e): e is HistoryEntry => Boolean(e) && typeof e === "object" && typeof (e as HistoryEntry).path === "string" && typeof (e as HistoryEntry).method === "string")
    .slice(0, HISTORY_CAP)
    .map((e) => toHistoryEntry({ ...e, status: typeof e.status === "number" ? e.status : null, ms: Number(e.ms) || 0, query: String(e.query ?? ""), at: Number.isNaN(Date.parse(e.at)) ? new Date(0) : new Date(e.at) }));
}

export function loadHistory(): HistoryEntry[] {
  return sanitize(readJson<unknown>(HISTORY_KEY, []));
}

export function recordHistory(input: RecordInput): HistoryEntry[] {
  const next = [toHistoryEntry(input), ...loadHistory()].slice(0, HISTORY_CAP);
  writeJson(HISTORY_KEY, next);
  return next;
}

export function clearHistory(): void {
  removeKey(HISTORY_KEY);
}
