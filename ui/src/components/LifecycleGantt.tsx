/**
 * Campaign lifecycle (GET /v1/campaigns/{id}/lifecycle): one row per incident on a shared time axis.
 * ● observed date of the material, ◆ domain registration (RDAP), ▲ first certificate (CT), – span from the
 * earliest preparation date to the observation. Dates with month precision sit on the 1st of the month.
 */
import type { Lifecycle } from "../api/visual";
import { formatDatePl } from "../lib/evidence";

function parse(d: string | null | undefined): number | null {
  if (!d) return null;
  const s = /^\d{4}-\d{2}$/.test(d) ? `${d}-01` : d;
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
}

const DAY = 86_400_000;

export function LifecycleGantt({ lc }: { lc: Lifecycle }) {
  const rows = lc.incidents.map((i) => {
    const obs = parse(i.observed_at);
    const marks = lc.dates.filter((d) => d.incident_id === i.id).map((d) => ({ ...d, t: parse(d.date) })).filter((m) => m.t !== null) as (Lifecycle["dates"][number] & { t: number })[];
    return { ...i, obs, marks };
  });
  const all = rows.flatMap((r) => [r.obs, ...r.marks.map((m) => m.t)]).filter((t): t is number => t !== null);
  if (all.length === 0) return <div className="text-sm text-subtle">Brak dat w materiale i w faktach wzbogacenia: cyklu życia nie da się narysować.</div>;
  const t0 = Math.min(...all) - 3 * DAY, t1 = Math.max(...all) + 3 * DAY;
  const W = 900, L = 120, R = 16, rowH = 26;
  const x = (t: number) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const H = rows.length * rowH + 40;
  const reuse = new Set(lc.asset_reuse.flatMap((a) => a.incidents));
  const fmt = (t: number) => new Date(t).toLocaleDateString("pl-PL", { day: "numeric", month: "short", year: "numeric" });
  const ticks = 5;
  return (
    <div className="space-y-2">
      <svg viewBox={`0 0 ${W} ${H}`} width={W} className="max-w-full" role="img" aria-label="Cykl życia kampanii na osi czasu">
        {Array.from({ length: ticks + 1 }, (_, k) => { const t = t0 + ((t1 - t0) * k) / ticks; return <g key={k}><line x1={x(t)} x2={x(t)} y1={8} y2={H - 26} stroke="var(--color-line)" /><text x={x(t)} y={H - 10} fontSize={10} textAnchor={k === 0 ? "start" : k === ticks ? "end" : "middle"} className="fill-subtle">{fmt(t)}</text></g>; })}
        {rows.map((r, i) => {
          const y = 18 + i * rowH;
          const prep = r.marks.length ? Math.min(...r.marks.map((m) => m.t)) : null;
          return (
            <g key={r.id}>
              <text x={L - 8} y={y + 4} fontSize={11} textAnchor="end" className="fill-fg font-mono">{r.id}{reuse.has(r.id) ? " ⧉" : ""}</text>
              {prep !== null && r.obs !== null && <line x1={x(prep)} x2={x(r.obs)} y1={y} y2={y} stroke="var(--color-accent)" strokeWidth={2} strokeOpacity={0.5}><title>{`przygotowanie → reklama: ${Math.round((r.obs - prep) / DAY)} dni`}</title></line>}
              {r.marks.map((m, k) => m.kind.startsWith("registration")
                ? <rect key={k} x={x(m.t) - 5} y={y - 5} width={10} height={10} transform={`rotate(45 ${x(m.t)} ${y})`} fill="var(--color-fam-meta)" stroke="var(--color-panel)" strokeWidth={2}><title>{`rejestracja domeny${m.subject ? ` ${m.subject}` : ""}: ${m.date}`}</title></rect>
                : m.kind.startsWith("cert") ? <path key={k} d={`M${x(m.t)},${y - 6} L${x(m.t) + 6},${y + 5} L${x(m.t) - 6},${y + 5} Z`} fill="var(--color-fam-vis)" stroke="var(--color-panel)" strokeWidth={1.5}><title>{`pierwszy certyfikat${m.subject ? ` ${m.subject}` : ""}: ${m.date}`}</title></path>
                : <circle key={k} cx={x(m.t)} cy={y} r={4} fill="var(--color-muted)"><title>{`${m.kind}: ${m.date}`}</title></circle>)}
              {r.obs !== null ? <circle cx={x(r.obs)} cy={y} r={6} fill="var(--color-for)" stroke="var(--color-panel)" strokeWidth={2}><title>{`${r.id} zaobserwowany ${formatDatePl(r.observed_at)}`}</title></circle>
                : <text x={L + 4} y={y + 4} fontSize={10} className="fill-subtle">data obserwacji nieznana</text>}
            </g>
          );
        })}
      </svg>
      <div className="flex flex-wrap gap-4 text-xs text-muted">
        <span className="inline-flex items-center gap-1.5"><span className="inline-block size-3 rounded-full bg-for" />materiał zaobserwowany</span>
        <span className="inline-flex items-center gap-1.5"><span className="inline-block size-2.5 rotate-45 bg-fam-meta" />rejestracja domeny (RDAP)</span>
        <span className="inline-flex items-center gap-1.5"><span className="inline-block size-0 border-x-[6px] border-b-[10px] border-x-transparent border-b-fam-vis" />pierwszy certyfikat (CT)</span>
        <span className="inline-flex items-center gap-1.5"><span className="inline-block h-0.5 w-5 bg-accent/60" />przygotowanie → reklama</span>
        <span>⧉ ten sam plik u kilku członków</span>
        <span>pierwsze: {formatDatePl(lc.first_seen)} · ostatnie: {formatDatePl(lc.last_seen)}</span>
      </div>
      {lc.asset_reuse.length > 0 && (
        <ul className="space-y-0.5 text-xs text-muted">
          {lc.asset_reuse.slice(0, 5).map((a) => <li key={a.value}><span className="font-mono">{a.value.slice(0, 16)}…</span> ({a.kind === "artifact_sha256" ? "identyczny plik materiału" : "identyczny zasób strony"}) w {a.incidents.join(", ")}</li>)}
        </ul>
      )}
    </div>
  );
}
