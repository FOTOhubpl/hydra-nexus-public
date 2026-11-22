/**
 * Shared-footage timeline: one strip per video with the matched segment highlighted (reason kind `video_segment`,
 * GET /v1/pairs/{a}/{b}/diff → video_segments). Times come from the scorer; the strip length is the last sampled
 * frame of each video (or the segment end when unknown).
 */
import { cx } from "../lib/cx";
import { formatBits, plNumber } from "../lib/evidence";

export interface VideoSegment {
  artifact_a: string | null; artifact_b: string | null;
  start_a: number; end_a: number; start_b: number; end_b: number;
  offset_s: number; frames: number; footage_df: number;
  length_a: number | null; length_b: number | null;
  bits: number | null; explanation_pl: string | null;
}

export function mmss(t: number): string {
  const s = Math.round(Math.max(0, t));
  const m = Math.floor(s / 60);
  return `${m}:${String(s - 60 * m).padStart(2, "0")}`;
}

function Strip({ label, start, end, length, common }: { label: string; start: number; end: number; length: number | null; common: boolean }) {
  const total = Math.max(length ?? 0, end, 1);
  const left = (100 * start) / total;
  const width = Math.max(1.5, (100 * Math.max(0, end - start)) / total);
  return (
    <div className="flex items-center gap-3 text-xs">
      <span className="w-24 shrink-0 truncate font-mono text-muted" title={label}>{label}</span>
      <div className="relative h-4 flex-1 rounded-sm bg-hover" role="img" aria-label={`${label}: wspólny fragment ${mmss(start)}–${mmss(end)} z ${mmss(total)}`}>
        <div className={cx("absolute inset-y-0 rounded-sm", common ? "bg-line-strong" : "bg-for")} style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }} />
      </div>
      <span className="w-28 shrink-0 text-right font-mono text-subtle">{mmss(start)}–{mmss(end)} / {mmss(total)}</span>
    </div>
  );
}

export function VideoSegmentStrip({ segment, a, b }: { segment: VideoSegment; a: string; b: string }) {
  const common = segment.bits === 0 && segment.footage_df > 2;
  return (
    <div className="space-y-1.5" data-testid="video-segment">
      <Strip label={a} start={segment.start_a} end={segment.end_a} length={segment.length_a} common={common} />
      <Strip label={b} start={segment.start_b} end={segment.end_b} length={segment.length_b} common={common} />
      <div className="flex flex-wrap items-center gap-x-3 text-xs text-muted">
        <span>{segment.frames} dopasowanych klatek</span>
        <span>przesunięcie {segment.offset_s >= 0 ? "+" : "−"}{plNumber(Math.abs(segment.offset_s), 1)} s</span>
        {common
          ? <span>materiał wspólny dla {segment.footage_df} incydentów (stock / TV), nie liczy się do powiązania</span>
          : <span className="font-mono">{formatBits(segment.bits)}</span>}
      </div>
    </div>
  );
}
