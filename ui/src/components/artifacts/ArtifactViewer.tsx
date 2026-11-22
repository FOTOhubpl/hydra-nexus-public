import { useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { rawUrl, useArtifactGenes, useArtifactInfo, type ArtifactGene, type ArtifactInfo } from "../../api/artifacts";
import { boxMarks, focusTermOf, type BoxMark } from "../../lib/artifactEvidence";
import { cx } from "../../lib/cx";
import { formatDatePl, ROLE_LABEL } from "../../lib/evidence";
import { formatBytes } from "../../lib/format";
import { GeneStatusChip } from "../evidence";
import { GenesPanel } from "./GenesPanel";
import { HarPreview, HtmlPreview, ImagePreview, Note, TextPreview, VideoPreview, type MediaHandle } from "./Previews";

export function CopyHash({ value, chars = 12 }: { value: string; chars?: number }) {
  const [done, setDone] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(value).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }, () => undefined);
  };
  return (
    <span className="inline-flex items-center gap-1">
      <span className="font-mono" title={value}>{value.slice(0, chars)}…</span>
      <button type="button" onClick={copy} className="rounded-[3px] border border-line px-1 text-2xs text-muted hover:bg-hover" aria-label="Kopiuj pełny skrót sha256">{done ? "skopiowano" : "kopiuj"}</button>
    </span>
  );
}

function Row({ k, children }: { k: string; children: ReactNode }) {
  return <><dt className="text-subtle">{k}</dt><dd className="min-w-0 break-words">{children}</dd></>;
}

function MediaFacts({ info }: { info: ArtifactInfo }) {
  const m = info.media;
  if (!m) return null;
  if (m.error) return <Row k="Właściwości">nieznane ({m.error})</Row>;
  const parts = [
    m.width && m.height ? `${m.width} × ${m.height} px` : null,
    m.duration_s ? `${m.duration_s.toFixed(1).replace(".", ",")} s` : null,
    m.video_codec ? `wideo ${m.video_codec}` : null,
    m.audio_codec ? `audio ${m.audio_codec}${m.sample_rate ? ` ${m.sample_rate} Hz` : ""}` : null,
    m.format && !m.video_codec ? m.format : null,
    m.rotation ? `obrót ${m.rotation}°` : null,
  ].filter(Boolean);
  return parts.length ? <Row k="Właściwości">{parts.join(" · ")}</Row> : null;
}

function Provenance({ info }: { info: ArtifactInfo }) {
  if (info.provenance.length === 0) return null;
  return (
    <div className="rounded-md border border-line p-2 text-xs">
      <div className="mb-1 font-semibold text-muted">Pochodzenie pliku (C2PA, metadane zapisu)</div>
      <ul className="space-y-0.5">
        {info.provenance.map((p, i) => (
          <li key={i} className="flex flex-wrap items-baseline gap-2">
            <span className="font-mono text-muted">{p.kind}</span>
            {p.value && <span className="font-mono">{p.value}</span>}
            {p.status !== "observed" && <GeneStatusChip status={p.status} />}
            {p.note && <span className="text-subtle">{p.note}</span>}
            {p.scope === "incident" && <span className="text-2xs text-subtle">(dla całego incydentu)</span>}
          </li>
        ))}
      </ul>
      <div className="mt-1 text-2xs text-subtle">Brak manifestu C2PA nie świadczy ani o autentyczności, ani o fałszerstwie.</div>
    </div>
  );
}

export function ArtifactHeader({ info, role, path }: { info: ArtifactInfo; role?: string | null; path?: string | null }) {
  const link = info.incidents[0];
  return (
    <dl className="grid grid-cols-[120px_1fr] gap-x-3 gap-y-1 text-xs">
      <Row k="Rola">{ROLE_LABEL[role ?? link?.role ?? ""] ?? role ?? link?.role ?? "—"}</Row>
      <Row k="Typ MIME"><span className="font-mono">{info.mime}</span> <span className="text-subtle">({info.kind})</span></Row>
      <Row k="Rozmiar">{formatBytes(info.size)}</Row>
      <Row k="SHA-256"><CopyHash value={info.sha256} /></Row>
      <Row k="Ścieżka"><span className="font-mono">{path ?? link?.path_in_incident ?? "—"}</span></Row>
      <Row k="Źródło"><span className="font-mono">{info.source_ref ?? "—"}</span></Row>
      <Row k="Data obserwacji">{info.observed_at ? formatDatePl(info.observed_at) : <span className="text-subtle">nieznana (brak w materiale)</span>}</Row>
      <Row k="Zaimportowano">{formatDatePl(info.created_at)}</Row>
      <MediaFacts info={info} />
      {info.incidents.length > 1 && (
        <Row k="Też w incydentach">
          {info.incidents.map((l) => <Link key={l.incident_id + l.path_in_incident} to={`/incident/${encodeURIComponent(l.incident_id)}?art=${info.id}`} className="mr-2 font-mono text-accent hover:underline">{l.incident_id}</Link>)}
        </Row>
      )}
    </dl>
  );
}

/** Full artefact view: metadata, type-specific preview with evidence marks, provenance and "Co wyciągnięto". */
export function ArtifactViewer({ id, role, path, pairHref }: { id: string; role?: string | null; path?: string | null; pairHref: (a: string, b: string) => string }) {
  const info = useArtifactInfo(id);
  const genesQ = useArtifactGenes(id);
  const genes = useMemo(() => genesQ.data?.genes ?? [], [genesQ.data]);
  const [focus, setFocus] = useState<ArtifactGene | null>(null);
  const media = useRef<MediaHandle>(null);
  const marks = useMemo(() => boxMarks(genes), [genes]);
  const focusKey = useMemo(() => {
    if (!focus) return null;
    const all = focus.evidence?.timecode_s !== null && focus.evidence?.timecode_s !== undefined ? boxMarks(genes, { timecode: focus.evidence.timecode_s }) : marks;
    return all.find((m) => m.genes.includes(focus))?.key ?? null;
  }, [focus, genes, marks]);

  if (info.isPending) return <Note>Ładowanie artefaktu…</Note>;
  if (info.isError || !info.data) return <Note>Nie udało się pobrać opisu artefaktu ({id.slice(0, 12)}…).</Note>;
  const d = info.data;

  const onFocus = (g: ArtifactGene) => {
    setFocus(g);
    const t = g.evidence?.timecode_s;
    if ((d.kind === "video" || d.kind === "audio") && t !== null && t !== undefined) media.current?.seek(t);
    document.getElementById(`art-preview-${id}`)?.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  const onMark = (m: BoxMark) => setFocus(m.genes[0] as ArtifactGene);
  const term = focus ? focusTermOf(focus) : null;

  let preview: ReactNode;
  switch (d.kind) {
    case "image": preview = <ImagePreview info={d} genes={genes} focusKey={focusKey} onMark={onMark} />; break;
    case "video": preview = <VideoPreview ref={media} info={d} genes={genes} focusKey={focusKey} onMark={onMark} />; break;
    case "audio": preview = <VideoPreview ref={media} info={d} genes={genes} audioOnly />; break;
    case "html": preview = <HtmlPreview info={d} genes={genes} focus={term} />; break;
    case "har": preview = <HarPreview info={d} genes={genes} focusEntry={focus?.evidence?.har_entry ?? null} />; break;
    case "json": case "text": preview = <TextPreview info={d} genes={genes} focus={term} />; break;
    default: preview = <Note>Brak podglądu dla typu {d.mime}. Oryginał można pobrać.</Note>;
  }

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_340px]">
        <section id={`art-preview-${id}`} data-testid="artifact-preview" className="min-w-0 overflow-hidden rounded-md border border-line bg-panel">{preview}</section>
        <div className="flex min-w-0 flex-col gap-3">
          <ArtifactHeader info={d} role={role} path={path} />
          <div className="flex flex-wrap gap-2 text-xs">
            <a href={rawUrl(d.id, true)} className="inline-flex h-7 items-center rounded-md border border-line-strong px-2.5 hover:bg-hover">Pobierz oryginał</a>
            <a href={rawUrl(d.id)} target="_blank" rel="noreferrer noopener" className="inline-flex h-7 items-center rounded-md border border-line px-2.5 hover:bg-hover" title="HTML/SVG/JS otwierają się jako zwykły tekst">Otwórz surowe bajty</a>
          </div>
          <Provenance info={d} />
          {focus && (
            <div className={cx("rounded-md border border-against p-2 text-xs")}>
              <div className="flex items-center gap-2"><span className="font-semibold">Wybrany dowód:</span> <span className="font-mono">{focus.kind}</span>
                <button type="button" onClick={() => setFocus(null)} className="ml-auto text-accent hover:underline">wyczyść</button></div>
              {focus.value && <div className="mt-1 font-mono break-all">{focus.value.slice(0, 200)}</div>}
              {focus.evidence?.note && <div className="mt-1 text-subtle">{focus.evidence.note}</div>}
            </div>
          )}
        </div>
      </div>
      <section className="rounded-md border border-line bg-panel p-3">
        <h3 className="mb-2 text-sm font-semibold">Co wyciągnięto z tego artefaktu</h3>
        {genesQ.isPending ? <Note>Ładowanie genów…</Note> : genesQ.data ? <GenesPanel data={genesQ.data} focusId={focus?.id ?? null} onFocus={onFocus} pairHref={pairHref} /> : <Note>Nie udało się pobrać genów.</Note>}
      </section>
    </div>
  );
}
