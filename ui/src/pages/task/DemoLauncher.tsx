/**
 * One-click demo: pick a corpus that exists on this machine, start the corpus run, watch it live.
 * The live view polls GET /v1/demo/progress/{job}: CORE's run_progress when available, else the rows written
 * since the job started. Stages light up only when their rows exist.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { startDemo, useDemoCorpora, useDemoProgress } from "../../api/task";
import { Thumb } from "../../components/evidence";
import { RelationGraph } from "../../components/RelationGraph";
import { cx } from "../../lib/cx";
import { FAMILY_STACK_ORDER, familyColors, familyLabel } from "../../lib/evidence";
import { extSource } from "../../lib/ext";
import { formatCount } from "../../lib/format";
import { useTheme } from "../../lib/theme";
import { campaignColors } from "./DashboardPage";

function Stage({ label, state }: { label: string; state: "done" | "running" | "waiting" | "failed" }) {
  return (
    <span className={cx("inline-flex h-8 items-center gap-2 rounded-md border px-3 text-sm", state === "done" ? "border-for text-fg" : state === "running" ? "border-accent text-accent" : state === "failed" ? "border-err text-err" : "border-line text-subtle")}>
      {state === "running" && <span aria-hidden="true" className="inline-block size-3 animate-spin rounded-full border-2 border-accent border-t-transparent" />}
      {state === "done" && <span aria-hidden="true">✓</span>}
      {label}
    </span>
  );
}

const STAGE_LABEL: Record<string, string> = {
  import: "Import", genes: "Geny", rarity: "Rzadkość", candidates: "Kandydaci", scoring: "Ocena par",
  clustering: "Grupowanie", assign: "Przypisanie", done: "Gotowe",
};

function Meter({ label, done, total, color = "var(--color-accent)" }: { label: string; done: number; total: number | null; color?: string }) {
  const f = total ? Math.min(1, done / total) : 0;
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-sm"><span className="text-muted">{label}</span><span className="font-mono">{formatCount(done)}{total ? ` / ${formatCount(total)}` : ""}</span></div>
      <div className="h-2.5 overflow-hidden rounded-full bg-hover"><div className="h-full w-full origin-left rounded-full transition-transform duration-500" style={{ transform: `scaleX(${total ? f : 0})`, backgroundColor: color }} /></div>
    </div>
  );
}

function LiveCorpus({ jobId, corpusLabel, onClose }: { jobId: string; corpusLabel: string; onClose: () => void }) {
  const q = useDemoProgress(jobId);
  const theme = useTheme();
  const fam = familyColors(theme);
  const client = useQueryClient();
  const d = q.data;
  const core = d?.core_progress ?? null;
  const status = d?.job.status ?? "queued";
  const done = status === "done";
  const failed = status === "failed" || core?.status === "failed";
  useEffect(() => { if (done) void client.invalidateQueries({ queryKey: ["task"] }); }, [done, client]);
  const sizes = useMemo(() => (d?.campaigns ?? []).map((c) => ({ id: c.id, size: c.members.length })), [d]);
  const colors = useMemo(() => campaignColors(sizes, theme), [sizes, theme]);
  const member = useMemo(() => Object.fromEntries((d?.campaigns ?? []).flatMap((c) => c.members.map((m) => [m, c.id]))), [d]);
  const genesBy = core?.genes_by_family ?? d?.genes_written_by_family ?? {};
  const genes = core?.genes ?? Object.values(genesBy).reduce((a, b) => a + b, 0);
  const stages = core?.stages ?? ["import", "genes", "rarity", "candidates", "scoring", "clustering", "done"];
  const idx = core?.stage_index ?? (d?.run_id ? stages.length - 1 : 0);

  // Real edges of the persisted run, revealed strongest first (the run writes pairs in one transaction).
  const allEdges = useMemo(() => (d?.edges ?? []).filter((e) => e.bits >= 10 && e.hypothesis !== "independent"), [d]);
  const [shown, setShown] = useState(0);
  useEffect(() => {
    if (!allEdges.length) { setShown(0); return; }
    const t = window.setInterval(() => setShown((n) => (n >= allEdges.length ? n : n + 1)), 350);
    return () => window.clearInterval(t);
  }, [allEdges.length]);
  const edges = allEdges.slice(0, shown).map((e) => ({ id: `${e.a}|${e.b}`, source: e.a, target: e.b, bits: e.bits, family: e.dominant_family ?? null, dashed: e.hypothesis !== "same_campaign" }));
  const formed = shown >= allEdges.length && allEdges.length > 0;
  const nodes = (d?.incidents ?? []).map((i) => ({ id: i.id, label: i.id, group: formed ? member[i.id] ?? null : null, thumbnail: i.thumbnail, title: i.title }));
  const isNew = new Set(d?.new_incidents ?? []);
  const elapsed = d?.job.started_at ? ((Date.parse(String(d.job.finished_at ?? new Date().toISOString())) - Date.parse(String(d.job.started_at))) / 1000).toFixed(1).replace(".", ",") : null;
  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-canvas" role="dialog" aria-label="Analiza zbioru na żywo">
      <header className="flex items-center gap-4 border-b border-line bg-panel px-8 py-4">
        <h2 className="text-2xl font-semibold tracking-tight">Analiza zbioru na żywo</h2>
        <span className="rounded-md border border-line px-2 py-0.5 text-sm">{corpusLabel}</span>
        <span className="font-mono text-sm text-subtle">zadanie {jobId}{core?.run_id ? ` · przebieg ${core.run_id}` : ""}</span>
        <span className="ml-auto text-sm text-muted">{done ? "zakończone" : failed ? "błąd" : "przetwarzanie"}{elapsed && <span className="font-mono"> · {elapsed} s (zmierzone)</span>}</span>
        <button type="button" onClick={onClose} className="h-9 rounded-md border border-line-strong px-3 text-sm hover:bg-hover">{done ? "Pokaż pulpit" : "Zamknij"}</button>
      </header>
      <ol className="flex flex-wrap items-center gap-2 border-b border-line bg-panel px-8 py-3" aria-label="Etapy">
        {stages.map((sname, i) => (
          <li key={sname}><Stage label={STAGE_LABEL[sname] ?? sname} state={failed && i === idx ? "failed" : i < idx || (done && i <= idx) ? "done" : i === idx && !done ? "running" : "waiting"} /></li>
        ))}
        {!core && <li className="text-xs text-subtle">postęp wyprowadzony z tabel (brak pliku postępu CORE dla tego zadania)</li>}
      </ol>
      <div className="grid min-h-0 flex-1 gap-4 p-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <div className="flex min-h-0 flex-col gap-4">
          <section className="surface-card space-y-3 p-4">
            <Meter label="Incydenty przetworzone" done={core?.incidents_done ?? (d?.incidents.length ?? 0)} total={core?.incidents_total ?? null} />
            <Meter label="Pary ocenione" done={core?.pairs_scored ?? (d?.edges.length ?? 0)} total={core?.pairs_total ?? null} color="var(--color-fam-vis)" />
            <div className="flex gap-6 text-sm"><span className="text-muted">kampanie <b className="font-mono text-fg">{core?.campaigns ?? (d?.campaigns.length ?? 0)}</b></span><span className="text-muted">grupy <b className="font-mono text-fg">{core?.groups ?? (d?.groups?.length ?? 0)}</b></span><span className="text-muted">geny <b className="font-mono text-fg">{formatCount(genes)}</b></span></div>
          </section>
          <section className="surface-card p-4">
            <h3 className="mb-2 text-sm font-semibold">Geny według rodzin</h3>
            {FAMILY_STACK_ORDER.map((f) => {
              const n = genesBy[f] ?? 0;
              const max = Math.max(1, ...Object.values(genesBy));
              return (
                <div key={f} className="grid grid-cols-[150px_1fr_70px] items-center gap-3 py-0.5 text-sm">
                  <span className="text-muted">{f} {familyLabel(f)}</span>
                  <span className="h-2.5 overflow-hidden rounded-full bg-hover"><span className="block h-2.5 w-full origin-left rounded-full transition-transform duration-500" style={{ transform: `scaleX(${n / max})`, backgroundColor: fam[f] }} /></span>
                  <span className="text-right font-mono">{formatCount(n)}</span>
                </div>
              );
            })}
            {genes === 0 && idx > 1 && <div className="mt-1 text-xs text-subtle">Brak nowych genów: niezmienione incydenty używają cache ekstrakcji.</div>}
          </section>
          <section className="surface-card min-h-0 flex-1 overflow-auto p-4">
            <h3 className="mb-2 text-sm font-semibold">Incydenty</h3>
            <div className="grid grid-cols-[repeat(auto-fill,minmax(96px,1fr))] gap-2">
              {(d?.incidents ?? []).map((i) => (
                <div key={i.id} className={cx("min-w-0 rounded-md p-1", isNew.has(i.id) && "ring-2 ring-accent")}>
                  <Thumb artifactId={i.thumbnail} alt={i.id} size={96} className="!h-16 !w-full" />
                  <div className="mt-0.5 flex items-center gap-1 text-xs"><span className="size-2 rounded-full" style={{ backgroundColor: member[i.id] ? colors[member[i.id]!] : "var(--color-unknown)" }} /><span className="truncate font-mono">{i.id}</span></div>
                  {extSource(i.source) && <div className="text-2xs text-subtle">{extSource(i.source)}</div>}
                </div>
              ))}
            </div>
          </section>
        </div>
        <section className="surface-card relative min-h-0 overflow-hidden">
          {nodes.length ? <RelationGraph compact nodes={nodes} edges={edges} campaignColors={colors} groups={formed ? (d?.groups ?? []).map((g) => ({ id: g.id, campaign_ids: g.campaign_ids })) : []} />
            : <div className="p-6 text-sm text-subtle">Graf pojawi się, gdy w bazie będą incydenty.</div>}
          <div className="absolute top-3 left-3 rounded-md border border-line bg-panel px-3 py-1.5 text-xs text-muted">
            {allEdges.length ? `powiązania przebiegu od najsilniejszego: ${shown}/${allEdges.length}` : "krawędzie pojawią się po zapisaniu przebiegu"}
          </div>
        </section>
      </div>
      {failed && <div className="mx-6 mb-6 rounded-md border border-err p-3 text-sm"><b>{String(d?.job.error_code ?? "błąd")}</b> <span className="font-mono">{String(d?.job.error ?? core?.error ?? "")}</span></div>}
    </div>
  );
}

export function DemoLauncher({ onClose }: { onClose: () => void }) {
  const corpora = useDemoCorpora();
  const [choice, setChoice] = useState<string>("nask");
  const [fresh, setFresh] = useState(false);
  const [job, setJob] = useState<{ id: string; label: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const items = corpora.data?.items ?? [];
  if (job) return <LiveCorpus jobId={job.id} corpusLabel={job.label} onClose={onClose} />;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-fg/30 p-6" role="dialog" aria-label="Uruchom analizę">
      <div className="w-[560px] max-w-full rounded-lg border border-line bg-panel p-6">
        <h2 className="text-lg font-semibold">Uruchom analizę zbioru</h2>
        <p className="mt-1 text-sm text-muted">Pełny przebieg: import, geny, pary, kampanie i grupy. Na liście są tylko zbiory obecne na tym komputerze.</p>
        <div className="mt-4 space-y-2" role="radiogroup">
          {corpora.isPending && <div className="text-sm text-subtle">Ładowanie listy zbiorów…</div>}
          {!corpora.isPending && items.length === 0 && <div className="text-sm text-subtle">Brak zbiorów danych w katalogu danych (HYDRA_DATA_DIR).</div>}
          {items.map((c) => (
            <label key={c.id} className={cx("flex cursor-pointer items-center gap-3 rounded-md border p-3", choice === c.id ? "border-accent bg-for-soft" : "border-line hover:bg-hover")}>
              <input type="radio" name="corpus" value={c.id} checked={choice === c.id} onChange={() => setChoice(c.id)} />
              <span className="flex-1"><span className="font-medium">{c.label}</span><span className="block font-mono text-xs text-subtle">{c.path}{c.incidents_on_disk != null && ` · ${c.incidents_on_disk} folderów incydentów`}{c.incidents_in_db != null && ` · ${c.incidents_in_db} incydentów w bazie`}</span></span>
              {c.id !== "nask" && <span className="rounded border border-line px-1.5 text-2xs">{c.spec ?? c.id}</span>}
            </label>
          ))}
        </div>
        <label className="mt-4 flex items-center gap-2 text-sm"><input type="checkbox" checked={fresh} onChange={(e) => setFresh(e.target.checked)} /> Ekstrakcja od zera (bez cache) — wolniej, ale widać każdy etap</label>
        {err && <div className="mt-3 text-sm text-err">{err}</div>}
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="h-10 rounded-md border border-line-strong px-4 text-sm hover:bg-hover">Anuluj</button>
          <button type="button" disabled={!items.some((c) => c.id === choice)} onClick={() => { setErr(null); startDemo(choice, fresh).then((r) => setJob({ id: r.job_id, label: items.find((c) => c.id === choice)?.label ?? choice })).catch((e: unknown) => setErr(e instanceof Error ? e.message : "Błąd startu")); }}
            className="h-10 rounded-md bg-accent px-5 text-sm font-semibold text-canvas disabled:cursor-not-allowed disabled:bg-unknown disabled:text-panel">▶ Uruchom</button>
        </div>
      </div>
    </div>
  );
}
