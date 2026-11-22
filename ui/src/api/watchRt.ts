/**
 * Client for the realtime watch engine (WATCH-RT, `/v1/watch/rt/*`): source health, polled items, watchlist
 * matches, watchlist management and the replay (demo) mode. Every number shown by the page comes from these
 * endpoints or from the `watch.*` events of GET /v1/stream; nothing is synthesised client-side.
 *
 * Query keys live under ["watch-rt", ...] so the classic `watch` stream event (which invalidates ["watch"]) does not
 * refetch the realtime page.
 */
import { useQuery, type QueryClient } from "@tanstack/react-query";
import { apiFetch, isApiError } from "./client";

// ------------------------------------------------------------------ wire types

export type EngineState = "live" | "offline" | "replay" | "paused" | "stopped";

export interface ReplayInfo {
  fixture: string;
  speed: number;
  synthetic: boolean;
  label_pl?: string | null;
  ticks_total?: number | null;
  ticks_done?: number | null;
  done?: boolean;
}

export interface EngineInfo {
  state: EngineState;
  state_pl?: string | null;
  message_pl?: string | null;
  started_at?: string | null;
  now?: string | null;
  replay: ReplayInfo | null;
}

export type SourceState = "ok" | "no_new" | "idle" | "disabled" | "offline" | "backoff" | "circuit_open" | "needs_key" | "error" | "replay" | "paused";

export interface RtSource {
  id: string;
  name_pl: string;
  enabled: boolean;
  state: SourceState | string;
  state_pl?: string | null;
  interval_s: number;
  min_interval_s?: number | null;
  next_due?: string | null;
  next_due_in_s?: number | null;
  last_poll?: string | null;
  last_ok?: string | null;
  last_latency_ms?: number | null;
  last_error_pl?: string | null;
  consecutive_errors: number;
  total_polls?: number | null;
  total_items?: number | null;
  rate_limit?: { limit_per_min: number | null; used_60s: number | null } | null;
  sends_pl?: string | null;
  connector_id?: string | null;
  /** Freshness facts (backend `_freshness`): CERT file size / Last-Modified, newest entry, last new entry, 24 h. */
  file_size?: number | null;
  server_last_modified?: string | null;
  newest_entry?: string | null;
  last_new_at?: string | null;
  new_24h?: number | null;
  hourly_new?: { hour: string; new: number }[] | null;
  summary_pl?: string | null;
  state_detail_pl?: string | null;
}

export interface SparkPoint { minute: string; fetched: number; new: number; matched: number }
export interface TimelinePoint { hour: string; new: number; matched: number }

export interface RtStatus {
  engine: EngineInfo;
  sources: RtSource[];
  totals: { items: number; items_24h: number; matches: number; matches_new: number; watchlist: number };
  sparkline: SparkPoint[];
  timeline: TimelinePoint[];
}

export interface RtIndicator { kind: string; value_display: string }

export interface RtItem {
  id: number;
  source: string;
  external_id?: string | null;
  kind: "url" | "domain" | "cert" | "ad" | string;
  value_defanged: string;
  first_seen: string;
  last_seen?: string | null;
  seen_count?: number | null;
  indicators?: RtIndicator[];
  match_count: number;
  replay: boolean;
}

export interface RtAssign {
  decision: string;
  campaign_id?: string | null;
  new_pattern_id?: string | null;
  batch_run?: string | null;
  message_pl?: string | null;
}

export type MatchStatus = "new" | "acknowledged" | "dismissed";

export interface RtMatch {
  id: number;
  item_id: number;
  source: string;
  indicator_kind: string;
  indicator_display: string;
  watch_id?: number | null;
  origin: "campaign" | "manual" | "replay" | string;
  campaign_id?: string | null;
  campaign_label?: string | null;
  run_id?: string | null;
  incident_ids?: string[];
  strength: "strong" | "weak" | string;
  bits?: number | null;
  df?: number | null;
  n_incidents?: number | null;
  status: MatchStatus | string;
  created_at: string;
  updated_at?: string | null;
  evidence?: Record<string, unknown> | null;
  assign?: RtAssign | null;
  item?: { value_defanged: string; first_seen: string } | null;
  replay: boolean;
}

export interface WatchEntry {
  id: number;
  kind: string;
  value_display: string;
  origin: "campaign" | "manual" | "replay" | string;
  campaign_id?: string | null;
  campaign_label?: string | null;
  label?: string | null;
  created_at?: string | null;
  hits?: number | null;
}

export interface RtRun {
  id: number;
  source: string;
  started_at: string;
  duration_ms?: number | null;
  fetched: number;
  new: number;
  matched: number;
  http_status?: number | null;
  not_modified?: boolean;
  error_pl?: string | null;
  /** Polish answer for "Odpytaj teraz", e.g. "Sprawdzono: brak nowych wpisów (plik bez zmian, 31,6 MB)." */
  result_pl?: string | null;
  /** e.g. "plik CERT skrócony: resynchronizacja" */
  error_detail?: string | null;
  replay: boolean;
}

export interface ReplayFixture { name: string; description_pl?: string | null; ticks?: number | null; synthetic: boolean }

/** SSE payloads (GET /v1/stream). */
export interface WatchPollEvent { source: string; run_id: number; fetched: number; new: number; matched: number; duration_ms?: number | null; error_pl?: string | null; replay: boolean }

export type WatchRtEvent =
  | { type: "poll"; poll: WatchPollEvent }
  | { type: "item"; item: RtItem }
  | { type: "match"; match: RtMatch }
  | { type: "source_state"; source: RtSource };

// ------------------------------------------------------------------ labels (Polish)

export const ENGINE_LABEL: Record<EngineState, string> = {
  live: "Na żywo",
  offline: "Offline",
  replay: "Odtworzenie",
  paused: "Wstrzymany",
  stopped: "Wyłączony",
};

export const ENGINE_HELP: Record<EngineState, string> = {
  live: "Silnik odpytuje publiczne źródła zgodnie z harmonogramem; nowe pozycje trafiają do strumienia od razu.",
  offline: "Silnik działa, ale dostęp do sieci jest wyłączony (tryb offline). Źródła nie są odpytywane; widać tylko zapisane dane.",
  replay: "Odtwarzane jest nagranie lub syntetyczny zestaw demonstracyjny. To nie są dane z sieci na żywo.",
  paused: "Silnik jest wstrzymany: harmonogram nie odpytuje źródeł, dane zostają zachowane.",
  stopped: "Silnik jest wyłączony. Uruchom go, aby zacząć odpytywanie źródeł.",
};

export const SOURCE_STATE_HELP: Record<string, string> = {
  ok: "Ostatnie odpytanie zakończyło się poprawnie.",
  no_new: "Źródło odpowiada poprawnie, ale od ostatniego odpytania nie pojawiły się nowe wpisy. To normalny stan: lista nie zawsze rośnie co minutę.",
  idle: "Źródło czeka na pierwsze odpytanie.",
  disabled: "Źródło wyłączone przez analityka; nie jest odpytywane.",
  offline: "Tryb offline: żadne zapytanie nie wychodzi do sieci.",
  backoff: "Po błędach kolejne odpytanie jest odsunięte w czasie (wykładnicze opóźnienie).",
  circuit_open: "Zbyt wiele błędów z rzędu: źródło jest czasowo odcięte (bezpiecznik), aby nie obciążać serwisu.",
  needs_key: "Źródło wymaga klucza API, którego nie skonfigurowano.",
  error: "Ostatnie odpytanie zakończyło się błędem.",
  replay: "Źródło zasilane z odtwarzanego nagrania, nie z sieci.",
  paused: "Silnik wstrzymany: źródło nie jest odpytywane.",
};

export const SOURCE_SHORT: Record<string, string> = {
  "cert-pl": "CERT Polska",
  urlhaus: "URLhaus",
  openphish: "OpenPhish",
  crtsh: "crt.sh (CT)",
  "meta-adlibrary": "Biblioteka reklam Meta",
};

export const ITEM_KIND_LABEL: Record<string, string> = { url: "URL", domain: "domena", cert: "certyfikat", ad: "reklama" };

export const WATCH_KINDS = ["domain", "host", "url", "ip", "phone", "iban", "wallet", "tracker", "ad_id", "page_id", "keyword"] as const;

export const KIND_LABEL: Record<string, string> = {
  domain: "domena",
  host: "host",
  url: "URL",
  ip: "adres IP",
  phone: "telefon",
  iban: "IBAN",
  wallet: "portfel krypto",
  tracker: "identyfikator trackera",
  ad_id: "identyfikator reklamy",
  page_id: "identyfikator strony",
  keyword: "słowo kluczowe (CT)",
};

export const ORIGIN_LABEL: Record<string, string> = { campaign: "z kampanii", manual: "ręczny", replay: "odtworzenie" };

export const MATCH_STATUS_LABEL: Record<string, string> = { new: "nowe", acknowledged: "potwierdzone", dismissed: "odrzucone" };

export const STRENGTH_LABEL: Record<string, string> = { strong: "silne", weak: "słabe" };

export const kindLabel = (k: string) => KIND_LABEL[k] ?? k;
export const sourceLabel = (id: string, name?: string | null) => name || SOURCE_SHORT[id] || id;

// ------------------------------------------------------------------ helpers

/** Polish message of a failed request: `detail.message_pl`, then `detail.message`, then the generic text. */
export function errorPl(err: unknown, fallback = "Operacja nie powiodła się."): string {
  if (isApiError(err)) {
    const body = err.body as { detail?: unknown } | undefined;
    const detail = body?.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const d = detail as Record<string, unknown>;
      if (typeof d.message_pl === "string" && d.message_pl) return d.message_pl;
      if (typeof d.message === "string" && d.message) return d.message;
    }
    if (typeof detail === "string" && detail) return detail;
    if (err.status === 403) return "Brak uprawnień do tej operacji (wymagana wyższa rola).";
    if (err.isOffline) return "Serwer HYDRA jest nieosiągalny.";
    if (Array.isArray(detail)) return `Niepoprawne dane: ${err.message}`;
    return err.message || fallback;
  }
  return fallback;
}

/** "przed chwilą" / "40 s temu" / "3 min temu" / "2 h temu" / "3 dni temu" for an ISO time. */
export function relativePl(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "nigdy";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.round((now - t) / 1000);
  if (s < 0) return "za chwilę";
  if (s < 5) return "przed chwilą";
  if (s < 60) return `${s} s temu`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min temu`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h temu`;
  return `${Math.floor(h / 24)} dni temu`;
}

/** "45 s", "3 min 05 s", "1 h 02 min". */
export function durationPl(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

/** Polling interval in words: "co 30 s", "co 5 min", "co 1 h". */
export function intervalPl(sec: number | null | undefined): string {
  if (sec == null) return "—";
  if (sec < 60) return `co ${sec} s`;
  if (sec < 3600) return `co ${Math.round(sec / 60)} min`;
  return `co ${Math.round(sec / 360) / 10} h`.replace(".", ",");
}

/** File size in Polish units: "31,6 MB", "12,3 kB". */
export function bytesPl(n: number | null | undefined): string {
  if (!n) return "—";
  if (n >= 100_000) return `${(n / 1e6).toFixed(1).replace(".", ",")} MB`;
  return `${(n / 1e3).toFixed(1).replace(".", ",")} kB`;
}

export function timePl(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("pl-PL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ------------------------------------------------------------------ requests

const BASE = "/v1/watch/rt";

export interface ItemFilters { source?: string; kind?: string; matched?: "" | "1" | "0"; q?: string }

export function itemsPath(f: ItemFilters, beforeId?: number | null, limit = 200): string {
  const p = new URLSearchParams();
  if (f.source) p.set("source", f.source);
  if (f.kind) p.set("kind", f.kind);
  if (f.matched) p.set("matched", f.matched);
  if (f.q) p.set("q", f.q);
  if (beforeId != null) p.set("before_id", String(beforeId));
  p.set("limit", String(limit));
  return `${BASE}/items?${p.toString()}`;
}

export const rtKeys = {
  all: ["watch-rt"] as const,
  status: ["watch-rt", "status"] as const,
  items: (f: ItemFilters) => ["watch-rt", "items", f] as const,
  matches: (status: string) => ["watch-rt", "matches", status] as const,
  watchlist: (kind: string, origin: string, q: string) => ["watch-rt", "watchlist", kind, origin, q] as const,
  fixtures: ["watch-rt", "fixtures"] as const,
  runs: (source: string) => ["watch-rt", "runs", source] as const,
};

export const rtApi = {
  status: (signal?: AbortSignal) => apiFetch<RtStatus>(`${BASE}/status`, { signal }),
  items: (f: ItemFilters, beforeId?: number | null, signal?: AbortSignal) =>
    apiFetch<{ items: RtItem[]; next_before: number | null }>(itemsPath(f, beforeId), { signal }),
  matches: (status: string, signal?: AbortSignal) =>
    apiFetch<{ matches: RtMatch[] }>(`${BASE}/matches?${status ? `status=${encodeURIComponent(status)}&` : ""}limit=200`, { signal }),
  setMatch: (id: number, status: MatchStatus) => apiFetch<RtMatch>(`${BASE}/matches/${id}`, { method: "POST", json: { status } }),
  incident: (id: number) => apiFetch<{ match: RtMatch; assign: RtAssign | null }>(`${BASE}/matches/${id}/incident`, { method: "POST" }),
  setSource: (id: string, body: { enabled?: boolean; interval_s?: number }) =>
    apiFetch<RtSource>(`${BASE}/sources/${encodeURIComponent(id)}`, { method: "POST", json: body }),
  poll: (id: string) => apiFetch<{ run: RtRun }>(`${BASE}/sources/${encodeURIComponent(id)}/poll`, { method: "POST" }),
  engine: (action: "start" | "pause" | "resume" | "stop") => apiFetch<RtStatus>(`${BASE}/engine`, { method: "POST", json: { action } }),
  watchlist: (kind: string, origin: string, q: string, signal?: AbortSignal) => {
    const p = new URLSearchParams();
    if (kind) p.set("kind", kind);
    if (origin) p.set("origin", origin);
    if (q) p.set("q", q);
    const qs = p.toString();
    return apiFetch<{ entries: WatchEntry[]; counts: Record<string, number> }>(`${BASE}/watchlist${qs ? `?${qs}` : ""}`, { signal });
  },
  addWatch: (body: { kind: string; value: string; label?: string }) => apiFetch<WatchEntry>(`${BASE}/watchlist`, { method: "POST", json: body }),
  deleteWatch: (id: number) => apiFetch<unknown>(`${BASE}/watchlist/${id}`, { method: "DELETE" }),
  rebuild: () => apiFetch<{ added: number; removed: number; total: number }>(`${BASE}/watchlist/rebuild`, { method: "POST" }),
  fixtures: (signal?: AbortSignal) => apiFetch<{ fixtures: ReplayFixture[] }>(`${BASE}/replay/fixtures`, { signal }),
  replay: (fixture: string, speed: number) => apiFetch<RtStatus>(`${BASE}/replay`, { method: "POST", json: { fixture, speed } }),
  replayStop: () => apiFetch<RtStatus>(`${BASE}/replay/stop`, { method: "POST" }),
};

export function useRtStatus() {
  return useQuery({ queryKey: rtKeys.status, queryFn: ({ signal }) => rtApi.status(signal), refetchInterval: 4000 });
}

export function useRtMatches(status: string) {
  return useQuery({ queryKey: rtKeys.matches(status), queryFn: ({ signal }) => rtApi.matches(status, signal), refetchInterval: 10_000 });
}

export function useRtWatchlist(kind: string, origin: string, q: string) {
  return useQuery({ queryKey: rtKeys.watchlist(kind, origin, q), queryFn: ({ signal }) => rtApi.watchlist(kind, origin, q, signal) });
}

export function useRtFixtures() {
  return useQuery({ queryKey: rtKeys.fixtures, queryFn: ({ signal }) => rtApi.fixtures(signal), staleTime: 60_000 });
}

/** Replace one source in the cached status (watch.source_state event, POST /sources/{id} response). */
export function patchStatusSource(client: QueryClient, src: RtSource) {
  if (!src || typeof src.id !== "string") return;
  client.setQueryData<RtStatus>(rtKeys.status, (prev) => {
    if (!prev) return prev;
    const i = prev.sources.findIndex((s) => s.id === src.id);
    const sources = i < 0 ? [...prev.sources, src] : prev.sources.map((s, j) => (j === i ? src : s));
    return { ...prev, sources };
  });
}

// ------------------------------------------------------------------ event bus (fed by api/stream.ts)

const rtListeners = new Set<(e: WatchRtEvent) => void>();

export const watchRtBus = {
  emit(e: WatchRtEvent) {
    rtListeners.forEach((l) => l(e));
  },
  subscribe(l: (e: WatchRtEvent) => void) {
    rtListeners.add(l);
    return () => { rtListeners.delete(l); };
  },
};
