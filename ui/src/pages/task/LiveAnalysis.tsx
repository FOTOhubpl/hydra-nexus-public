/**
 * Full-screen live analysis of one new item (the demo hero). Two real sources, polled every second:
 * GET /v1/analyse/{id} (job, assignment, live rows) and GET /v1/analyse/{id}/explain-data (panels built from the
 * stored genes, pairs and assignment). Panels whose rows do not exist yet show "w toku"; durations come from row
 * timestamps; nothing is estimated.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { artifactContentUrl } from "../../api/client";
import { FAMILIES, useAnalyseResult, useExplainData, usePair, type ExplainData } from "../../api/task";
import { AiAnalystPanel } from "../../components/AiAnalystPanel";
import { Timeline } from "../../components/charts";
import { ConformalList, DecisionRule } from "../../components/DecisionRule";
import { DecisionBadge, FamChip, GeneValue, HypothesisBadge, ProbBar, Prov, Thumb, Waterfall } from "../../components/evidence";
import { RelationGraph } from "../../components/RelationGraph";
import { RobustnessBadge } from "../../components/RobustnessBadge";
import { cx } from "../../lib/cx";
import { familyColors, familyLabel, formatBits, formatProbability, plNumber, ROLE_LABEL } from "../../lib/evidence";
import { formatBytes, formatCount } from "../../lib/format";
import { useTheme } from "../../lib/theme";
import { useReducedMotion } from "../../lib/media";
import { pairPath } from "./common";
import { polishReason } from "../../lib/displayPl";
import { LabLink, topPair } from "../../components/LabLink";

function Spinner() {
  return <span aria-hidden="true" className="inline-block size-3.5 animate-spin rounded-full border-2 border-accent border-t-transparent" />;
}

function Box({ title, sub, children, className, pending }: { title: string; sub?: ReactNode; children: ReactNode; className?: string; pending?: boolean }) {
  return (
    <section className={cx("surface-card flex min-w-0 flex-col p-4", className)}>
      <header className="mb-3 flex items-baseline gap-2">
        <h3 className="shrink-0 text-sm font-semibold">{title}</h3>
        {sub && <span className="min-w-0 truncate text-xs text-subtle">{sub}</span>}
        {pending && <span className="ml-auto inline-flex items-center gap-1.5 text-xs text-accent"><Spinner />w toku</span>}
      </header>
      <div className="min-h-0 flex-1 text-sm">{children}</div>
    </section>
  );
}

const LEVEL_LABEL: Record<string, string> = { campaign: "operator", asset: "zasób", kit: "kit", none: "boilerplate", unknown: "?" };
const LEVEL_CLS: Record<string, string> = {
  campaign: "border-for bg-for-soft text-fg", asset: "border-fam-vis text-fg", kit: "border-fam-meta text-fg",
  none: "border-line text-subtle line-through", unknown: "border-line text-subtle",
};
const TIER_LABEL: Record<string, string> = { operator: "operator", platform: "platforma", kit: "kit", boilerplate: "boilerplate", asset: "zasób", persona: "persona", phrase: "fraza", label: "etykieta" };

function StageTimeline({ x }: { x: ExplainData | undefined }) {
  const stages = x?.stages ?? [
    { key: "import", label: "import + artefakty", seconds: null, state: "running" as const },
    { key: "genes", label: "geny", seconds: null, state: "waiting" as const },
    { key: "enrich", label: "wzbogacanie", seconds: null, state: "waiting" as const },
    { key: "assign", label: "kandydaci + ocena + decyzja", seconds: null, state: "waiting" as const },
  ];
  const total = stages.reduce((s, st) => s + (st.seconds ?? 0), 0) || 1;
  return (
    <div className="space-y-2">
      <div className="flex h-9 gap-[2px] overflow-hidden rounded-md">
        {stages.map((st) => (
          <div key={st.key} title={`${st.label}: ${st.seconds === null ? "—" : `${plNumber(st.seconds, 2)} s`}`}
            className={cx("flex min-w-[120px] items-center gap-2 px-3 text-xs", st.state === "done" ? "bg-for text-canvas" : st.state === "running" ? "bg-for-soft text-fg" : st.state === "skipped" ? "bg-hover text-subtle" : "bg-hover text-subtle")}
            style={{ flexGrow: st.seconds ? Math.max(0.6, st.seconds / total) : 0.6 }}>
            {st.state === "running" && <Spinner />}
            <span className="truncate font-medium">{st.label}</span>
            <span className="ml-auto font-mono">{st.seconds === null ? (st.state === "skipped" ? "pominięto" : "") : `${plNumber(st.seconds, 2)} s`}</span>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        {Object.entries(x?.per_family_seconds ?? {}).map(([f, s]) => <span key={f}><FamChip family={f} /> {s === null ? "—" : `${plNumber(s, 2)} s`}</span>)}
        {x?.timing_source && <span className="text-subtle">czasy: {x.timing_source}</span>}
      </div>
    </div>
  );
}

function Funnel({ f }: { f: NonNullable<ExplainData["candidates"]>["funnel"] }) {
  const steps: [string, number][] = [["incydenty w korpusie", f.incidents], ["kandydaci", f.candidates], ["ocenione bitami", f.scored], ["powiązane (ta sama kampania)", f.linked]];
  const max = Math.max(1, f.incidents);
  return (
    <div className="space-y-1.5">
      {steps.map(([l, v]) => (
        <div key={l} className="grid grid-cols-[170px_1fr_40px] items-center gap-2 text-xs">
          <span className="text-muted">{l}</span>
          <span className="flex justify-center"><span className="h-5 rounded-[3px] bg-accent" style={{ width: `${Math.max(2, (100 * v) / max)}%` }} /></span>
          <span className="text-right font-mono">{v}</span>
        </div>
      ))}
    </div>
  );
}

/** Top-3 candidate campaigns: campaign-level bits by family, plus kit and asset, with the link threshold. */
function WhyBars({ top, link }: { top: NonNullable<ExplainData["why"]>["top"]; link: number | null }) {
  const theme = useTheme();
  const fam = familyColors(theme);
  const rows = top.map((t) => {
    const byFam = Object.entries(t.campaign_by_family ?? {}).filter(([, v]) => v > 0);
    const famSum = byFam.reduce((s, [, v]) => s + v, 0);
    const camp = Math.max(famSum, t.campaign_bits ?? 0);
    const rest = camp - famSum; // campaign-level bits not attributed to one family (e.g. identical file)
    return { t, byFam: rest > 0.05 ? [...byFam, ["_rest", rest] as [string, number]] : byFam, kit: t.kit_bits ?? 0, asset: t.asset_bits ?? 0, camp };
  });
  const max = Math.max(link ?? 0, 1, ...rows.map((r) => Math.max(r.camp, r.kit, r.asset, r.t.support ?? 0)));
  const x = (v: number) => `${(100 * v) / max}%`;
  return (
    <div className="space-y-4">
      {rows.map(({ t, byFam, kit, asset, camp }, i) => (
        <div key={t.campaign_id} className="space-y-1">
          <div className="flex items-center gap-2 text-xs"><span className="font-mono text-sm text-fg">#{i + 1} {t.campaign_id}</span><span className="text-muted">wsparcie {t.support === null || t.support === undefined ? "—" : formatBits(t.support)} · wynik nieskalibrowany {formatProbability(t.probability)}{t.pair ? ` · para ${t.pair[0]} ↔ ${t.pair[1]}` : ""}</span></div>
          {[["kampania / operator", camp, byFam.length ? null : "var(--color-for)"], ["kit / szablon", kit, "var(--color-fam-meta)"], ["wspólny zasób", asset, "var(--color-fam-vis)"]].map(([label, v, color]) => (
            <div key={String(label)} className="relative grid grid-cols-[130px_1fr_60px] items-center gap-2 text-xs">
              <span className="text-muted">{label}</span>
              <span className="relative h-3.5">
                {label === "kampania / operator" && byFam.length ? (
                  <span className="absolute inset-y-0 left-0 flex gap-[2px]" style={{ width: x(camp) }}>
                    {byFam.map(([f, b]) => <span key={f} title={f === "_rest" ? `bity kampanii spoza jednej rodziny (np. identyczny plik): ${plNumber(b, 1)} b` : `${f} ${familyLabel(f)}: ${plNumber(b, 1)} b`} style={{ flexGrow: b, backgroundColor: f === "_rest" ? "var(--color-for)" : fam[f] }} className="rounded-[2px]" />)}
                  </span>
                ) : <span className="absolute inset-y-0 left-0 rounded-r-[3px]" style={{ width: x(Number(v)), backgroundColor: String(color) }} />}
                {link !== null && label === "kampania / operator" && <span className="absolute -top-1 -bottom-1 w-[2px] bg-fg" style={{ left: x(link) }} title={`próg kampanii ${link} b`} />}
              </span>
              <span className="text-right font-mono">{plNumber(Number(v), 1)} b</span>
            </div>
          ))}
        </div>
      ))}
      <div className="flex flex-wrap gap-3 text-xs text-muted">
        {FAMILIES.map((f) => <span key={f} className="inline-flex items-center gap-1"><span className="inline-block size-2.5 rounded-sm" style={{ backgroundColor: fam[f] }} />{f}</span>)}
        <span className="inline-flex items-center gap-1"><span className="inline-block size-2.5 rounded-sm bg-for" />poza rodziną (np. identyczny plik)</span>
        {link !== null && <span className="inline-flex items-center gap-1"><span className="inline-block h-3 w-[2px] bg-fg" />próg kampanii {link} b</span>}
      </div>
    </div>
  );
}

function Profile({ p }: { p: NonNullable<ExplainData["profile"]> }) {
  const max = Math.max(1, ...Object.values(p.new_item), ...Object.values(p.campaign_core));
  return (
    <div className="space-y-1.5">
      <div className="flex gap-4 text-xs text-muted"><span className="inline-flex items-center gap-1.5"><span className="inline-block size-2.5 rounded-sm bg-accent" />nowy materiał: wspólne rzadkie geny</span><span className="inline-flex items-center gap-1.5"><span className="inline-block size-2.5 rounded-sm bg-muted" />genom rdzenia {p.campaign_id ?? "kampanii"}</span></div>
      {p.families.map((f) => (
        <div key={f} className="grid grid-cols-[110px_1fr] items-center gap-2 text-xs">
          <FamChip family={f} withName />
          <div className="space-y-[2px]">
            {[["bg-accent", p.new_item[f] ?? 0], ["bg-muted", p.campaign_core[f] ?? 0]].map(([cls, v], i) => (
              <div key={i} className="flex items-center gap-2"><span className={cx("h-2 rounded-r-[3px]", String(cls))} style={{ width: `${Math.max(0.5, (100 * Number(v)) / max)}%` }} /><span className="font-mono text-muted">{Number(v)}</span></div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function BestPairWaterfall({ pair }: { pair: [string, string] }) {
  const q = usePair(pair[0], pair[1]);
  const s = q.data?.score;
  if (!s) return <div className="text-xs text-subtle">{q.isError ? "Para nie jest dostępna w bieżącym przebiegu." : "Ładowanie rachunku bitów…"}</div>;
  return <Waterfall familyBits={s.family_bits} totalBits={s.total_bits} missing={Array.from(new Set([...(s.unknown_families ?? []), ...s.missing_families]))} reasons={s.reasons} contradictions={s.contradictions} />;
}

export function LiveAnalysis({ jobId, modeLabel, onClose, activity }: { jobId: string; modeLabel: string; onClose: () => void; activity?: ReactNode }) {
  const q = useAnalyseResult(jobId);
  const status = q.data?.job.status ?? "queued";
  const finished = status === "done" || status === "failed";
  const xq = useExplainData(jobId, finished);
  const x = xq.data;
  const theme = useTheme();
  const fam = familyColors(theme);
  const [, setTick] = useState(0);
  const [revealed, setRevealed] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((v) => v + 1), 500);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => { clearInterval(t); window.removeEventListener("keydown", onKey); };
  }, [onClose]);
  const edgesAll = useMemo(() => [...(x?.graph?.edges ?? [])].sort((a, b) => b.bits - a.bits), [x?.graph?.edges]);
  const reducedMotion = useReducedMotion();
  useEffect(() => {
    if (!edgesAll.length) return;
    // reduced motion: all edges at once, no staged reveal
    if (reducedMotion) { setRevealed(edgesAll.length); return; }
    const t = setInterval(() => setRevealed((n) => (n >= edgesAll.length ? n : n + 1)), 300);
    return () => clearInterval(t);
  }, [edgesAll.length, reducedMotion]);

  const job = q.data?.job;
  const a = q.data?.assignment ?? null;
  const isHint = q.data?.result?.kind === "hint";
  const started = typeof job?.started_at === "string" ? Date.parse(job.started_at) : NaN;
  const elapsed = Number.isNaN(started) ? null : ((typeof job?.finished_at === "string" ? Date.parse(job.finished_at) : Date.now()) - started) / 1000;
  const waiting = !finished;
  const dec = x?.decision?.decision ?? a?.decision ?? null;
  const graphNodes = x?.incident_id ? [{ id: x.incident_id, label: x.incident_id, group: null, thumbnail: x.thumbnail ?? null, title: x.title ?? null },
    ...edgesAll.map((e) => ({ id: e.other, label: e.other, group: null, thumbnail: null, title: null }))] : [];
  const graphEdges = edgesAll.slice(0, revealed).filter((e) => e.bits > 0).map((e) => ({ id: `${x?.incident_id}|${e.other}`, source: x!.incident_id!, target: e.other, bits: e.bits, family: e.family, dashed: e.hypothesis !== "same_campaign", faded: e.bits < (x?.why?.link_threshold ?? 10) }));
  // decision rule state: Assignment.meta of the job; explain-data fills the conformal set / margin on older payloads
  const ruleMeta = a?.meta ? { ...a.meta, conformal: a.meta.conformal ?? x?.why?.conformal ?? null, margin_bits: a.meta.margin_bits ?? x?.why?.margin ?? null } : null;
  const exportJson = () => {
    const blob = new Blob([JSON.stringify({ job: q.data?.job, assignment: a, explain: x }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const el = document.createElement("a");
    el.href = url;
    el.download = `analiza-${jobId}.json`;
    el.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="fixed inset-0 z-50 flex flex-col overflow-hidden bg-canvas" role="dialog" aria-label="Analiza na żywo">
      <header className="flex items-center gap-4 border-b border-line bg-panel px-8 py-3">
        <h2 className="text-2xl font-semibold tracking-tight">Analiza na żywo</h2>
        <span className="rounded-md border border-line px-2 py-0.5 text-sm">{modeLabel}</span>
        <span className="font-mono text-sm text-subtle">zadanie {jobId}{x?.incident_id ? ` · ${x.incident_id}` : ""}</span>
        <span className="ml-auto inline-flex items-center gap-2 text-sm">
          {waiting && <Spinner />}
          <span className={status === "failed" ? "text-err" : status === "done" ? "text-ok" : "text-accent"}>{status === "queued" ? "w kolejce" : status === "running" ? "przetwarzanie" : status === "done" ? "zakończone" : "błąd"}</span>
          {elapsed !== null && <span className="font-mono text-muted">· {plNumber(elapsed, 1)} s (zmierzone)</span>}
        </span>
        <button type="button" onClick={onClose} className="h-9 rounded-md border border-line-strong px-3 text-sm hover:bg-hover">Zamknij (Esc)</button>
      </header>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-8 py-5">
        {activity}
        <StageTimeline x={x} />

        {status === "failed" && <div className="rounded-md border border-err bg-against-soft p-3 text-sm"><b>{String(job?.error_code ?? "błąd")}</b> <span className="font-mono break-all">{String(job?.error ?? "")}</span></div>}
        {isHint && q.data?.result?.search && (
          <Box title="Wskazówka: wyszukiwanie w korpusie" sub="wskazówka nie tworzy incydentu">
            <div className="grid gap-4 md:grid-cols-2">
              <ul className="space-y-1 text-sm">{(q.data.result.search.campaigns ?? []).map((c) => <li key={c.campaign_id}><Link className="font-mono text-accent hover:underline" to={`/campaigns/${encodeURIComponent(c.campaign_id)}`}>{c.campaign_id}</Link> <span className="text-muted">{c.label}</span> <span className="font-mono text-subtle">wynik {plNumber(c.score, 1)}</span></li>)}</ul>
              <ul className="space-y-1 text-xs">{(q.data.result.search.incidents ?? []).map((c) => <li key={c.incident_id}><span className="font-mono">{c.incident_id}</span> {(c.matches ?? []).slice(0, 3).map((m) => `${m.kind}: „${m.value.slice(0, 40)}” ${formatBits(m.bits)}`).join(" · ")}</li>)}</ul>
            </div>
            {(q.data.result.search.incidents ?? []).length === 0 && <div className="text-sm text-subtle">Brak trafień w genach korpusu.</div>}
          </Box>
        )}

        {!isHint && (
          <>
            <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
              <Box title="Artefakty" sub="rola, typ wykryty z bajtów, rozmiar, sha256, duplikaty, ukryte sekrety" pending={!x?.artifacts?.length && waiting}>
                {x?.artifacts?.length ? (
                  <div className="space-y-2">
                    {x.artifacts.slice(0, 12).map((f) => (
                      <div key={f.artifact_id + f.path} className="flex items-center gap-3">
                        {f.previewable && f.mime?.startsWith("image/") ? <img src={artifactContentUrl(f.artifact_id)} alt={f.path} className="size-12 rounded-md object-cover" /> : <span className="flex size-12 items-center justify-center rounded-md bg-hover font-mono text-2xs text-subtle">{(f.mime ?? "?").split("/")[1]?.slice(0, 6)}</span>}
                        <div className="min-w-0 flex-1">
                          <div className="truncate font-mono text-xs text-fg" title={f.path}>{f.path}</div>
                          <div className="text-xs text-muted">{ROLE_LABEL[f.role] ?? f.role} · {f.mime ?? "?"} · {formatBytes(f.size)} · <span className="font-mono">sha256 {f.sha_short}…</span></div>
                        </div>
                        {f.duplicate_of.length > 0 && <span className="rounded-md border border-abstain bg-abstain-soft px-2 py-0.5 text-xs text-abstain-ink" title={f.duplicate_of.join(", ")}>duplikat: {f.duplicate_of.slice(0, 3).join(", ")}</span>}
                        {f.redactions > 0 && <span className="rounded-md border border-line px-2 py-0.5 text-xs">ukryto {f.redactions}</span>}
                      </div>
                    ))}
                    {x.artifacts.length > 12 && <div className="text-xs text-subtle">… oraz {x.artifacts.length - 12} kolejnych plików</div>}
                  </div>
                ) : <div className="text-xs text-subtle">Zapis plików (SHA-256), rozpoznanie ról…</div>}
              </Box>
              <Box title="Geny" sub="najbardziej informatywne najpierw: rzadkie wspólne, potem zasoby, kit, szum" pending={waiting && !(x?.genes_total)}>
                <div className="mb-3 grid grid-cols-6 gap-2">
                  {FAMILIES.map((f) => {
                    const n = Object.values(x?.metrics?.coverage?.[f] ?? {}).reduce((s, v) => s + v, 0);
                    return <div key={f} className="rounded-md border border-line px-2 py-1.5"><div className="flex items-center gap-1 text-xs"><span className="size-2 rounded-sm" style={{ backgroundColor: fam[f] }} />{f}</div><div className="font-mono text-lg">{n ? formatCount(n) : waiting ? "…" : "—"}</div></div>;
                  })}
                </div>
                <ul className="max-h-[300px] space-y-1 overflow-y-auto">
                  {(x?.genes_feed ?? []).slice(0, 18).map((g, i) => (
                    <li key={i} className="flex items-center gap-2 text-xs">
                      <Prov value={g.provenance} />
                      <FamChip family={g.family} />
                      <span className="w-28 shrink-0 truncate text-muted">{g.kind}</span>
                      <span className="min-w-0 flex-1 truncate"><GeneValue value={g.value} max={42} /></span>
                      <span className={cx("shrink-0 rounded-md border px-1.5 text-2xs", LEVEL_CLS[g.level] ?? LEVEL_CLS.unknown)} title={g.tier ? `tier: ${TIER_LABEL[g.tier] ?? g.tier}` : undefined}>{LEVEL_LABEL[g.level] ?? g.level}</span>
                      <span className="w-16 shrink-0 text-right font-mono">{g.bits !== null ? formatBits(g.bits) : g.df_other ? `df ${g.df_other}` : "—"}</span>
                      <span className="hidden w-[300px] shrink-0 truncate text-subtle 2xl:inline" title={g.why}>{g.why}</span>
                    </li>
                  ))}
                </ul>
              </Box>
            </div>

            <div className="grid gap-4 xl:grid-cols-3">
              <Box title="Wzbogacanie" sub="fakty z publicznych źródeł" pending={waiting && !x?.enrichment?.length}>
                {x?.enrichment?.length ? (
                  <ul className="space-y-1 text-xs">
                    {x.enrichment.slice(0, 10).map((e, i) => (
                      <li key={i} className="flex items-center gap-2"><Prov value="OBS" /><span className="w-32 shrink-0 truncate text-muted">{e.kind}</span><span className="min-w-0 flex-1 truncate font-mono">{e.value}</span><span className="shrink-0 text-subtle" title={e.origin}>{e.subject ?? ""}{e.fetched_at ? ` · ${e.fetched_at}` : " · cache"}</span></li>
                    ))}
                  </ul>
                ) : <div className="text-xs text-subtle">{waiting ? "RDAP / DNS / CT / listy ostrzeżeń…" : "Brak faktów wzbogacenia (tryb offline lub brak domen)."}</div>}
              </Box>
              <Box title="Kandydaci" sub="który indeks zaproponował parę" pending={waiting && !x?.candidates?.pairs.length}>
                {x?.candidates?.pairs.length ? (
                  <div className="space-y-3">
                    <Funnel f={x.candidates.funnel} />
                    <div className="flex flex-wrap gap-2 text-xs">{Object.entries(x.candidates.by_index).sort((p, q2) => q2[1] - p[1]).map(([k, v]) => <span key={k} className="rounded-md border border-line px-2 py-0.5"><span className="font-mono">{k}</span> {v}</span>)}</div>
                  </div>
                ) : <div className="text-xs text-subtle">Indeksy kandydatów: dokładne geny, bliskie skróty percepcyjne (near-hash), kNN wektorów.</div>}
              </Box>
              <Box title="Metryki" sub="tego materiału">
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                  <dt className="text-muted">genów</dt><dd className="font-mono">{formatCount(x?.metrics?.genes_total ?? 0)}</dd>
                  <dt className="text-muted">rzadkich wspólnych</dt><dd className="font-mono">{x?.metrics?.rare_genes ?? "—"}</dd>
                  <dt className="text-muted">faktów wzbogacenia</dt><dd className="font-mono">{x?.metrics?.enrichment_facts ?? "—"}</dd>
                  <dt className="text-muted">ocenionych kandydatów</dt><dd className="font-mono">{x?.metrics?.candidates_scored ?? "—"}</dd>
                  <dt className="text-muted">czas całkowity</dt><dd className="font-mono">{x?.metrics?.total_seconds === null || x?.metrics?.total_seconds === undefined ? "—" : `${plNumber(x.metrics.total_seconds, 2)} s`}</dd>
                  <dt className="text-muted">cache ekstrakcji</dt><dd className="font-mono">{x?.metrics?.cache === null || x?.metrics?.cache === undefined ? "nieznane" : x.metrics.cache ? "tak" : "nie"}</dd>
                  <dt className="text-muted">rodziny zaobserwowane</dt><dd className="font-mono">{FAMILIES.filter((f) => (x?.metrics?.coverage?.[f]?.observed ?? 0) > 0).join(" ") || "—"}</dd>
                </dl>
              </Box>
            </div>

            {(dec || x?.why?.top.length) && (
              <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
                <Box title="Dlaczego tak" sub="bity na poziomie kampanii / kitu / zasobu dla 3 najbliższych kampanii; kreska = próg kampanii">
                  {x?.why?.top.length ? <WhyBars top={x.why.top} link={x.why.link_threshold} /> : <div className="text-xs text-subtle">Brak kandydatów z bitami.</div>}
                  <div className="mt-4 grid gap-1 text-sm">
                    {x?.why?.margin !== null && x?.why?.margin !== undefined && <div>Przewaga #1 nad #2: <b className="font-mono">{formatBits(x.why.margin)}</b></div>}
                    {x?.why?.counterfactual && <div>Kontrfakt: <b>{x.why.counterfactual}</b></div>}
                    {x?.why?.robustness && <div className="flex items-center gap-2">Odporność najlepszej pary: <RobustnessBadge r={x.why.robustness} /></div>}
                    {!ruleMeta && x?.why?.conformal && <ConformalList c={x.why.conformal} className="mt-1" />}
                  </div>
                  <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm">{(x?.why?.rationale_pl ?? []).map((r, i) => <li key={i}>{r}</li>)}</ol>
                </Box>
                <div className="flex min-w-0 flex-col gap-4">
                  <Box title="Rachunek bitów najlepszej pary" sub={x?.why?.best_pair ? `${x.why.best_pair[0]} ↔ ${x.why.best_pair[1]}` : undefined}>
                    {x?.why?.best_pair ? <BestPairWaterfall pair={x.why.best_pair} /> : <div className="text-xs text-subtle">Brak pary z dowodami.</div>}
                  </Box>
                  {x?.why?.best_pair && <AiAnalystPanel kind="pair" a={x.why.best_pair[0]} b={x.why.best_pair[1]} />}
                </div>
              </div>
            )}

            {dec && (
              <section className="grid gap-6 rounded-lg border-2 border-fg bg-panel p-6 xl:grid-cols-[auto_1fr_auto]">
                <Thumb artifactId={x?.thumbnail} alt="nowy materiał" size={160} />
                <div className="min-w-0 space-y-2">
                  <DecisionBadge value={dec} large />
                  {x?.decision?.group_id && <div className="text-sm">w grupie kampanii <span className="font-mono">{x.decision.group_id}</span></div>}
                  <div className="text-sm text-muted [overflow-wrap:anywhere]">{polishReason(x?.decision?.reason_pl ?? a?.reason_pl)}</div>
                  <DecisionRule meta={ruleMeta} bestPair={topPair(a)} className="max-w-2xl" />
                  <LabLink incidentId={a?.incident_id ?? x?.incident_id} />
                  {x?.decision?.probability !== null && x?.decision?.probability !== undefined && <div className="max-w-xl"><ProbBar p={x.decision.probability} calibrated={false} /></div>}
                </div>
                <div className="flex flex-col gap-2">
                  {x?.decision?.campaign_id && <Link to={`/campaigns/${encodeURIComponent(x.decision.campaign_id)}`} className="inline-flex h-9 items-center rounded-md bg-fg px-3 text-sm text-canvas">Otwórz kampanię</Link>}
                  {x?.why?.best_pair && <Link to={pairPath(x.why.best_pair[0], x.why.best_pair[1])} className="inline-flex h-9 items-center rounded-md border border-line-strong px-3 text-sm hover:bg-hover">Dowód pary</Link>}
                  <Link to="/queue" className="inline-flex h-9 items-center rounded-md border border-line-strong px-3 text-sm hover:bg-hover">Kolejka analityka</Link>
                  <button type="button" onClick={exportJson} className="inline-flex h-9 items-center rounded-md border border-line-strong px-3 text-sm hover:bg-hover">Eksport dowodów (JSON)</button>
                </div>
              </section>
            )}

            <div className="grid gap-4 xl:grid-cols-3">
              <Box title="Profil rodzin" sub="rzadkie wspólne geny nowego materiału vs lista genomu rdzenia kampanii" pending={waiting && !x?.profile}>
                {x?.profile ? <Profile p={x.profile} /> : <div className="text-xs text-subtle">—</div>}
              </Box>
              <Box title="Nowy węzeł w grafie" sub="krawędzie do korpusu od najsilniejszej" pending={waiting && !edgesAll.length}>
                <div className="h-64 overflow-hidden rounded-md border border-line">
                  {graphNodes.length > 1 ? <RelationGraph compact nodes={graphNodes} edges={graphEdges} campaignColors={{}} /> : <div className="p-4 text-xs text-subtle">Krawędzie pojawią się po ocenie par.</div>}
                </div>
                <div className="mt-1 text-xs text-subtle">{edgesAll.length ? `${Math.min(revealed, edgesAll.length)}/${edgesAll.length} par` : ""}</div>
              </Box>
              <Box title="Na osi czasu" sub="data obserwacji nowego materiału na tle kampanii">
                {x?.timeline?.length ? <Timeline items={x.timeline.map((t) => ({ id: t.id, observed_at: t.observed_at, campaign_id: t.is_new ? "nowy" : x.decision?.campaign_id ?? null, title: t.is_new ? "nowy materiał" : null }))} colorOf={(c) => (c === "nowy" ? "var(--color-accent)" : "var(--color-muted)")} onPick={() => undefined} /> : <div className="text-xs text-subtle">—</div>}
              </Box>
            </div>
            {x?.per_extractor?.length ? (
              <Box title="Ekstraktory" sub="liczba genów (czas tylko, gdy zapis był rozłożony w czasie)">
                <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-x-6 gap-y-1 text-xs">
                  {x.per_extractor.map((e) => <div key={e.extractor} className="flex justify-between gap-2"><span className="truncate font-mono">{e.extractor}</span><span className="font-mono text-muted">{e.genes}{e.seconds === null ? "" : ` · ${plNumber(e.seconds, 2)} s`}</span></div>)}
                </div>
              </Box>
            ) : null}
            {x?.candidates?.pairs.length ? (
              <Box title="Najbliższe incydenty" sub="pary ocenione w tym przypisaniu">
                <ul className="grid gap-1 text-xs md:grid-cols-2">
                  {x.candidates.pairs.map((p) => <li key={p.other} className="flex items-center gap-2"><Link to={pairPath(p.a, p.b)} className="font-mono text-accent hover:underline">{p.other}</Link><HypothesisBadge value={p.hypothesis} /><span className="text-subtle">{p.indexes.join(" · ")}</span><span className="ml-auto font-mono">{formatBits(p.bits)}</span></li>)}
                </ul>
              </Box>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
