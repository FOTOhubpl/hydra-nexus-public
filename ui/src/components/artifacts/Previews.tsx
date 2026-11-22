/** Per-type artefact previews. Every mark comes from a stored gene / evidence pointer of this artefact. */
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState, type ReactNode } from "react";
import { frameUrl, rawUrl, useArtifactText, useHar, useTranscript, type ArtifactGene, type ArtifactInfo } from "../../api/artifacts";
import { cx } from "../../lib/cx";
import { boxMarks, findSpans, geneTerms, mmss, pickKeyframes, SPAN_LABEL, videoBboxScale, type BoxMark, type SpanKind } from "../../lib/artifactEvidence";
import { formatBytes } from "../../lib/format";
import { AnnotatedImage } from "./AnnotatedImage";
import { isWholeFile } from "../../lib/displayPl";

const SPAN_CLASS: Record<SpanKind, string> = {
  url: "bg-for-soft text-fg underline decoration-for/60",
  phone: "bg-abstain-soft text-abstain-ink",
  tracker: "bg-against-soft text-fg",
  email: "bg-abstain-soft text-abstain-ink",
  gene: "bg-for-soft text-fg ring-1 ring-for/40",
  focus: "bg-against text-white",
};

export function Note({ children }: { children: ReactNode }) {
  return <div className="px-3 py-2 text-xs text-subtle">{children}</div>;
}

// ------------------------------------------------------------------ highlighted text

export function HighlightedText({ text, genes = [], focus, maxChars = 400_000, className }: { text: string; genes?: readonly ArtifactGene[]; focus?: string | null; maxChars?: number; className?: string }) {
  const ref = useRef<HTMLPreElement>(null);
  const body = text.length > maxChars ? text.slice(0, maxChars) : text;
  const terms = useMemo(() => geneTerms(genes), [genes]);
  const spans = useMemo(() => findSpans(body, terms, focus), [body, terms, focus]);
  const counts = useMemo(() => spans.reduce<Record<string, number>>((acc, s) => ({ ...acc, [s.kind]: (acc[s.kind] ?? 0) + 1 }), {}), [spans]);
  useEffect(() => {
    if (!focus) return;
    ref.current?.querySelector("[data-span=focus]")?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focus, spans]);
  const nodes: ReactNode[] = [];
  let pos = 0;
  spans.forEach((s, i) => {
    if (s.start > pos) nodes.push(body.slice(pos, s.start));
    nodes.push(<mark key={i} data-span={s.kind} title={s.title ?? SPAN_LABEL[s.kind]} className={cx("rounded-[2px] px-px", SPAN_CLASS[s.kind])}>{body.slice(s.start, s.end)}</mark>);
    pos = s.end;
  });
  nodes.push(body.slice(pos));
  return (
    <div className={cx("flex min-h-0 flex-col", className)}>
      <div className="flex flex-wrap gap-2 border-b border-line px-2 py-1 text-2xs text-muted">
        {(Object.keys(SPAN_LABEL) as SpanKind[]).filter((k) => counts[k]).map((k) => (
          <span key={k} className={cx("rounded-[3px] px-1.5 py-0.5", SPAN_CLASS[k])}>{SPAN_LABEL[k]}: {counts[k]}</span>
        ))}
        {spans.length === 0 && <span>brak rozpoznanych adresów, telefonów, trackerów ani wartości genów</span>}
        {text.length > maxChars && <span>podświetlono pierwsze {formatBytes(maxChars)} tekstu</span>}
      </div>
      <pre ref={ref} className="max-h-[70vh] min-h-0 overflow-auto p-3 font-mono text-2xs leading-4 break-all whitespace-pre-wrap text-fg">{nodes}</pre>
    </div>
  );
}

// ------------------------------------------------------------------ HTML

export function HtmlPreview({ info, genes, focus }: { info: ArtifactInfo; genes: readonly ArtifactGene[]; focus?: string | null }) {
  const [tab, setTab] = useState<"view" | "source">("view");
  useEffect(() => { if (focus) setTab("source"); }, [focus]);
  const q = useArtifactText(info.id, true);
  const d = q.data;
  return (
    <div data-testid="html-preview" className="flex min-w-0 flex-col">
      <div className="flex items-center gap-1 border-b border-line px-2 py-1 text-xs" role="tablist">
        {([["view", "podgląd (bez skryptów)"], ["source", "źródło"]] as const).map(([k, label]) => (
          <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setTab(k)} className={cx("rounded-[4px] px-2 py-0.5", tab === k ? "bg-active text-fg" : "text-muted hover:bg-hover")}>{label}</button>
        ))}
        {d && <span className="ml-auto text-2xs text-subtle">{d.encoding} · {formatBytes(d.size)}{d.truncated ? ` · obcięto do ${formatBytes(d.limit)}` : ""}{d.rewritten_assets !== undefined ? ` · lokalne zasoby: ${d.rewritten_assets}` : ""}</span>}
      </div>
      {q.isPending ? <Note>Ładowanie źródła…</Note> : q.isError || !d ? <Note>Nie udało się pobrać tekstu strony.</Note> : tab === "view" ? (
        <>
          <iframe
            title={`Podgląd strony ${info.id.slice(0, 12)}`}
            sandbox=""
            referrerPolicy="no-referrer"
            srcDoc={d.srcdoc ?? ""}
            className="h-[65vh] w-full border-0 bg-white"
          />
          <Note>Strona wyświetlona w izolowanej ramce: skrypty, formularze i zasoby z internetu są zablokowane; obrazy zapisane razem ze stroną ładowane są z lokalnego magazynu (CAS).</Note>
        </>
      ) : (
        <HighlightedText text={d.text} genes={genes} focus={focus} />
      )}
    </div>
  );
}

export function TextPreview({ info, genes, focus }: { info: ArtifactInfo; genes: readonly ArtifactGene[]; focus?: string | null }) {
  const q = useArtifactText(info.id);
  const pretty = useMemo(() => {
    if (!q.data || info.kind !== "json" || q.data.truncated) return q.data?.text ?? "";
    try { return JSON.stringify(JSON.parse(q.data.text), null, 2); } catch { return q.data.text; }
  }, [q.data, info.kind]);
  if (q.isPending) return <Note>Ładowanie tekstu…</Note>;
  if (q.isError || !q.data) return <Note>Brak podglądu tekstowego dla {info.mime}.</Note>;
  return (
    <div>
      <HighlightedText text={pretty} genes={genes} focus={focus} />
      {q.data.truncated && <Note>Pokazano pierwsze {formatBytes(q.data.limit)} z {formatBytes(q.data.size)}; pełny plik: „Pobierz oryginał”.</Note>}
    </div>
  );
}

// ------------------------------------------------------------------ HAR

export function HarPreview({ info, genes, focusEntry }: { info: ArtifactInfo; genes: readonly ArtifactGene[]; focusEntry?: number | null }) {
  const q = useHar(info.id);
  const [filter, setFilter] = useState("");
  const tbody = useRef<HTMLTableSectionElement>(null);
  const byEntry = useMemo(() => {
    const m = new Map<number, Set<string>>();
    for (const g of genes) {
      const e = g.evidence?.har_entry;
      if (e === null || e === undefined) continue;
      m.set(e, (m.get(e) ?? new Set()).add(g.kind));
    }
    return m;
  }, [genes]);
  useEffect(() => {
    if (focusEntry === null || focusEntry === undefined) return;
    tbody.current?.querySelector(`[data-har="${focusEntry}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [focusEntry, q.data]);
  if (q.isPending) return <Note>Ładowanie logu HAR…</Note>;
  if (q.isError || !q.data) return <Note>Plik nie jest poprawnym logiem HAR lub jest za duży ({formatBytes(info.limits.har_bytes)}).</Note>;
  const d = q.data;
  const f = filter.trim().toLowerCase();
  const rows = d.rows.filter((r) => !f || `${r.host} ${r.url} ${r.mime} ${r.status}`.toLowerCase().includes(f) || (byEntry.get(r.idx) && [...byEntry.get(r.idx)!].join(" ").includes(f)));
  return (
    <div className="flex min-w-0 flex-col gap-2 p-2">
      <div className="flex flex-wrap gap-3 text-xs text-muted">
        <span><b className="text-fg">{d.entries}</b> żądań</span>
        <span>statusy: {Object.entries(d.statuses).map(([s, n]) => `${s}×${n}`).join(", ")}</span>
        <span>{d.hosts.length} hostów</span>
        {d.creator && <span>zapisane przez {d.creator}</span>}
        <span>{byEntry.size} żądań z genami</span>
      </div>
      {d.redirect_chains.length > 0 && (
        <div className="space-y-1">
          <div className="text-xs font-semibold text-muted">Łańcuchy przekierowań ({d.redirect_chains.length})</div>
          {d.redirect_chains.map((c, i) => (
            <div key={i} className="flex flex-wrap items-center gap-1 text-2xs">
              {c.map((s, j) => (
                <span key={j} className="inline-flex items-center gap-1">
                  {j > 0 && <span aria-hidden="true" className="text-subtle">→</span>}
                  <span className="rounded-[3px] border border-line px-1 font-mono" title={s.url ?? undefined}>{s.host ?? "poza logiem"}{s.status ? ` · ${s.status}` : ""}</span>
                </span>
              ))}
            </div>
          ))}
        </div>
      )}
      <div className="flex flex-wrap gap-1 text-2xs">
        {d.hosts.slice(0, 16).map((h) => (
          <button key={h.host} type="button" onClick={() => setFilter(h.host)} className="rounded-[3px] border border-line px-1.5 py-0.5 font-mono hover:border-accent">{h.host} <span className="text-subtle">{h.requests}</span></button>
        ))}
      </div>
      <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filtr: host, URL, typ, status, rodzaj genu" aria-label="Filtr żądań HAR" className="h-7 rounded-[4px] border border-line bg-raised px-2 text-xs focus:border-accent focus:outline-none" />
      <div className="max-h-[60vh] overflow-auto rounded-md border border-line">
        <table data-testid="har-table" className="w-full text-2xs whitespace-nowrap">
          <thead className="sticky top-0 bg-raised text-subtle uppercase">
            <tr><th className="px-1.5 py-1 text-right">#</th><th className="px-1.5 py-1 text-left">metoda</th><th className="px-1.5 py-1 text-left">host</th><th className="px-1.5 py-1 text-left">ścieżka</th><th className="px-1.5 py-1 text-right">status</th><th className="px-1.5 py-1 text-left">typ</th><th className="px-1.5 py-1 text-right">rozmiar</th><th className="px-1.5 py-1 text-right">czas</th><th className="px-1.5 py-1 text-left">przekierowanie</th><th className="px-1.5 py-1 text-left">geny</th></tr>
          </thead>
          <tbody ref={tbody}>
            {rows.map((r) => {
              let path = r.url ?? "";
              try { const u = new URL(r.url ?? ""); path = u.pathname + u.search; } catch { /* keep raw */ }
              const kinds = byEntry.get(r.idx);
              return (
                <tr key={r.idx} data-har={r.idx} className={cx("border-t border-line", focusEntry === r.idx ? "bg-against-soft" : kinds ? "bg-for-soft/50" : "")}>
                  <td className="px-1.5 py-0.5 text-right font-mono text-subtle">{r.idx}</td>
                  <td className="px-1.5 py-0.5 font-mono">{r.method}</td>
                  <td className="px-1.5 py-0.5 font-mono whitespace-nowrap">{r.host}</td>
                  <td className="max-w-[280px] truncate px-1.5 py-0.5 font-mono text-muted" title={r.url ?? undefined}>{path}</td>
                  <td className={cx("px-1.5 py-0.5 text-right font-mono", (r.status ?? 0) >= 400 ? "text-err" : (r.status ?? 0) >= 300 ? "text-warn" : "")}>{r.status ?? "—"}</td>
                  <td className="px-1.5 py-0.5 text-muted">{r.mime ?? r.resource_type ?? "—"}</td>
                  <td className="px-1.5 py-0.5 text-right font-mono">{r.size !== null && r.size >= 0 ? formatBytes(r.size) : "—"}</td>
                  <td className="px-1.5 py-0.5 text-right font-mono">{r.time_ms !== null ? `${Math.round(r.time_ms)} ms` : "—"}</td>
                  <td className="max-w-[180px] truncate px-1.5 py-0.5 font-mono text-muted" title={r.redirect ?? undefined}>{r.redirect ?? ""}</td>
                  <td className="max-w-[160px] truncate px-1.5 py-0.5 text-muted" title={kinds ? [...kinds].join(", ") : undefined}>{kinds ? [...kinds].join(", ") : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {d.truncated && <Note>Pokazano pierwsze {d.rows.length} z {d.entries} żądań.</Note>}
      <Note>Adresy przechodzą przez redakcję sekretów (tokeny w parametrach zapytań są ukryte).</Note>
    </div>
  );
}

// ------------------------------------------------------------------ transcript

export function TranscriptList({ artifactId, current, onSeek, matched = [] }: { artifactId: string; current?: number; onSeek?: (t: number) => void; matched?: readonly number[] }) {
  const q = useTranscript(artifactId);
  if (q.isPending) return <Note>Ładowanie transkrypcji…</Note>;
  const t = q.data;
  if (!t || !t.available) {
    return <Note>Transkrypcja ASR niedostępna{t?.gene ? ` (status: ${t.gene.status}${t.gene.note ? `, ${t.gene.note}` : ""})` : " (ASR nie był uruchomiony dla tego pliku)"}. Brak = nieznane.</Note>;
  }
  if (t.segments.length === 0) return <Note>ASR ({t.model}) nie wykrył mowy w tym pliku.</Note>;
  return (
    <div>
      <div className="px-3 pt-2 text-2xs text-subtle">Transkrypcja automatyczna ({t.model}, INF): {t.segments.length} segmentów. Pogrubione: segmenty z dowodami użytymi w parach.</div>
      <ol className="max-h-72 overflow-auto p-2 text-xs">
        {t.segments.map((s, i) => {
          const active = current !== undefined && current >= s.start && current < s.end;
          const hit = matched.some((m) => m >= s.start - 0.5 && m <= s.end + 0.5);
          return (
            <li key={i}>
              <button type="button" onClick={() => onSeek?.(s.start)} className={cx("flex w-full gap-2 rounded-[3px] px-1 py-0.5 text-left hover:bg-hover", active && "bg-active", hit && "font-semibold")}>
                <span className="w-24 shrink-0 font-mono text-subtle">{mmss(s.start)}–{mmss(s.end)}</span>
                <span>{s.text}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// ------------------------------------------------------------------ video / audio

export interface MediaHandle { seek: (t: number) => void }

function Timeline({ duration, marks, keyframes, current, onSeek }: { duration: number; marks: { t: number; label: string; strong: boolean }[]; keyframes: number[]; current: number; onSeek: (t: number) => void }) {
  if (!duration) return null;
  return (
    <div className="relative h-6 rounded-sm bg-hover" role="img" aria-label={`Oś czasu ${mmss(duration)}: ${marks.length} znaczników dowodów`}>
      {keyframes.map((t) => <span key={`k${t}`} className="absolute top-0 h-2 w-px bg-line-strong" style={{ left: `${(100 * t) / duration}%` }} />)}
      {marks.map((m, i) => (
        <button key={i} type="button" title={`${mmss(m.t)} · ${m.label}`} onClick={() => onSeek(m.t)} className={cx("absolute top-1 h-4 w-1.5 -translate-x-1/2 rounded-[1px]", m.strong ? "bg-against" : "bg-for/60")} style={{ left: `${(100 * m.t) / duration}%` }} />
      ))}
      <span className="pointer-events-none absolute inset-y-0 w-0.5 bg-fg" style={{ left: `${Math.min(100, (100 * current) / duration)}%` }} />
    </div>
  );
}

export const VideoPreview = forwardRef<MediaHandle, { info: ArtifactInfo; genes: readonly ArtifactGene[]; audioOnly?: boolean; focusKey?: string | null; onMark?: (m: BoxMark) => void }>(
  function VideoPreview({ info, genes, audioOnly = false, focusKey, onMark }, ref) {
    const media = useRef<HTMLVideoElement & HTMLAudioElement>(null);
    const [t, setT] = useState(0);
    const [frame, setFrame] = useState<number | null>(null);
    const [failed, setFailed] = useState(false);
    const duration = info.media?.duration_s ?? Math.max(0, ...info.timecodes);
    const keyframes = useMemo(() => pickKeyframes(info.timecodes, duration, 12), [info.timecodes, duration]);
    const seek = (s: number) => {
      if (media.current) {
        media.current.currentTime = s;
        void media.current.play?.()?.catch(() => undefined);
      }
      setT(s);
      if (!audioOnly) setFrame(Math.round(s * 10) / 10);
    };
    useImperativeHandle(ref, () => ({ seek }));
    const marks = useMemo(() => {
      const m = new Map<number, { t: number; label: string; strong: boolean }>();
      for (const g of genes) {
        const tc = g.evidence?.timecode_s;
        if (tc === null || tc === undefined || g.kind.startsWith("audio_fp") || g.kind === "asr_transcript" || isWholeFile(g.evidence, g.kind)) continue;
        const k = Math.round(tc * 2) / 2;
        const strong = g.pairs.length > 0;
        const prev = m.get(k);
        if (!prev || (strong && !prev.strong)) m.set(k, { t: tc, label: `${g.kind}${g.value ? `: ${g.value.slice(0, 40)}` : ""}${strong ? ` · w ${g.pairs.length} parach` : ""}`, strong });
      }
      return [...m.values()];
    }, [genes]);
    const matchedTimes = marks.filter((m) => m.strong).map((m) => m.t);
    const frameMarks = useMemo(() => (frame === null ? [] : boxMarks(genes, { timecode: frame, tolerance: 0.55, scaleOf: (g) => videoBboxScale(g, info.media?.width, info.media?.height) })), [genes, frame, info.media]);
    const space = info.media?.width && info.media?.height ? { w: info.media.width, h: info.media.height } : null;
    return (
      <div className="flex min-w-0 flex-col gap-2 p-2">
        {audioOnly ? (
          <audio ref={media} src={rawUrl(info.id)} controls preload="metadata" className="w-full" onTimeUpdate={(e) => setT(e.currentTarget.currentTime)} onError={() => setFailed(true)} />
        ) : (
          <video ref={media} data-testid="video-preview" src={rawUrl(info.id)} controls preload="metadata" className="max-h-[55vh] w-full rounded-md bg-black object-contain" onTimeUpdate={(e) => setT(e.currentTarget.currentTime)} onError={() => setFailed(true)}
            onLoadedMetadata={(e) => { if (e.currentTarget.videoWidth === 0) setFailed(true); }} />
        )}
        {failed && <Note>Przeglądarka nie odtwarza tego formatu ({info.media?.video_codec ?? info.mime}); klatki poniżej są dekodowane lokalnie przez ffmpeg.</Note>}
        <div className="flex flex-wrap items-center gap-3 text-2xs text-muted">
          <span className="font-mono">{mmss(t)} / {mmss(duration)}</span>
          <span><span className="inline-block h-2 w-1.5 bg-against align-middle" /> dowód użyty w parach</span>
          <span><span className="inline-block h-2 w-1.5 bg-for/60 align-middle" /> inny gen z kodem czasu</span>
          <span>{info.timecodes.length} próbkowanych chwil z genami</span>
        </div>
        <Timeline duration={duration} marks={marks} keyframes={keyframes} current={t} onSeek={seek} />
        {!audioOnly && keyframes.length > 0 && (
          <div className="flex gap-1.5 overflow-x-auto pb-1" aria-label="Klatki kluczowe" data-testid="video-keyframes">
            {keyframes.map((k) => (
              <button key={k} type="button" onClick={() => seek(k)} className={cx("shrink-0 overflow-hidden rounded-[3px] border", frame === k ? "border-accent" : "border-line hover:border-line-strong")} title={`klatka ${mmss(k)}`}>
                <img src={frameUrl(info.id, k, 160)} alt={`klatka ${mmss(k)}`} loading="lazy" className="h-20 w-auto bg-black object-contain" />
                <span className="block bg-raised px-1 font-mono text-2xs">{mmss(k)}</span>
              </button>
            ))}
          </div>
        )}
        {!audioOnly && frame !== null && (
          <div className="rounded-md border border-line">
            <div className="flex items-center gap-2 border-b border-line px-2 py-1 text-xs text-muted">
              Klatka {mmss(frame)} z ramkami genów (±0,5 s)
              <button type="button" className="ml-auto text-accent hover:underline" onClick={() => setFrame(null)}>zamknij</button>
            </div>
            <AnnotatedImage src={frameUrl(info.id, frame, 1280)} alt={`klatka ${mmss(frame)}`} marks={frameMarks} space={space} focusKey={focusKey} onMark={onMark} maxHeight="60vh" />
          </div>
        )}
        <TranscriptList artifactId={info.id} current={t} onSeek={seek} matched={matchedTimes} />
      </div>
    );
  },
);

export function ImagePreview({ info, genes, focusKey, onMark }: { info: ArtifactInfo; genes: readonly ArtifactGene[]; focusKey?: string | null; onMark?: (m: BoxMark) => void }) {
  const marks = useMemo(() => boxMarks(genes), [genes]);
  const space = info.media?.width && info.media?.height ? { w: info.media.width, h: info.media.height } : null;
  return <AnnotatedImage src={rawUrl(info.id)} alt={`Artefakt ${info.id.slice(0, 12)}`} marks={marks} space={space} focusKey={focusKey} onMark={onMark} />;
}
