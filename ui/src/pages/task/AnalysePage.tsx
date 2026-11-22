import { DecisionRule } from "../../components/DecisionRule";
import { READ_ONLY_TIP, useCanWrite } from "../../api/auth";
import { useEffect, useRef, useState, type DragEvent, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";
import { toUploadEntries, type UploadEntry } from "../../api/client";
import { useAnalyseResult, useStartAnalyse, type AnalyseInput, type AnalyseResult, type DatasetImportReport } from "../../api/task";
import { Button } from "../../components/Button";
import { collectDropped } from "../../components/FileDropzone";
import { DecisionBadge, HypothesisBadge, Probability, SectionNote } from "../../components/evidence";
import { Panel } from "../../components/Panel";
import { ReferenceGalleryPanel } from "../../components/ReferenceGalleryPanel";
import { cx } from "../../lib/cx";
import { formatBits, plNumber } from "../../lib/evidence";
import { formatBytes } from "../../lib/format";
import { addSessionJob, useSessionJobs } from "../../lib/sessionJobs";
import { CampaignLink, IncidentLink, Page, pairPath } from "./common";
import { LiveAnalysis } from "./LiveAnalysis";
import { BatchPanel } from "./BatchPanel";
import { splitUploads } from "../../lib/uploadPolicy";
import { polishReason } from "../../lib/displayPl";
import { LabLink, topPair } from "../../components/LabLink";
import { AnalyseActivity, type UploadState } from "../../components/AnalyseActivity";
import { useAnalyseProgress } from "../../api/progress";
import { isApiError } from "../../api/client";
import type { UploadProgress } from "../../api/upload";

const MODE_LABEL: Record<string, string> = {
  corpus: "zbiór incydentów (pełny przebieg: geny, pary, kampanie)",
  path: "pojedynczy incydent z folderu serwera",
  files: "pliki / ZIP incydentu",
  screenshot: "zrzut ekranu z linkiem",
  url: "adres URL",
  hint: "wskazówka (wyszukiwanie w korpusie)",
};

const TABS = [
  { key: "url", label: "Adres URL", help: "Bezpośredni adres reklamy lub strony docelowej. Backend pobiera stronę w izolacji i wzbogaca domenę (RDAP, DNS, CT)." },
  { key: "screenshot", label: "Zrzut ekranu z linkiem", help: "Zrzut zawierający adres. Backend: OCR → wyodrębnienie URL/domeny → dalej jak dla adresu URL; zrzut zostaje jako dowód." },
  { key: "hint", label: "Wskazówka (np. nazwa produktu)", help: "Słowo kluczowe lub nazwa produktu: wyszukiwanie w przetworzonym korpusie (i w bibliotece reklam, gdy dostępna)." },
  { key: "files", label: "Plik lub ZIP incydentu", help: "Folder incydentu (kreacje, nagrania, zapisana strona, HAR, .info.json), pojedynczy plik albo archiwum ZIP. Cały zbiór (folder z podfolderami incydentów albo ZIP zbioru) uruchamia pełny przebieg: kampanie, pary, graf." },
  { key: "path", label: "Folder na serwerze", help: "Ścieżka w katalogu danych (HYDRA_DATA_DIR). Folder z wieloma podfolderami incydentów = pełny przebieg na zbiorze; jeden folder = jeden incydent." },
] as const;
type Tab = (typeof TABS)[number]["key"];

/** Per-file statuses of a dataset import (files that did not become an artifact), JURY-READY. */
function ImportReportList({ report }: { report: DatasetImportReport }) {
  return (
    <div id="import-report" data-testid="import-report" className="rounded-md border border-line p-2 text-xs text-muted">
      <div className="font-medium text-fg">{report.summary_pl}</div>
      {report.files.length > 0 && (
        <ul className="mt-1 max-h-60 space-y-0.5 overflow-y-auto">
          {report.files.map((f, i) => (
            <li key={`${f.path}-${i}`}>
              <span className="font-mono break-all text-fg">{f.path}</span>
              {f.incident_id && <> · <IncidentLink id={f.incident_id} /></>}
              {": "}{f.status_pl} <span className="font-mono text-subtle">[{f.code}]</span>
            </li>
          ))}
        </ul>
      )}
      {(report.notes_pl ?? []).map((n) => <div key={n} className="mt-1 text-abstain-ink">• {n}</div>)}
      {report.truncated && <div className="mt-1">Lista skrócona do {report.files.length} pozycji z {report.skipped_files}.</div>}
    </div>
  );
}

function FilePicker({ entries, setEntries, single, onJunk }: { entries: UploadEntry[]; setEntries: (e: UploadEntry[]) => void; single?: boolean; onJunk?: (paths: string[]) => void }) {
  const files = useRef<HTMLInputElement>(null);
  const folder = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  useEffect(() => {
    folder.current?.setAttribute("webkitdirectory", "");
  }, []);
  const onDrop = async (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const { keep, junk } = splitUploads(await collectDropped(e.dataTransfer));
    onJunk?.(junk.map((j) => j.path));
    setEntries(single ? keep.slice(0, 1) : keep);
  };
  const total = entries.reduce((s, x) => s + x.file.size, 0);
  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      className={cx("rounded-md border-2 border-dashed p-6 text-center text-sm", over ? "border-accent bg-for-soft" : "border-line-strong bg-raised")}
    >
      <div className="text-muted">{single ? "Upuść zrzut ekranu tutaj" : "Upuść folder incydentu, pliki lub ZIP tutaj"}</div>
      <div className="mt-2 flex justify-center gap-2">
        <Button onClick={() => files.current?.click()}>{single ? "Wybierz obraz…" : "Wybierz pliki / ZIP…"}</Button>
        {!single && <Button onClick={() => folder.current?.click()}>Wybierz folder…</Button>}
      </div>
      <input ref={files} data-testid="analyse-file-input" type="file" hidden multiple={!single} accept={single ? "image/*" : undefined} onChange={(e) => setEntries(toUploadEntries(e.target.files ?? []))} />
      <input ref={folder} data-testid="analyse-folder-input" type="file" hidden multiple onChange={(e) => { const { keep, junk } = splitUploads(toUploadEntries(e.target.files ?? [])); onJunk?.(junk.map((j) => j.path)); setEntries(keep); }} />
      {entries.length > 0 && (
        <div className="mt-3 text-xs text-fg">
          wybrano {entries.length} plik(ów), {formatBytes(total)}: <span className="font-mono text-muted">{entries.slice(0, 4).map((e) => e.path).join(", ")}{entries.length > 4 ? "…" : ""}</span>
        </div>
      )}
    </div>
  );
}

function jobMode(r: AnalyseResult): string {
  const p = (r.job.params ?? {}) as Record<string, unknown>;
  return String(p.mode ?? p.input_kind ?? p.source ?? "");
}

function AssignmentCard({ result, mode }: { result: AnalyseResult; mode: string }) {
  const { job, assignment, campaign } = result;
  const core = result.result ?? null;
  const status = job.status;
  const items = job.items_by_status ?? {};
  return (
    <Panel title="Wynik analizy">
      <div data-testid="analyse-result" className="space-y-3 text-sm">
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-mono text-xs text-subtle">zadanie {String(job.id ?? "")}</span>
          {mode && <span className="rounded-[3px] border border-line px-1.5 text-xs">tryb: {MODE_LABEL[mode] ?? mode}</span>}
          <span className={cx("font-semibold", status === "failed" ? "text-err" : status === "done" ? "text-ok" : "text-accent")}>
            {status === "queued" ? "w kolejce" : status === "running" ? "przetwarzanie…" : status === "done" ? "zakończone" : "błąd"}
          </span>
          {Object.keys(items).length > 0 && <span className="text-xs text-muted">pliki: {Object.entries(items).map(([k, v]) => `${k} ${v}`).join(" · ")}</span>}
        </div>
        {(status === "queued" || status === "running") && <div className="relative h-1.5 overflow-hidden rounded-full bg-hover"><div className="absolute inset-y-0 w-1/3 animate-indeterminate rounded-full bg-accent" /></div>}
        {status === "failed" && (
          <div className="rounded-md border border-err/40 bg-against-soft p-2 text-xs">
            <div className="font-semibold">{String(job.error_code ?? "błąd")}</div>
            <div className="font-mono break-all">{String(job.error ?? "")}</div>
            {job.error_code === "PipelineUnavailable" && <div className="mt-1">Potok analizy (CORE) nie jest jeszcze dostępny w tej wersji backendu. Materiał nie został oceniony.</div>}
          </div>
        )}
        {core && (
          <div className="space-y-1 text-xs">
            {core.urls_in_image && <div><span className="text-subtle">Adresy znalezione na zrzucie (OCR): </span>{core.urls_in_image.length ? <span className="font-mono">{core.urls_in_image.join(", ")}</span> : "brak"}</div>}
            {core.fetch && <div><span className="text-subtle">Pobranie strony: </span><span className="font-mono break-all">{Object.entries(core.fetch).filter(([, v]) => typeof v !== "object").map(([k, v]) => `${k}=${String(v)}`).join(" · ")}</span></div>}
            {(core.notes ?? []).map((n, i) => <div key={i} className="text-abstain-ink">• {n}</div>)}
            {(core.notes ?? []).some((n) => /galeria twarzy|wzorce logo/.test(String(n))) && <ReferenceGalleryPanel compact />}
          </div>
        )}
        {core?.kind === "corpus" && (
          <div className="space-y-1 text-sm">
            <div>Nowy przebieg analizy: {core.run_id ? <span className="font-mono">{String(core.run_id)}</span> : "—"}</div>
            {core.metrics && <div className="font-mono text-xs text-muted">{Object.entries(core.metrics).filter(([, v]) => typeof v === "number").map(([k, v]) => `${k}=${String(v)}`).join(" · ")}</div>}
            {core.import_report && <ImportReportList report={core.import_report} />}
            <Link to="/campaigns" className="text-accent hover:underline">Zobacz kampanie →</Link>
          </div>
        )}
        {core?.search && (
          <div className="space-y-2">
            <div className="text-sm font-semibold">Wyniki wyszukiwania w korpusie</div>
            {(() => {
              type AdHit = { ad_id: string; page_name?: string | null; text?: string; reasons?: string[]; delivery_start?: string | null };
              const adlib = core.search.ad_library as
                | { status?: string; note?: string; mode?: string; n_ads?: number; ads?: AdHit[] }
                | undefined;
              const ads = adlib?.ads ?? [];
              return (
                <>
                  {adlib?.note && <div className="text-xs text-muted">Biblioteka reklam: {adlib.note}</div>}
                  {ads.length > 0 && (
                    <div className="text-xs">
                      <div className="mb-1 text-subtle">
                        Biblioteka reklam Meta: {adlib?.n_ads ?? ads.length} reklam
                        {adlib?.mode === "replay" ? " (z pamięci podręcznej)" : ""}
                      </div>
                      <ul className="space-y-1">
                        {ads.slice(0, 5).map((a) => (
                          <li key={a.ad_id}>
                            <span className="font-mono">{a.ad_id}</span> {a.page_name ?? ""}
                            {a.delivery_start ? ` · od ${a.delivery_start.slice(0, 10)}` : ""}
                            <div className="text-2xs text-muted">
                              {(a.reasons ?? []).length > 0 ? `powody: ${(a.reasons ?? []).slice(0, 4).join(", ")}` : "brak reguł"}
                              {a.text ? ` · „${a.text.slice(0, 80)}”` : ""}
                            </div>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </>
              );
            })()}
            <div className="grid gap-3 md:grid-cols-2">
              <div>
                <div className="mb-1 text-xs text-subtle">Kampanie</div>
                <ul className="space-y-0.5 text-xs">{(core.search.campaigns ?? []).map((c) => <li key={c.campaign_id}><CampaignLink id={c.campaign_id} label={c.label} /> <span className="font-mono text-subtle" title="suma bitów rzadkości trafionych genów × pokrycie słów">wynik {plNumber(c.score, 1)}</span></li>)}</ul>
                {(core.search.campaigns ?? []).length === 0 && <div className="text-xs text-subtle">brak</div>}
              </div>
              <div>
                <div className="mb-1 text-xs text-subtle">Incydenty</div>
                <ul className="space-y-1 text-xs">{(core.search.incidents ?? []).map((c) => (
                  <li key={c.incident_id}>
                    <IncidentLink id={c.incident_id} /> <span className="font-mono text-subtle">wynik {plNumber(c.score, 1)}</span>
                    <div className="text-2xs text-muted">{(c.matches ?? []).slice(0, 3).map((m) => `${m.kind}: „${m.value.slice(0, 40)}” ${formatBits(m.bits)}`).join(" · ")}</div>
                  </li>
                ))}</ul>
                {(core.search.incidents ?? []).length === 0 && <div className="text-xs text-subtle">brak</div>}
              </div>
            </div>
          </div>
        )}
        {assignment && (
          <div className="space-y-2">
            <DecisionBadge value={assignment.decision} large />
            <div className="text-fg" data-testid="assignment-reason">{polishReason(assignment.reason_pl)}</div>
            <DecisionRule meta={assignment.meta} bestPair={topPair(assignment)} className="max-w-2xl" />
            <LabLink incidentId={assignment.incident_id} />
            <div className="text-xs text-muted">
              incydent <IncidentLink id={assignment.incident_id} />
              {assignment.campaign_id && <> · kampania <CampaignLink id={assignment.campaign_id} label={campaign?.label} /></>}
              {assignment.group_id && <> · pasuje do grupy kampanii <span className="font-mono">{assignment.group_id}</span></>}
              {" "}· model <span className="font-mono">{assignment.model}</span>
            </div>
            {assignment.candidates.length > 0 && (
              <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs">
                <thead className="text-2xs tracking-wider text-subtle uppercase">
                  <tr><th className="py-1 text-left">Kandydat</th><th className="py-1 text-right" title="wynik, nie prawdopodobieństwo">wynik nieskalibrowany</th><th className="py-1 text-right">Wsparcie</th><th className="py-1 pl-4 text-left">Najlepsza para</th></tr>
                </thead>
                <tbody>
                  {assignment.candidates.map((c) => (
                    <tr key={c.campaign_id} className="border-t border-line">
                      <td className="py-1"><CampaignLink id={c.campaign_id} /></td>
                      <td className="py-1 text-right"><Probability p={c.probability} /></td>
                      <td className="py-1 text-right font-mono">{c.support}</td>
                      <td className="py-1 pl-4">
                        {c.best_pair ? (
                          <span className="inline-flex items-center gap-2">
                            <Link className="font-mono text-accent hover:underline" to={pairPath(c.best_pair.a, c.best_pair.b)}>{c.best_pair.a} ↔ {c.best_pair.b}</Link>
                            <HypothesisBadge value={c.best_pair.hypothesis} />
                            <span className="font-mono">{formatBits(c.best_pair.total_bits)}</span>
                          </span>
                        ) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
}

export function AnalysePage() {
  const [params] = useSearchParams();
  const wanted = params.get("tab");
  const [tab, setTab] = useState<Tab>(TABS.some((t) => t.key === wanted) ? (wanted as Tab) : "url");
  const [url, setUrl] = useState("");
  const [hint, setHint] = useState("");
  const [path, setPath] = useState("");
  const [mode, setMode] = useState<string | null>(null);
  const [liveOpen, setLiveOpen] = useState(false);
  const [skipped, setSkipped] = useState<{ path: string; reason: string }[]>([]);
  const [entries, setEntries] = useState<UploadEntry[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const start = useStartAnalyse();
  const canWrite = useCanWrite();
  const result = useAnalyseResult(jobId);
  const recent = useSessionJobs().filter((j) => j.label.startsWith("Analiza"));
  // IMPORT-PROGRESS: browser upload (XHR progress, cancel) + live server activity (SSE, polling fallback)
  const [upload, setUpload] = useState<UploadState | null>(null);
  const [activityJob, setActivityJob] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const lastSample = useRef(0);
  const progress = useAnalyseProgress(activityJob);

  const input: AnalyseInput | null =
    tab === "url" ? (url.trim() ? { kind: "url", url: url.trim() } : null)
    : tab === "hint" ? (hint.trim() ? { kind: "hint", hint: hint.trim() } : null)
    : tab === "path" ? (path.trim() ? { kind: "path", path: path.trim() } : null)
    : entries.length ? { kind: tab, entries } : null;

  const run = (inp: AnalyseInput) => {
    const multipart = inp.kind === "files" || inp.kind === "screenshot";
    const tracked = multipart || inp.kind === "path";
    const ctrl = new AbortController();
    abort.current = ctrl;
    lastSample.current = 0;
    setActivityJob(null);
    setUpload(multipart ? { phase: "uploading", samples: [] } : null);
    const onUploadProgress = (p: UploadProgress) => {
      const finished = p.total !== null && p.loaded >= p.total;
      if (!finished && p.at - lastSample.current < 250) return; // <= 4 UI updates per second
      lastSample.current = p.at;
      setUpload((u) => (u && (u.phase === "uploading" || u.phase === "processing")
        ? { phase: finished ? "processing" : "uploading", samples: [...u.samples.slice(-59), p] } : u));
    };
    start.mutate({ input: inp, opts: { signal: ctrl.signal, onUploadProgress } }, {
      onSuccess: ({ job_id, mode: m, skipped: sk }) => {
        if (sk?.length) setSkipped((prev) => [...prev, ...sk]);
        setUpload((u) => (u ? { ...u, phase: "done" } : u));
        if (tracked) setActivityJob(job_id);
        setJobId(job_id);
        setMode(m ?? null);
        setLiveOpen(true);
        addSessionJob(job_id, `Analiza: ${TABS.find((t) => t.key === tab)!.label}`);
      },
      onError: (err) => {
        const aborted = isApiError(err) && err.kind === "aborted";
        setUpload((u) => (u ? { ...u, phase: aborted ? "cancelled" : "error", error: err instanceof Error ? err.message : String(err) } : u));
      },
    });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (input && canWrite) run(input);
  };
  const lastInput = useRef<AnalyseInput | null>(null);
  if (start.variables?.input) lastInput.current = start.variables.input;
  const retry = () => { if (lastInput.current) run(lastInput.current); };
  const importReport = result.data?.result?.import_report ? "#import-report" : null;
  const activity = (upload || activityJob) ? (
    <AnalyseActivity upload={upload} samples={progress.samples} onCancel={() => abort.current?.abort()} onRetry={retry} reportHref={importReport} />
  ) : null;

  return (
    <>
    {liveOpen && jobId && <LiveAnalysis jobId={jobId} modeLabel={MODE_LABEL[mode ?? tab] ?? tab} onClose={() => setLiveOpen(false)}
      activity={activityJob === jobId && progress.samples.length ? <AnalyseActivity upload={upload} samples={progress.samples} compact /> : null} />}
    <Page title="Nowa analiza" sub="przypisanie do znanej kampanii, nowy wzorzec albo wstrzymanie, z powodami">
      <div className="grid gap-3 xl:grid-cols-[1fr_1fr]">
        <Panel title="Wejście">
          <form onSubmit={submit} className="space-y-3">
            <div role="tablist" data-testid="analyse-tabs" className="flex flex-wrap gap-1">
              {TABS.map((t) => (
                <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} onClick={() => { setTab(t.key); setEntries([]); }}
                  className={cx("h-7 rounded-[4px] border px-3 text-sm", tab === t.key ? "border-fg bg-fg text-canvas" : "border-line-strong bg-panel hover:bg-hover")}>
                  {t.label}
                </button>
              ))}
            </div>
            <p className="text-xs text-muted">{TABS.find((t) => t.key === tab)!.help}</p>
            {tab === "url" && <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" aria-label="Adres URL" className="h-8 w-full rounded-[4px] border border-line bg-raised px-3 font-mono text-sm focus:border-accent focus:outline-none" />}
            {tab === "hint" && <input value={hint} onChange={(e) => setHint(e.target.value)} placeholder="np. nazwa produktu, hasło z reklamy, nazwisko z wizerunku" aria-label="Wskazówka" className="h-8 w-full rounded-[4px] border border-line bg-raised px-3 text-sm focus:border-accent focus:outline-none" />}
            {tab === "path" && <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="/hydra/data/jury (Docker) lub ~/HydraNexus/data/jury" aria-label="Ścieżka" className="h-8 w-full rounded-[4px] border border-line bg-raised px-3 font-mono text-sm focus:border-accent focus:outline-none" />}
            {(tab === "files" || tab === "screenshot") && <FilePicker entries={entries} setEntries={setEntries} single={tab === "screenshot"} onJunk={(p) => setSkipped(p.map((x) => ({ path: x, reason: "plik systemowy (pominięty przed wysłaniem)" })))} />}
            {skipped.length > 0 && (
              <div className="rounded-md border border-line p-2 text-xs text-muted">
                <div className="font-medium text-fg">Pominięte pliki ({skipped.length})</div>
                <ul className="mt-1 space-y-0.5">{skipped.slice(0, 10).map((x) => <li key={x.path}><span className="font-mono">{x.path}</span>: {x.reason}</li>)}</ul>
              </div>
            )}
            <div className="flex items-center gap-2">
              <Button type="submit" variant="primary" disabled={!input || start.isPending || !canWrite} title={canWrite ? undefined : READ_ONLY_TIP}>Analizuj</Button>
              {!canWrite && <span className="text-xs text-muted" data-testid="read-only-note">{READ_ONLY_TIP}</span>}
              {start.isError && !upload && <span className="text-xs text-err">{start.error instanceof Error ? start.error.message : "Błąd"}</span>}
            </div>
            {activity}
          </form>
        </Panel>
        {result.data ? <AssignmentCard result={result.data} mode={mode ?? jobMode(result.data)} /> : (
          <Panel title="Wynik analizy">
            <div className="text-sm text-subtle">{jobId ? "Ładowanie stanu zadania…" : "Wybierz wejście i kliknij „Analizuj”. Wynik pokaże decyzję, kampanię i najmocniejsze powody."}</div>
            {result.isError && <div className="text-xs text-err">{result.error.message}</div>}
          </Panel>
        )}
      </div>
      <BatchPanel />
      {recent.length > 0 && (
        <Panel title="Ostatnie analizy w tej sesji">
          <ul className="space-y-1 text-xs">
            {recent.slice(0, 10).map((j) => (
              <li key={j.id}><button type="button" className="font-mono text-accent hover:underline" onClick={() => setJobId(j.id)}>{j.id}</button> <span className="text-muted">{j.label}</span></li>
            ))}
          </ul>
        </Panel>
      )}
      {jobId && !liveOpen && <button type="button" onClick={() => setLiveOpen(true)} className="self-start text-sm text-accent hover:underline">▶ Pokaż analizę na żywo (pełny ekran)</button>}
      <SectionNote>Decyzja „nowa kampania” oznacza, że żadna znana kampania nie ma wystarczającego poparcia w bitach (rozpoznanie zbioru otwartego). „Wstrzymano” = dowody słabe lub sprzeczne, decyzja dla analityka.</SectionNote>
    </Page>
    </>
  );
}
