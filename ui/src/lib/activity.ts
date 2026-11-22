/**
 * Activity feed of the live stream (GET /v1/stream, docs/ui/STREAM_API.md): what arrived in this tab since it
 * connected. Only events the server actually sent are recorded (new assignments, reviews, exports, batch results,
 * job state changes, watch alerts, resets); nothing is synthesised. Feeds the top-bar notifications and the
 * "Aktywność na żywo" panel of the dashboard.
 */
import { useSyncExternalStore } from "react";
import { readJson, writeJson } from "./storage";

export type ActivityKind = "assign" | "review" | "export" | "batch" | "job" | "watch" | "reset";

export interface ActivityEntry {
  id: string;
  /** Receive time in this tab (ms since epoch). */
  at: number;
  kind: ActivityKind;
  title: string;
  detail?: string;
  /** In-app link (route) for the entry, when there is one. */
  to?: string;
  tone?: "ok" | "info" | "warn" | "err";
}

const MAX = 60;
let entries: ActivityEntry[] = [];
let lastEventAt: number | null = null;
let seenAt: number = readJson("activity.seenAt", 0);
let counter = 0;
const listeners = new Set<() => void>();
let snapshot: { entries: ActivityEntry[]; lastEventAt: number | null; seenAt: number } = { entries, lastEventAt, seenAt };
const emit = () => {
  snapshot = { entries, lastEventAt, seenAt };
  listeners.forEach((l) => l());
};

export const activity = {
  /** Any stream message (hello, delta, job, watch, reset): updates the "last event" time. */
  touch(at: number = Date.now()) {
    lastEventAt = at;
    emit();
  },
  push(items: Omit<ActivityEntry, "id" | "at">[], at: number = Date.now()) {
    if (items.length === 0) return;
    const add = items.map((e) => ({ ...e, at, id: `${at}-${(counter += 1)}` }));
    entries = [...add.reverse(), ...entries].slice(0, MAX);
    lastEventAt = at;
    emit();
  },
  markSeen(at: number = Date.now()) {
    seenAt = at;
    writeJson("activity.seenAt", at);
    emit();
  },
  /** Test hook. */
  reset() {
    entries = [];
    lastEventAt = null;
    seenAt = 0;
    counter = 0;
    lastJobStatus.clear();
    emit();
  },
  get: () => snapshot,
  subscribe(l: () => void) {
    listeners.add(l);
    return () => { listeners.delete(l); };
  },
};

export function useActivity() {
  const s = useSyncExternalStore(activity.subscribe, activity.get, activity.get);
  return { ...s, unread: s.entries.filter((e) => e.at > s.seenAt).length };
}

/** "12 s temu", "3 min temu", "2 h temu" (Polish, relative to `now`). */
export function agoPl(at: number, now: number = Date.now()): string {
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 5) return "przed chwilą";
  if (s < 60) return `${s} s temu`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min temu`;
  return `${Math.floor(m / 60)} h temu`;
}

const DECISION: Record<string, string> = { assign: "przypisano do kampanii", new_campaign: "nowa kampania", abstain: "wstrzymano decyzję" };
const JOB: Record<string, { label: string; tone: ActivityEntry["tone"] }> = {
  queued: { label: "w kolejce", tone: "info" },
  running: { label: "w toku", tone: "info" },
  done: { label: "zakończone", tone: "ok" },
  failed: { label: "błąd", tone: "err" },
};

interface DeltaLike {
  kinds?: string[];
  n?: number;
  nodes?: { id: string; decision?: string | null; campaign_id?: string | null; new?: boolean }[];
  exports?: { total?: number } | null;
  batch?: { summary?: { items?: number; new_patterns?: number } } | null;
}

/** Entries for one `delta` message: one per new node (assignment), plus review / export / batch summaries. */
export function deltaEntries(d: DeltaLike): Omit<ActivityEntry, "id" | "at">[] {
  const out: Omit<ActivityEntry, "id" | "at">[] = [];
  for (const n of (d.nodes ?? []).filter((x) => x.new !== false).slice(0, 10)) {
    const dec = n.decision ? DECISION[n.decision] ?? n.decision : "nowy materiał";
    out.push({ kind: "assign", title: `${n.id}: ${dec}`, detail: n.campaign_id ? `kampania ${n.campaign_id}` : undefined,
      to: `/incident/${encodeURIComponent(n.id)}`, tone: n.decision === "abstain" ? "warn" : n.decision === "new_campaign" ? "info" : "ok" });
  }
  const kinds = d.kinds ?? [];
  if (kinds.includes("review")) out.push({ kind: "review", title: "Nowa decyzja analityka", detail: d.n && d.n > 1 ? `${d.n} zmian` : undefined, to: "/queue", tone: "info" });
  if (kinds.includes("export")) out.push({ kind: "export", title: "Eksport pakietu dowodowego", detail: d.exports?.total != null ? `eksportów w przebiegu: ${d.exports.total}` : undefined, tone: "info" });
  if (kinds.includes("batch")) {
    const s = d.batch?.summary;
    out.push({ kind: "batch", title: "Zakończono przypisanie partii", detail: s ? `pozycje: ${s.items ?? "—"} · nowe wzorce: ${s.new_patterns ?? 0}` : undefined, to: "/analyse", tone: "ok" });
  }
  return out;
}

const lastJobStatus = new Map<string, string>();

/** One entry per job status change (the stream sends progress up to 4/s; only transitions are recorded). */
export function jobEntry(j: { id: string; kind?: string; status?: string; done?: number; total?: number }): Omit<ActivityEntry, "id" | "at"> | null {
  const st = j.status ?? "";
  if (lastJobStatus.get(j.id) === st) return null;
  lastJobStatus.set(j.id, st);
  const s = JOB[st] ?? { label: st, tone: "info" as const };
  return { kind: "job", title: `Zadanie ${j.kind ?? ""}: ${s.label}`.replace("  ", " "), detail: `${j.id.slice(0, 8)}${j.total ? ` · ${j.done ?? 0}/${j.total}` : ""}`, tone: s.tone };
}

export function watchEntry(w: { host_defanged?: string; note_pl?: string; level?: string; campaigns?: unknown[] }): Omit<ActivityEntry, "id" | "at"> {
  return { kind: "watch", title: `Monitoring domen: ${w.host_defanged ?? "nowy alert"}`, detail: w.note_pl ?? (w.campaigns?.length ? `kandydat do kampanii (${w.campaigns.length})` : undefined), to: "/watch", tone: "warn" };
}

const MATCH_KIND_PL: Record<string, string> = {
  domain: "domena", host: "host", url: "URL", ip: "adres IP", phone: "telefon", iban: "IBAN", wallet: "portfel krypto",
  tracker: "identyfikator trackera", ad_id: "identyfikator reklamy", page_id: "identyfikator strony", keyword: "słowo kluczowe (CT)",
};

/** One entry per realtime watchlist match (`watch.match` stream event); replayed matches are labelled ODTWORZENIE. */
export function watchMatchEntry(m: {
  indicator_kind?: string;
  indicator_display?: string;
  campaign_id?: string | null;
  campaign_label?: string | null;
  source?: string;
  origin?: string;
  replay?: boolean;
  item?: { value_defanged?: string } | null;
}): Omit<ActivityEntry, "id" | "at"> {
  const kind = m.indicator_kind ? MATCH_KIND_PL[m.indicator_kind] ?? m.indicator_kind : "wskaźnik";
  const camp = m.campaign_id ? `kampania ${m.campaign_id}${m.campaign_label ? ` (${m.campaign_label.slice(0, 40)})` : ""}` : m.campaign_label ? m.campaign_label.slice(0, 40) : m.origin === "replay" ? "wpis z odtworzenia" : "wpis ręczny";
  const where = m.item?.value_defanged ? ` · ${m.item.value_defanged}` : "";
  return {
    kind: "watch",
    title: `${m.replay ? "ODTWORZENIE · " : ""}Dopasowanie watchlisty: ${kind} ${m.indicator_display ?? ""}`.trim(),
    detail: `${camp}${m.source ? ` · źródło ${m.source}` : ""}${where}`,
    to: "/watch",
    tone: m.replay ? "info" : "warn",
  };
}

/** BONUS-AUTO: one entry per finished Meta analysis that assigned a collected ad to an EXISTING campaign of the base
 * run (`meta.analysis` stream event with decision `assign`); other decisions only touch the feed. */
export function metaAnalysisEntry(m: {
  ad_id?: string;
  mode?: string;
  decision?: string | null;
  campaign_id?: string | null;
  page_name?: string | null;
  triage_priority?: string | null;
  incident_id?: string | null;
}): Omit<ActivityEntry, "id" | "at"> | null {
  if (m.decision !== "assign" || !m.campaign_id) return null;
  const tag = m.mode === "synthetic" ? "DANE SYNTETYCZNE · " : m.mode === "recorded" ? "ODTWORZENIE · " : "";
  return {
    kind: "watch",
    title: `${tag}Nowa reklama Meta powiązana z kampanią ${m.campaign_id}`,
    detail: `reklama ${m.ad_id ?? "—"}${m.page_name ? ` · ${m.page_name.slice(0, 40)}` : ""}${m.triage_priority ? ` · ocena wstępna: ${m.triage_priority}` : ""}`,
    to: `/campaigns/${encodeURIComponent(m.campaign_id)}`,
    tone: m.mode === "live" ? "warn" : "info",
  };
}
