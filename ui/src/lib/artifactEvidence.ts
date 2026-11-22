/**
 * Pure helpers that turn stored genes / evidence pointers into what the artefact viewer draws: bbox layers on
 * images and video frames, highlighted spans in page sources and transcripts. Nothing here invents a location:
 * a box is drawn only where a gene carries a bbox, a span only where its text actually occurs.
 */
import type { EvidencePointer, Gene } from "../api/task";
import { isWholeFile, WHOLE_FILE_TEXT } from "./displayPl";

export type LayerKey = "ocr" | "logo" | "face" | "region" | "narrative" | "other";

export const LAYER_LABEL: Record<LayerKey, string> = {
  ocr: "tekst OCR",
  logo: "logo / marki",
  face: "twarze / osoby",
  region: "regiony obrazu",
  narrative: "narracja",
  other: "inne",
};

export const LAYER_COLOR: Record<LayerKey, string> = {
  ocr: "var(--color-fam-txt)",
  logo: "var(--color-fam-meta)",
  face: "var(--color-fam-id)",
  region: "var(--color-fam-vis)",
  narrative: "var(--color-fam-nar)",
  other: "var(--color-fam-inf)",
};

const LOGO_KINDS = new Set(["logo", "media_brand", "institution_brand", "brand_mention", "platform", "platform_page_name"]);
const FACE_KINDS = new Set(["face", "person_named", "person_candidate", "persona_match", "persona_renamed", "commenter_name"]);
const REGION_KINDS = new Set(["dinov2", "pdq", "pdq_canon", "phash", "phash_tiles", "image_class"]);

export function layerOf(g: Pick<Gene, "family" | "kind">): LayerKey {
  if (LOGO_KINDS.has(g.kind)) return "logo";
  if (FACE_KINDS.has(g.kind)) return "face";
  if (REGION_KINDS.has(g.kind)) return "region";
  if (g.kind === "ocr_line" || g.family === "TXT") return "ocr";
  if (g.family === "NAR") return "narrative";
  return "other";
}

export interface BoxMark {
  key: string;
  layer: LayerKey;
  bbox: [number, number, number, number];
  label: string;
  genes: Gene[];
  timecode: number | null;
}

/** One mark per distinct bbox (+ layer); OCR and the phrases derived from the same line share it. */
export function boxMarks(genes: readonly Gene[], opts: { timecode?: number | null; tolerance?: number; scaleOf?: (g: Gene) => number } = {}): BoxMark[] {
  const tol = opts.tolerance ?? 0.55;
  const out = new Map<string, BoxMark>();
  for (const g of genes) {
    const raw = g.evidence?.bbox;
    const s = opts.scaleOf?.(g) ?? 1;
    const b = raw && s > 0 && s !== 1 ? (raw.map((v) => v / s) as [number, number, number, number]) : raw;
    if (!b || b.length !== 4 || !b.every(Number.isFinite) || b[2] <= 0 || b[3] <= 0) continue;
    const t = g.evidence?.timecode_s ?? null;
    if (opts.timecode !== undefined) {
      if (opts.timecode === null ? t !== null : t === null || Math.abs(t - opts.timecode) > tol) continue;
    }
    const layer = layerOf(g);
    const key = `${layer}:${raw!.map((v) => Math.round(v)).join(",")}:${t ?? ""}`;
    const m = out.get(key);
    if (m) {
      m.genes.push(g);
      continue;
    }
    out.set(key, { key, layer, bbox: [b[0], b[1], b[2], b[3]], label: markLabel(g), genes: [g], timecode: t });
  }
  return [...out.values()];
}

function markLabel(g: Gene): string {
  if (g.kind === "ocr_line") return (g.evidence?.note || g.value || "").slice(0, 40);
  if (g.kind === "face") return "twarz";
  if (g.kind === "dinov2" || g.kind === "pdq" || g.kind === "phash") {
    const cls = /class=([\w-]+)/.exec(g.evidence?.note ?? "")?.[1];
    return cls ? `region: ${cls}` : g.kind;
  }
  return `${g.kind}${g.value ? `: ${g.value.slice(0, 32)}` : ""}`;
}

/**
 * Text genes read from video frames (OCR in hydra/genome/txt_sources.py and the NAR/TXT genes derived from those
 * lines) carry bboxes in the working-frame space (longest side <= 1280 px), while VIS/ID/logo units are mapped back
 * to source pixels (SPEC_ERRATA E-ART-1). Returns the factor that maps a gene's bbox to source pixels (divide by it).
 */
export function videoBboxScale(g: Pick<Gene, "family" | "evidence">, width: number | null | undefined, height: number | null | undefined): number {
  if (!width || !height || g.evidence?.timecode_s === null || g.evidence?.timecode_s === undefined) return 1;
  if (g.family !== "TXT" && g.family !== "NAR") return 1;
  return Math.min(1, 1280 / Math.max(width, height));
}

/** Sampled timecodes reduced to at most `n` evenly spread keyframes. */
export function pickKeyframes(times: readonly number[], duration: number | null | undefined, n = 12): number[] {
  const uniq = [...new Set(times.map((t) => Math.round(t * 10) / 10))].sort((a, b) => a - b);
  if (uniq.length === 0) {
    if (!duration || duration <= 0) return [];
    const step = duration / n;
    return Array.from({ length: n }, (_, i) => Math.round((i * step + step / 2) * 10) / 10);
  }
  if (uniq.length <= n) return uniq;
  return Array.from({ length: n }, (_, i) => uniq[Math.round((i * (uniq.length - 1)) / (n - 1))]!);
}

export function mmss(t: number | null | undefined): string {
  if (t === null || t === undefined || !Number.isFinite(t)) return "—";
  const s = Math.max(0, t);
  const m = Math.floor(s / 60);
  const sec = s - 60 * m;
  return `${m}:${sec < 10 ? "0" : ""}${sec.toFixed(1).replace(".", ",")}`;
}

// ------------------------------------------------------------------ text highlighting

export type SpanKind = "url" | "phone" | "tracker" | "email" | "gene" | "focus";

export const SPAN_LABEL: Record<SpanKind, string> = {
  url: "adres URL",
  phone: "telefon",
  tracker: "identyfikator śledzący",
  email: "e-mail",
  gene: "wartość genu",
  focus: "wybrany dowód",
};

const GENERIC: [SpanKind, RegExp][] = [
  ["url", /\bhttps?:\/\/[^\s"'<>()\\]{3,300}/gi],
  ["email", /\b[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+\b/g],
  ["tracker", /\b(?:G-[A-Z0-9]{6,12}|UA-\d{4,10}-\d{1,4}|GTM-[A-Z0-9]{4,9}|AW-\d{6,12})\b|fbq\(\s*['"]init['"]\s*,\s*['"]\d{8,20}['"]/g],
  ["phone", /(?<![\w/.=-])\+?\d{2,3}[ -]?\d{2,3}[ -]\d{3}[ -]?\d{2,4}(?![\w/.-])/g],
];

export interface Span { start: number; end: number; kind: SpanKind; title?: string }

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Literal strings worth finding in a page source / text for the genes of this artefact. */
export function geneTerms(genes: readonly Gene[]): { term: string; title: string }[] {
  const out = new Map<string, string>();
  for (const g of genes) {
    if (g.status !== "observed") continue;
    let term: string | null = null;
    if (g.kind === "tracker_id" && g.value?.includes(":")) term = g.value.split(":").slice(1).join(":");
    else if (g.kind.startsWith("entity_") && g.evidence?.note) term = g.evidence.note.split(" (")[0]!.trim();
    else if (["url_in_text", "domain_in_text", "linked_host", "operator_id", "script_name", "form_action", "page_title", "headline", "byline", "cta"].includes(g.kind)) term = g.value;
    if (term && term.length >= 4 && term.length <= 200 && !out.has(term)) out.set(term, `${g.family} ${g.kind}`);
  }
  return [...out.entries()].map(([term, title]) => ({ term, title }));
}

/** Non-overlapping spans, earliest first; `focus` wins over everything, gene terms over generic patterns. */
export function findSpans(text: string, terms: readonly { term: string; title: string }[] = [], focus?: string | null, limit = 5000): Span[] {
  const all: (Span & { prio: number })[] = [];
  const push = (re: RegExp, kind: SpanKind, prio: number, title?: string) => {
    re.lastIndex = 0;
    let m: RegExpExecArray | null;
    let n = 0;
    while ((m = re.exec(text)) && n < limit) {
      if (m[0].length === 0) {
        re.lastIndex++;
        continue;
      }
      all.push({ start: m.index, end: m.index + m[0].length, kind, title, prio });
      n++;
    }
  };
  if (focus && focus.trim().length >= 2) push(new RegExp(escapeRe(focus.trim()), "gi"), "focus", 0, "wybrany dowód");
  for (const t of terms) push(new RegExp(escapeRe(t.term), "g"), "gene", 1, t.title);
  for (const [kind, re] of GENERIC) push(new RegExp(re.source, re.flags), kind, 2);
  all.sort((a, b) => a.start - b.start || a.prio - b.prio || b.end - a.end);
  const out: Span[] = [];
  let lastEnd = -1;
  // greedy by priority: take focus/gene spans first, then fill generic ones where free
  const taken: Span[] = [];
  for (const prio of [0, 1, 2]) {
    for (const s of all) {
      if (s.prio !== prio) continue;
      if (taken.some((t) => s.start < t.end && t.start < s.end)) continue;
      taken.push({ start: s.start, end: s.end, kind: s.kind, title: s.title });
    }
  }
  taken.sort((a, b) => a.start - b.start);
  for (const s of taken) {
    if (s.start >= lastEnd) {
      out.push(s);
      lastEnd = s.end;
    }
  }
  return out;
}

/** Short human description of where an evidence pointer points. */
export function pointerText(e: EvidencePointer | null | undefined, kind?: string | null): string {
  if (!e) return "";
  const parts: string[] = [];
  const whole = isWholeFile(e, kind);
  if (whole) parts.push(WHOLE_FILE_TEXT);
  if (e.bbox && !whole) parts.push(`ramka ${e.bbox.map((v) => Math.round(v)).join(",")}`);
  if (e.timecode_s !== null && e.timecode_s !== undefined && !whole) parts.push(`t=${mmss(e.timecode_s)}`);
  if (e.char_range) parts.push(`znaki ${e.char_range[0]}–${e.char_range[1]}`);
  if (e.har_entry !== null && e.har_entry !== undefined) parts.push(`HAR #${e.har_entry}`);
  if (e.dom_path) parts.push(`DOM ${e.dom_path.length > 48 ? `…${e.dom_path.slice(-46)}` : e.dom_path}`);
  return parts.join(" · ");
}

/** Text to look for in a source when the user focuses a gene (the raw form in the note beats the normalised value). */
export function focusTermOf(g: Pick<Gene, "kind" | "value" | "evidence">): string | null {
  const note = g.evidence?.note ?? "";
  if (g.kind.startsWith("entity_") && note) return note.split(" (")[0]!.trim();
  if (g.kind === "tracker_id" && g.value?.includes(":")) return g.value.split(":").slice(1).join(":");
  if (note && !/[=;]/.test(note) && note.length >= 3 && note.length <= 200) return note;
  return g.value && g.value.length <= 200 ? g.value : null;
}
