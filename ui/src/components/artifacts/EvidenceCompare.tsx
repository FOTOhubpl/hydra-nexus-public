/**
 * Side-by-side evidence for a pair: for each scorer reason that points into artefacts, both artefacts are shown
 * with the matched place highlighted (bbox on the image or video frame, the text span in the page source, the
 * request in the HAR log, the moment on the timeline). Only stored evidence pointers are used.
 */
import { useMemo, useState } from "react";
import { frameUrl, rawUrl, useArtifactInfo, useArtifactText, useHar, type ArtifactInfo } from "../../api/artifacts";
import type { EvidencePointer, GeneMatch } from "../../api/task";
import { layerOf, mmss, pointerText, videoBboxScale, type BoxMark } from "../../lib/artifactEvidence";
import { cx } from "../../lib/cx";
import { formatBits } from "../../lib/evidence";
import { FamChip, GeneValue } from "../evidence";
import { AnnotatedImage } from "./AnnotatedImage";
import { isWholeFile, WHOLE_FILE_TEXT } from "../../lib/displayPl";

function hasPointer(e: EvidencePointer | null | undefined): boolean {
  return Boolean(e?.artifact_id);
}

/** Reasons with an artefact pointer on at least one side, strongest first, one per kind+values. */
export function locatedReasons(reasons: readonly GeneMatch[]): GeneMatch[] {
  const seen = new Set<string>();
  return [...reasons]
    .filter((r) => hasPointer(r.evidence_a) || hasPointer(r.evidence_b))
    .sort((x, y) => Math.abs(y.bits) - Math.abs(x.bits))
    .filter((r) => {
      const k = `${r.kind}|${r.value_a}|${r.value_b}|${r.evidence_a?.artifact_id}|${r.evidence_b?.artifact_id}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
}

function markOf(r: GeneMatch, e: EvidencePointer, scale = 1): BoxMark[] {
  if (!e.bbox) return [];
  const b = e.bbox.map((v) => v / scale) as [number, number, number, number];
  return [{ key: "m", layer: layerOf(r), bbox: b, label: `${r.kind}`, genes: [], timecode: e.timecode_s ?? null }];
}

function TimeBar({ t, duration }: { t: number; duration: number | null | undefined }) {
  if (!duration) return null;
  return (
    <div className="relative mt-1 h-3 rounded-sm bg-hover" role="img" aria-label={`moment ${mmss(t)} z ${mmss(duration)}`}>
      <span className="absolute inset-y-0 w-1.5 -translate-x-1/2 rounded-[1px] bg-against" style={{ left: `${(100 * t) / duration}%` }} />
    </div>
  );
}

function TextSnippet({ info, value, pointer }: { info: ArtifactInfo; value: string | null | undefined; pointer: EvidencePointer }) {
  const q = useArtifactText(info.id);
  const text = q.data?.text ?? "";
  const termsKey = [pointer.note && !/[=;]/.test(pointer.note) ? pointer.note.split(" (")[0] : null, value].filter((s): s is string => Boolean(s && s.trim().length >= 2)).join("\u0000");
  const hit = useMemo(() => {
    const low = text.toLowerCase();
    for (const term of termsKey ? termsKey.split("\u0000") : []) {
      const i = low.indexOf(term.toLowerCase());
      if (i >= 0) return { i, len: term.length };
    }
    return null;
  }, [text, termsKey]);
  if (q.isPending) return <div className="p-2 text-xs text-subtle">Ładowanie tekstu…</div>;
  if (!hit) return <div className="p-2 text-xs text-subtle">Wartość jest znormalizowana (np. małe litery, bez interpunkcji) i nie występuje dosłownie w źródle{pointer.dom_path ? <>; miejsce: <span className="font-mono">{pointer.dom_path}</span></> : ""}.</div>;
  const from = Math.max(0, hit.i - 220);
  const to = Math.min(text.length, hit.i + hit.len + 220);
  return (
    <pre className="max-h-56 overflow-auto p-2 font-mono text-2xs leading-4 break-all whitespace-pre-wrap">
      {from > 0 && "…"}{text.slice(from, hit.i)}<mark className="rounded-[2px] bg-against-soft px-px text-fg outline outline-1 outline-against">{text.slice(hit.i, hit.i + hit.len)}</mark>{text.slice(hit.i + hit.len, to)}{to < text.length && "…"}
    </pre>
  );
}

function HarRowView({ info, entry }: { info: ArtifactInfo; entry: number }) {
  const q = useHar(info.id);
  if (q.isPending) return <div className="p-2 text-xs text-subtle">Ładowanie HAR…</div>;
  const rows = (q.data?.rows ?? []).filter((r) => Math.abs(r.idx - entry) <= 2);
  if (rows.length === 0) return <div className="p-2 text-xs text-subtle">Żądanie #{entry} poza zakresem tabeli.</div>;
  return (
    <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-2xs whitespace-nowrap">
      <tbody>
        {rows.map((r) => (
          <tr key={r.idx} className={cx("border-t border-line first:border-t-0", r.idx === entry && "bg-against-soft font-semibold")}>
            <td className="px-1 py-0.5 font-mono text-subtle">#{r.idx}</td><td className="px-1 font-mono">{r.host}</td>
            <td className="max-w-[200px] truncate px-1 font-mono text-muted" title={r.url ?? undefined}>{r.url}</td><td className="px-1 font-mono">{r.status}</td><td className="px-1 text-muted">{r.mime}</td>
          </tr>
        ))}
      </tbody>
    </table></div>
  );
}

export function EvidenceSide({ letter, incidentId, r, pointer, value }: { letter: string; incidentId: string; r: GeneMatch; pointer: EvidencePointer | null | undefined; value: string | null | undefined }) {
  const info = useArtifactInfo(pointer?.artifact_id);
  const head = (
    <div className="flex min-w-0 items-center gap-2 border-b border-line px-2 py-1 text-xs">
      <span className="flex size-5 shrink-0 items-center justify-center rounded-[3px] border border-line-strong font-mono">{letter}</span>
      <span className="font-mono">{incidentId}</span>
      <span className="min-w-0 truncate text-muted" title={info.data?.incidents.find((l) => l.incident_id === incidentId)?.path_in_incident}>{info.data?.incidents.find((l) => l.incident_id === incidentId)?.path_in_incident ?? ""}</span>
    </div>
  );
  if (!pointer?.artifact_id) {
    return <div className="rounded-md border border-line">{head}<div className="p-3 text-xs text-subtle">Ta strona dopasowania nie wskazuje artefaktu (gen policzony na poziomie incydentu). Wartość: <GeneValue value={value} max={80} /></div></div>;
  }
  const d = info.data;
  let body;
  if (!d) body = <div className="p-3 text-xs text-subtle">{info.isError ? "Artefakt niedostępny." : "Ładowanie…"}</div>;
  else if (d.kind === "image") {
    body = <AnnotatedImage src={rawUrl(d.id)} alt={`${letter}: ${r.kind}`} marks={markOf(r, pointer)} focusKey="m" space={d.media?.width && d.media?.height ? { w: d.media.width, h: d.media.height } : null} controls maxHeight="50vh" />;
  } else if (r.kind.startsWith("audio") && (d.kind === "video" || d.kind === "audio")) {
    body = <div className="p-2"><audio src={rawUrl(d.id)} controls preload="metadata" className="w-full" /><div className="mt-1 text-2xs text-subtle">dopasowanie dotyczy ścieżki dźwiękowej ({mmss(d.media?.duration_s)}), nie konkretnej klatki</div></div>;
  } else if (d.kind === "video" && pointer.timecode_s !== null && pointer.timecode_s !== undefined && !isWholeFile(pointer, r.kind, { w: d.media?.width, h: d.media?.height })) {
    const s = videoBboxScale({ family: r.family, evidence: pointer }, d.media?.width, d.media?.height);
    body = (
      <div>
        <AnnotatedImage src={frameUrl(d.id, pointer.timecode_s, 1280)} alt={`${letter}: klatka ${mmss(pointer.timecode_s)}`} marks={markOf(r, pointer, s)} focusKey="m" space={d.media?.width && d.media?.height ? { w: d.media.width, h: d.media.height } : null} maxHeight="50vh" />
        <div className="px-2 pb-2 text-2xs text-muted">klatka {mmss(pointer.timecode_s)} / {mmss(d.media?.duration_s)}<TimeBar t={pointer.timecode_s} duration={d.media?.duration_s} /></div>
      </div>
    );
  } else if (d.kind === "video" || d.kind === "audio") {
    body = <div className="p-2"><video src={rawUrl(d.id)} controls preload="metadata" className="max-h-64 w-full bg-black" /><div className="text-2xs text-subtle">{isWholeFile(pointer, r.kind, { w: d.media?.width, h: d.media?.height }) ? `${WHOLE_FILE_TEXT}: dopasowanie dotyczy całego nagrania, nie klatki` : "dopasowanie dotyczy całego nagrania (bez kodu czasu)"}</div></div>;
  } else if (d.kind === "har" && pointer.har_entry !== null && pointer.har_entry !== undefined) {
    body = <HarRowView info={d} entry={pointer.har_entry} />;
  } else if (d.kind === "html" || d.kind === "text" || d.kind === "json" || d.kind === "har") {
    body = <TextSnippet info={d} value={value} pointer={pointer} />;
  } else body = <div className="p-3 text-xs text-subtle">Brak podglądu dla {d.mime}.</div>;
  return (
    <div className="min-w-0 overflow-hidden rounded-md border border-line bg-panel">
      {head}
      {body}
      <div className="border-t border-line px-2 py-1 text-2xs text-muted"><GeneValue value={value} max={90} />{pointerText(pointer, r.kind) && <> · {pointerText(pointer, r.kind)}</>}</div>
    </div>
  );
}

export function EvidenceCompare({ a, b, reasons }: { a: string; b: string; reasons: readonly GeneMatch[] }) {
  const rows = useMemo(() => locatedReasons(reasons), [reasons]);
  const [sel, setSel] = useState(0);
  const [all, setAll] = useState(false);
  if (rows.length === 0) return <div className="text-xs text-subtle">Żaden zapisany powód nie wskazuje miejsca w artefaktach (geny policzone na poziomie incydentu lub osadzenia).</div>;
  const r = rows[Math.min(sel, rows.length - 1)]!;
  const shown = all ? rows : rows.slice(0, 14);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-1.5" role="listbox" aria-label="Dopasowania ze wskazaniem miejsca">
        {shown.map((m, i) => (
          <button key={i} type="button" role="option" aria-selected={i === sel} onClick={() => setSel(i)} title={m.explanation_pl}
            className={cx("inline-flex items-center gap-1 rounded-[4px] border px-1.5 py-0.5 text-xs", i === sel ? "border-accent bg-active" : "border-line hover:bg-hover")}>
            <FamChip family={m.family} /> <span className="text-muted">{m.kind}</span> <span className={cx("font-mono", m.bits < 0 ? "text-err" : "")}>{formatBits(m.bits)}</span>
          </button>
        ))}
        {rows.length > shown.length && <button type="button" onClick={() => setAll(true)} className="text-xs text-accent hover:underline">+{rows.length - shown.length}</button>}
      </div>
      <div className="text-xs text-muted">{r.explanation_pl}{r.similarity !== null && r.similarity !== undefined ? ` · podobieństwo ${r.similarity.toFixed(2).replace(".", ",")}` : ""}{r.doc_freq ? ` · df ${r.doc_freq}` : ""}</div>
      <div className="grid gap-3 md:grid-cols-2">
        <EvidenceSide letter="A" incidentId={a} r={r} pointer={r.evidence_a} value={r.value_a} />
        <EvidenceSide letter="B" incidentId={b} r={r} pointer={r.evidence_b} value={r.value_b ?? r.value_a} />
      </div>
    </div>
  );
}
