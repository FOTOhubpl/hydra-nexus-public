/**
 * HYDRA Watch: live ticker of newly listed / discovered scam domains (CERT Polska delta, CT), scored in bits
 * with named reasons, linked to name-pattern families ("kampanie na żywo" W-…) and to analysed campaigns (C-…)
 * by the open-set assigner. Replay mode re-emits a recording at N× with a visible REPLAY badge.
 * Hosts are always defanged and never clickable; a page is fetched only on the analyst's click (server, no JS).
 *
 * Moved unchanged from WatchPage.tsx (2026-11-22): it is the "Odkrywanie domen (nagrania)" tab of /watch; the
 * default tab is the realtime engine (WatchLive.tsx).
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "../../api/client";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Panel } from "../../components/Panel";
import { StatTile } from "../../components/StatTile";
import { Sparkline } from "../../components/charts";
import { CampaignLink, Page, QueryState } from "./common";
import { polishReason } from "../../lib/displayPl";

interface Reason { code: string; bits: number; label_pl: string; detail: string; campaign_id: string | null }
interface Assignment {
  decision: string;
  campaign_id?: string | null;
  campaign_label?: string | null;
  best_candidate?: string | null;
  best_bits?: number | null;
  reason_pl?: string;
}
interface WatchEvent {
  seq: number;
  event_id: string;
  source: string;
  host_defanged: string;
  first_seen: string;
  replay: boolean;
  live_campaign: string | null;
  score: { bits: number; high: boolean; family: string; registrable: string; reasons: Reason[]; campaign_hints: string[]; brands: string[] } | null;
  assignment: Assignment | null;
  enrichment: { age_days?: number | null; ns?: string[]; stage_b?: Reason[]; fetch?: FetchResult; live?: boolean } | null;
}
interface WatchStatus {
  mode: "idle" | "live" | "replay";
  replay: { file: string; speed: number; sha256: string; events: number; emitted: number; done: boolean; window: [string, string] } | null;
  clock: string | null;
  events: number;
  high: number;
  share_high: number | null;
  a_cut_bits: number;
  in_live_campaign: number;
  assigned_known: number;
  family_link_known: number;
  campaign_gene_hits: number;
  enriched: number;
  pending: number;
  live_campaigns: number;
  live_enrichment: boolean;
  alerts?: number;
  corpus: string;
  seeded_history: number;
  campaign_index: { campaigns: number; run_id: string | null; error: string | null };
  top_families: { family: string; domains: number }[];
  top_live_campaigns: { id: string; domains: number; hosts: number; sample: string[]; keys: string[] }[];
  sparkline: { minute: string; n: number }[];
}
interface FetchResult { status: string; url_defanged: string; final_url_defanged?: string | null; hops?: { status: number; url_defanged: string }[]; white_page?: string[]; note_pl?: string }
interface Recording { file: string; lines: number; bytes: number }
interface AlertMatch { kind: string; value: string; bits: number; level: string; label_pl: string }
interface Alert { campaign_id: string; campaign_label: string; group_id: string | null; level: "campaign" | "group"; confidence: string; bits: number; campaign_bits: number; matched: AlertMatch[]; status: string }
interface AlertRow { alert_seq: number; event_id: string; source: string; host_defanged: string; first_seen: string; replay: boolean; event_bits: number | null; assignment: string | null; alerts: Alert[] }
interface LeadRow { registrable: string; watch_first_seen: string; watch_source: string; cert_listed: string; lead_h: number; via?: string }
interface AlertsPayload { fingerprints: { fingerprints: number; sha256: string | null; created_at: string; corpus: string } | null; alerts: AlertRow[]; lead_time: LeadRow[]; note_pl: string }

const SOURCE_PL: Record<string, string> = { "watch:certpl": "CERT", "watch:ct": "CT", "watch:url": "URL" };
const defangHost = (h: string) => h.replace(/\./g, "[.]");
const time = (iso: string | null | undefined) => (iso ? iso.slice(11, 19) : "—");

function useWatchStatus() {
  return useQuery({ queryKey: ["watch", "status"], queryFn: ({ signal }) => apiFetch<WatchStatus>("/v1/watch/status", { signal }), refetchInterval: 2000 });
}
function useWatchEvents() {
  return useQuery({
    queryKey: ["watch", "events"],
    queryFn: ({ signal }) => apiFetch<{ events: WatchEvent[]; replay: boolean }>("/v1/watch/events?since=0&limit=150", { signal }),
    refetchInterval: 2000,
  });
}
function useAlerts(enabled: boolean) {
  return useQuery({ queryKey: ["watch", "alerts"], queryFn: ({ signal }) => apiFetch<AlertsPayload>("/v1/watch/alerts?since=0&limit=200", { signal }), refetchInterval: 3000, enabled });
}
function useRecordings() {
  return useQuery({ queryKey: ["watch", "recordings"], queryFn: ({ signal }) => apiFetch<{ recordings: Recording[] }>("/v1/watch/recordings", { signal }) });
}

function Decision({ a, live, familyOf }: { a: Assignment | null; live: string | null; familyOf: string | null }) {
  if (!a) return <Badge variant="neutral" dot pulse>analiza…</Badge>;
  if (a.decision === "assign" && a.campaign_id) {
    return <span className="text-xs">pasuje do <CampaignLink id={a.campaign_id} label={a.campaign_label ? a.campaign_label.slice(0, 40) : null} /></span>;
  }
  if (familyOf) return <span className="text-xs">rodzina nazw jak <CampaignLink id={familyOf} /></span>;
  if (a.decision === "candidate") return <Badge variant="warning" title={a.reason_pl}>kandydat (tylko kształt nazwy)</Badge>;
  if (a.decision === "abstain") return <Badge variant="warning" dot title={a.reason_pl}>do analityka{a.best_candidate ? ` (${a.best_candidate})` : ""}</Badge>;
  if (a.decision === "unavailable") return <Badge variant="neutral" title={a.reason_pl}>brak korpusu</Badge>;
  return <Badge variant={live ? "info" : "neutral"} title={a.reason_pl}>nowa kampania</Badge>;
}

function EventRow({ e, open, onToggle }: { e: WatchEvent; open: boolean; onToggle: () => void }) {
  const s = e.score;
  const qc = useQueryClient();
  const fetchPage = useMutation({
    mutationFn: () => apiFetch<FetchResult>("/v1/watch/fetch", { method: "POST", json: { event_id: e.event_id } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watch", "events"] }),
  });
  const f = fetchPage.data ?? e.enrichment?.fetch;
  return (
    <li className="border-b border-line px-2 py-1.5 text-sm">
      <button type="button" onClick={onToggle} aria-expanded={open} className="flex w-full flex-wrap items-center gap-2 text-left">
        <span className="w-16 font-mono text-xs text-subtle">{time(e.first_seen)}</span>
        <Badge variant="neutral">{SOURCE_PL[e.source] ?? e.source}</Badge>
        <span className="min-w-0 flex-1 truncate font-mono" title={e.host_defanged}>{e.host_defanged}</span>
        <span className={`w-16 text-right font-mono text-xs ${s && s.high ? "font-semibold text-err" : "text-muted"}`} title="suma bitów (triage, nie prawdopodobieństwo)">
          {s ? `${s.bits.toFixed(1).replace(".", ",")} bit` : "—"}
        </span>
        {e.live_campaign ? <Badge variant="info" title={s?.family}>{e.live_campaign}</Badge> : null}
        <Decision a={e.assignment} live={e.live_campaign} familyOf={s?.reasons.find((r) => r.code === "campaign_family")?.campaign_id ?? null} />
      </button>
      <div className="mt-1 flex flex-wrap gap-1 pl-[4.5rem]">
        {(s?.reasons ?? []).map((r) => (
          <span key={r.code + r.detail} className={`rounded border px-1.5 py-0.5 text-[11px] ${r.bits < 0 ? "border-ok/40 text-ok" : r.campaign_id ? "border-accent/50 text-accent" : "border-line-strong text-muted"}`} title={r.detail}>
            {r.label_pl} {r.bits > 0 ? "+" : ""}{r.bits}
          </span>
        ))}
        {(e.enrichment?.stage_b ?? []).map((r) => (
          <span key={r.code} className="rounded border border-warn/40 px-1.5 py-0.5 text-[11px] text-warn" title={r.detail}>{r.label_pl}{r.bits ? ` +${r.bits}` : ""}</span>
        ))}
      </div>
      {open && (
        <div className="mt-2 space-y-1 rounded-md border border-line bg-raised p-2 pl-[4.5rem] text-xs">
          <div>Rodzina nazw: <span className="font-mono">{s?.family}</span> · domena rejestrowalna: <span className="font-mono">{defangHost(s?.registrable ?? "")}</span></div>
          {s?.reasons.map((r) => <div key={r.code + r.detail}><b>{r.label_pl}</b> ({r.bits} bit): <span className="font-mono">{r.detail}</span>{r.campaign_id ? <> → <CampaignLink id={r.campaign_id} /></> : null}</div>)}
          {e.assignment?.reason_pl && <div className="text-muted">Przypisanie (open-set): {polishReason(e.assignment.reason_pl)}</div>}
          {e.enrichment && (
            <div className="text-muted">
              Wzbogacenie ({e.enrichment.live ? "na żywo" : "z pamięci podręcznej"}): wiek domeny {e.enrichment.age_days ?? "nieznany"} dni; NS {(e.enrichment.ns ?? []).join(", ") || "brak"}
            </div>
          )}
          <div className="flex items-center gap-2 pt-1">
            <Button size="sm" onClick={() => fetchPage.mutate()} disabled={fetchPage.isPending}>Pobierz stronę na serwerze (bez JS)</Button>
            <span className="text-subtle">tylko na kliknięcie analityka; adresy nieklikalne</span>
          </div>
          {f && (
            <div className="font-mono">
              status: {f.status}{f.final_url_defanged ? ` → ${f.final_url_defanged}` : ""}{f.white_page && f.white_page.length ? ` · sygnały white-page: ${f.white_page.join(", ")}` : ""}
              {f.note_pl && <div className="font-sans text-muted">{f.note_pl}</div>}
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function AlertsPanel() {
  const q = useAlerts(true);
  const d = q.data;
  if (!d) return <QueryState query={q} what="alerty" endpoint="/v1/watch/alerts" />;
  const rows = [...d.alerts].reverse();
  return (
    <div className="space-y-3">
      <div className="rounded-md border border-info/40 bg-info/10 px-3 py-1.5 text-xs text-info">
        Alert = <b>kandydat</b>: nowa domena pasuje do zapisanego odcisku kampanii (geny operatora i niezmienniki nazwy). To nie jest potwierdzony członek kampanii — decyduje przypisanie open-set i analityk.
        {d.fingerprints && <> · odciski: {d.fingerprints.fingerprints} kampanii (korpus {d.fingerprints.corpus}), utworzone {d.fingerprints.created_at.slice(0, 16)} UTC, sha256 {d.fingerprints.sha256?.slice(0, 12)}…</>}
      </div>
      <Panel title={`Alerty (${rows.length})`} flush>
        {rows.length === 0 ? <div className="p-3 text-sm text-subtle">Brak alertów.</div> : (
          <ul>
            {rows.map((r) => {
              const a = r.alerts[0];
              if (!a) return null;
              return (
                <li key={r.alert_seq} className="border-b border-line px-2 py-1.5 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="w-16 font-mono text-xs text-subtle">{time(r.first_seen)}</span>
                    <Badge variant="neutral">{SOURCE_PL[r.source] ?? r.source}</Badge>
                    <span className="min-w-0 flex-1 truncate font-mono">{r.host_defanged}</span>
                    <Badge variant={a.level === "campaign" ? "warning" : "neutral"} title="kandydat, nie potwierdzony członek">kandydat · {a.level === "campaign" ? "kampania" : "grupa"} · {a.confidence}</Badge>
                    <span className="text-xs">pasuje do odcisku <CampaignLink id={a.campaign_id} label={a.campaign_label?.slice(0, 36)} /></span>
                    <span className="w-16 text-right font-mono text-xs">{a.bits.toFixed(1).replace(".", ",")} bit</span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1 pl-[4.5rem]">
                    {a.matched.map((m) => (
                      <span key={m.kind + m.value} className={`rounded border px-1.5 py-0.5 text-[11px] ${m.level === "campaign" ? "border-accent/50 text-accent" : "border-line-strong text-muted"}`} title={m.value}>
                        {m.label_pl} +{m.bits}
                      </span>
                    ))}
                    {r.alerts.length > 1 && <span className="text-[11px] text-subtle">także: {r.alerts.slice(1).map((x) => x.campaign_id).join(", ")}</span>}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Panel>
      <Panel title="Wyprzedzenie (Watch vs lista CERT)">
        {d.lead_time.length === 0 ? <div className="text-xs text-subtle">Brak domen widzianych przez Watch (CT) przed wpisem na listę CERT w tej sesji.</div> : (
          <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs">
            <thead><tr className="text-left text-subtle"><th>domena</th><th>Watch (źródło)</th><th>CERT</th><th className="text-right">wyprzedzenie</th></tr></thead>
            <tbody>
              {d.lead_time.map((l) => (
                <tr key={l.registrable + l.cert_listed}><td className="font-mono">{defangHost(l.registrable)}</td><td>{l.watch_first_seen.slice(0, 16)} ({SOURCE_PL[l.watch_source] ?? l.watch_source})</td><td>{String(l.cert_listed).slice(0, 16)}</td><td className="text-right font-mono">{l.lead_h > 0 ? "+" : ""}{l.lead_h} h</td></tr>
              ))}
            </tbody>
          </table></div>
        )}
      </Panel>
    </div>
  );
}

export function WatchClassic() {
  const status = useWatchStatus();
  const events = useWatchEvents();
  const recs = useRecordings();
  const qc = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);
  const [file, setFile] = useState<string>("");
  const [speed, setSpeed] = useState(60);
  const [tab, setTab] = useState<"ticker" | "alerts">(() => (new URLSearchParams(window.location.search).get("tab") === "alerts" ? "alerts" : "ticker"));
  const refresh = () => qc.invalidateQueries({ queryKey: ["watch"] });
  const replay = useMutation({ mutationFn: (b: { file: string; speed: number }) => apiFetch<WatchStatus>("/v1/watch/replay", { method: "POST", json: b }), onSuccess: refresh });
  const live = useMutation({ mutationFn: () => apiFetch<WatchStatus>("/v1/watch/live", { method: "POST" }), onSuccess: refresh });
  const stop = useMutation({ mutationFn: () => apiFetch<WatchStatus>("/v1/watch/stop", { method: "POST" }), onSuccess: refresh });
  const s = status.data;
  const files = (recs.data?.recordings ?? []).filter((r) => r.file.startsWith("certpl"));
  const chosen = file || files.find((r) => !r.file.includes("backfill"))?.file || files[0]?.file || "";
  const rows = [...(events.data?.events ?? [])].reverse();
  const spark = (s?.sparkline ?? []).map((p) => p.n);

  return (
    <Page
      title={<span className="flex items-center gap-2">Monitoring domen {s?.mode === "replay" && s.replay ? <Badge variant="warning" dot>REPLAY {s.replay.speed}×</Badge> : s?.mode === "live" ? <Badge variant="danger" dot pulse>LIVE</Badge> : <Badge variant="neutral">zatrzymany</Badge>}</span>}
      sub="nowe domeny oszustw z listy ostrzeżeń CERT Polska (i CT): ocena nazwy w bitach, rodziny nazw, przypisanie do kampanii; strony nie są pobierane automatycznie"
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <select aria-label="Nagranie" value={chosen} onChange={(e) => setFile(e.target.value)} className="h-7 rounded-[4px] border border-line bg-panel px-2 text-sm">
            {files.map((r) => <option key={r.file} value={r.file}>{r.file} ({r.lines})</option>)}
          </select>
          <select aria-label="Prędkość" value={speed} onChange={(e) => setSpeed(Number(e.target.value))} className="h-7 rounded-[4px] border border-line bg-panel px-2 text-sm">
            {[10, 60, 300, 3600].map((v) => <option key={v} value={v}>{v}×</option>)}
          </select>
          <Button variant="primary" disabled={!chosen || replay.isPending} onClick={() => replay.mutate({ file: chosen, speed })}>Odtwórz</Button>
          <Button disabled={live.isPending} onClick={() => live.mutate()}>Na żywo</Button>
          <Button disabled={stop.isPending || s?.mode === "idle"} onClick={() => stop.mutate()}>Stop</Button>
        </div>
      }
    >
      {!s ? <QueryState query={status} what="stan monitoringu domen" endpoint="/v1/watch/status" /> : (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <StatTile label="Zdarzenia" value={s.events} hint={s.clock ? `czas źródła ${time(s.clock)} UTC` : "—"} aside={<Sparkline values={spark} width={84} label="zdarzenia na minutę, ostatnie 60 min" />} />
            <StatTile label={`Wysoka ocena (≥ ${s.a_cut_bits} bit)`} value={s.high} hint={s.share_high != null ? `${Math.round(s.share_high * 100)}% zdarzeń` : "—"} />
            <StatTile label="W kampaniach na żywo" value={s.in_live_campaign} hint={`${s.live_campaigns} kampanii W-…`} />
            <StatTile label="Pasuje do znanych kampanii" value={s.assigned_known} hint={`+${s.family_link_known ?? 0} przez rodzinę nazw · korpus ${s.corpus} (${s.campaign_index.campaigns})`} />
            <StatTile label="Wzbogacone" value={s.enriched} hint={s.live_enrichment ? "RDAP/DNS/crt.sh na żywo" : "tylko pamięć podręczna"} />
            <StatTile label="Historia rodzin" value={s.seeded_history} hint="wpisy CERT sprzed startu" />
          </div>
          {s.mode === "replay" && s.replay && (
            <div className="rounded-md border border-warn/40 bg-warn/10 px-3 py-1.5 text-xs text-warn">
              REPLAY {s.replay.speed}× · {s.replay.file} · okno {s.replay.window[0].slice(0, 16)}–{s.replay.window[1].slice(11, 16)} UTC · {s.replay.emitted}/{s.replay.events} · sha256 {s.replay.sha256.slice(0, 12)}…{s.replay.done ? " · zakończono" : ""}
            </div>
          )}
          <div role="tablist" aria-label="Widok" className="flex gap-1">
            <Button variant={tab === "ticker" ? "primary" : "secondary"} size="sm" role="tab" aria-selected={tab === "ticker"} onClick={() => setTab("ticker")}>Ticker</Button>
            <Button variant={tab === "alerts" ? "primary" : "secondary"} size="sm" role="tab" aria-selected={tab === "alerts"} onClick={() => setTab("alerts")}>Alerty ({s.alerts ?? 0})</Button>
          </div>
          {tab === "alerts" ? <AlertsPanel /> : (
          <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_22rem]">
            <Panel title="Ticker" flush>
              {rows.length === 0 ? <div className="p-3 text-sm text-subtle">Brak zdarzeń. Uruchom „Na żywo” (nagrywanie na serwerze) albo „Odtwórz” nagranie.</div> : (
                <ul aria-live="polite">{rows.map((e) => <EventRow key={e.event_id} e={e} open={open === e.event_id} onToggle={() => setOpen(open === e.event_id ? null : e.event_id)} />)}</ul>
              )}
            </Panel>
            <div className="space-y-3">
              <Panel title="Kampanie na żywo (W-…)">
                {s.top_live_campaigns.length === 0 ? <div className="text-xs text-subtle">Jeszcze brak.</div> : (
                  <ul className="space-y-1.5 text-xs">
                    {s.top_live_campaigns.map((c) => (
                      <li key={c.id}>
                        <Badge variant="info">{c.id}</Badge> <b>{c.domains}</b> dom. / {c.hosts} hostów · <span className="font-mono">{c.keys.join(", ") || "wspólna domena"}</span>
                        <div className="truncate font-mono text-subtle">{c.sample.map(defangHost).join(" · ")}</div>
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>
              <Panel title="Rodziny nazw (7 dni, ≥ 3 domeny)">
                <ul className="space-y-0.5 text-xs">
                  {s.top_families.map((f) => <li key={f.family} className="flex justify-between font-mono"><span>{f.family}</span><span>{f.domains}</span></li>)}
                </ul>
              </Panel>
            </div>
          </div>
          )}
        </div>
      )}
    </Page>
  );
}
