/**
 * Time scrubber: replays the growth of the constellation by campaign first-seen day. The histogram shows how many
 * campaigns start on each day (from the overview, nothing estimated); undated campaigns are counted and stay faded.
 */
import { useEffect } from "react";
import { cx } from "../../../lib/cx";
import { Button } from "../../Button";
import type { Growth } from "./geometry";

export function TimeScrubber({ growth, cursor, setCursor, playing, setPlaying, reducedMotion }: {
  growth: Growth;
  cursor: number | null;
  setCursor: (c: number | null) => void;
  playing: boolean;
  setPlaying: (p: boolean) => void;
  reducedMotion: boolean;
}) {
  const n = growth.dates.length;
  useEffect(() => {
    if (!playing || n < 2) return;
    const step = Math.max(1, Math.ceil(n / 50)); // a replay lasts ~20 s whatever the span
    const t = window.setInterval(() => {
      const next = (cursor ?? -1) + step;
      if (next >= n - 1) {
        setCursor(n - 1);
        setPlaying(false);
      } else setCursor(next);
    }, reducedMotion ? 900 : 400);
    return () => window.clearInterval(t);
  }, [playing, cursor, n, setCursor, setPlaying, reducedMotion]);

  if (n < 2) {
    return (
      <div className="pointer-events-auto rounded-md border border-line bg-panel/95 px-3 py-1.5 text-xs text-muted shadow-sm">
        Odtwarzanie wzrostu niedostępne: {n === 0 ? "żadna kampania nie ma daty pierwszej obserwacji" : growth.counts[0] === 1 ? "tylko jedna kampania ma datę pierwszej obserwacji" : "wszystkie datowane kampanie zaczynają się tego samego dnia"}
        {growth.undated ? ` (${growth.undated} bez daty)` : ""}.
      </div>
    );
  }
  const max = Math.max(...growth.counts);
  const upto = cursor ?? n - 1;
  const started = growth.counts.slice(0, upto + 1).reduce((s, c) => s + c, 0);
  return (
    <div className="pointer-events-auto flex w-full max-w-[620px] items-center gap-2 rounded-md border border-line bg-panel/95 px-2.5 py-1.5 shadow-sm">
      <Button size="sm" variant="secondary" aria-label={playing ? "Pauza" : "Odtwórz wzrost kampanii w czasie"} onClick={() => { if (!playing && (cursor === null || cursor >= n - 1)) setCursor(0); setPlaying(!playing); }}>
        {playing ? "pauza" : "odtwórz"}
      </Button>
      <div className="relative min-w-0 flex-1">
        <svg aria-hidden="true" className="absolute inset-x-0 -top-0.5 h-3 w-full" preserveAspectRatio="none" viewBox={`0 0 ${n} 10`}>
          {growth.counts.map((c, i) => (
            <rect key={growth.dates[i]} x={i + 0.1} width={0.8} y={10 - (c / max) * 10} height={(c / max) * 10} className={cx(i <= upto ? "fill-accent" : "fill-line-strong")} opacity={0.7} />
          ))}
        </svg>
        <input
          type="range"
          min={0}
          max={n - 1}
          value={upto}
          onChange={(e) => { setPlaying(false); setCursor(Number(e.target.value)); }}
          aria-label="Data: kampanie widoczne do tego dnia"
          aria-valuetext={`${growth.dates[upto]}: ${started} kampanii`}
          className="relative mt-2 w-full"
        />
      </div>
      <span className="w-44 shrink-0 truncate text-right font-mono text-2xs text-muted" aria-live="polite">
        {cursor === null ? `${growth.dates[0]} → ${growth.dates[n - 1]}` : `do ${growth.dates[upto]} · ${started} kamp.`}
      </span>
      {growth.undated > 0 && <span className="hidden shrink-0 text-2xs text-subtle lg:inline" title="Kampanie bez daty pierwszej obserwacji są w odtwarzaniu wyblakłe">{growth.undated} bez daty</span>}
      {cursor !== null && <Button size="sm" variant="ghost" onClick={() => { setPlaying(false); setCursor(null); }}>wszystko</Button>}
    </div>
  );
}
