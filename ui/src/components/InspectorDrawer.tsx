import { useState } from "react";
import { Link } from "react-router";
import { useIncident, usePair } from "../api/task";
import { inspector, useInspector } from "../lib/inspector";
import { formatBits, formatProbability } from "../lib/evidence";
import { CampaignInspector } from "../pages/task/CampaignInspector";
import { AiAnalystPanel } from "./AiAnalystPanel";
import { HypothesisBadge, ProbBar, Thumb, Waterfall } from "./evidence";
import { Icon } from "./Icon";
import { RobustnessBadge, type Robustness } from "./RobustnessBadge";

function PairBody({ a, b }: { a: string; b: string }) {
  const q = usePair(a, b);
  const [explain, setExplain] = useState(false);
  const s = q.data?.score;
  if (!s) return <div className="p-4 text-sm text-subtle">{q.isError ? "Para nie została oceniona w tym przebiegu." : "Ładowanie…"}</div>;
  const rob = (s as unknown as { robustness?: Robustness }).robustness;
  return (
    <div className="space-y-4 p-4">
      <div className="grid grid-cols-2 gap-2">
        {[q.data?.incident_a, q.data?.incident_b].map((side, i) => (
          <Link key={i} to={`/incident/${encodeURIComponent(side?.id ?? "")}`} className="min-w-0">
            <Thumb artifactId={side?.visuals?.[0]?.artifact_id ?? side?.thumbnail} alt={side?.id ?? ""} size={160} className="!h-28 !w-full" />
            <div className="mt-1 truncate font-mono text-xs">{side?.id}</div>
          </Link>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-2xl font-semibold">{formatProbability(s.probability)}</span>
        <HypothesisBadge value={s.hypothesis} />
        <RobustnessBadge r={rob} />
      </div>
      <ProbBar p={s.probability} calibrated={s.calibrated} />
      <Waterfall familyBits={s.family_bits} totalBits={s.total_bits} missing={Array.from(new Set([...(s.unknown_families ?? []), ...s.missing_families]))} reasons={s.reasons} contradictions={s.contradictions} />
      <div className="text-xs text-muted">suma {formatBits(s.total_bits)} · model <span className="font-mono">{s.model}</span></div>
      <div className="flex gap-2">
        <Link to={`/pair/${encodeURIComponent(a)}/${encodeURIComponent(b)}`} className="inline-flex h-9 items-center rounded-md bg-fg px-3 text-sm text-canvas">Pełny dowód pary</Link>
        <button type="button" onClick={() => setExplain(!explain)} className="inline-flex h-9 items-center rounded-md border border-line-strong px-3 text-sm hover:bg-hover">{explain ? "Ukryj wyjaśnienie" : "Wyjaśnij (AI)"}</button>
      </div>
      {explain && <AiAnalystPanel kind="pair" a={a} b={b} />}
    </div>
  );
}

function IncidentBody({ id }: { id: string }) {
  const q = useIncident(id);
  const inc = q.data;
  if (!inc) return <div className="p-4 text-sm text-subtle">Ładowanie…</div>;
  return (
    <div className="space-y-3 p-4 text-sm">
      <Thumb artifactId={inc.thumbnail} alt={inc.id} size={320} className="!h-44 !w-full" />
      <div className="font-medium">{inc.title ?? inc.id}</div>
      <div className="text-xs text-muted">kampania: {inc.campaign_id ? <Link className="font-mono text-accent hover:underline" to={`/campaigns/${encodeURIComponent(inc.campaign_id)}`}>{inc.campaign_id}</Link> : "—"}</div>
      <ul className="space-y-1 text-xs">
        {inc.neighbours.slice(0, 6).map((n) => (
          <li key={n.other} className="flex items-center gap-2">
            <button type="button" onClick={() => inspector.open({ kind: "pair", a: id, b: n.other })} className="font-mono text-accent hover:underline">{n.other}</button>
            <HypothesisBadge value={n.hypothesis} />
            <span className="ml-auto font-mono">{formatBits(n.total_bits)}</span>
          </li>
        ))}
      </ul>
      <Link to={`/incident/${encodeURIComponent(id)}`} className="inline-flex h-9 items-center rounded-md bg-fg px-3 text-sm text-canvas">Otwórz incydent</Link>
    </div>
  );
}

/** Right inspector drawer: whatever was selected anywhere (graph node/edge, dashboard tile, campaign row). */
export function InspectorDrawer() {
  const sel = useInspector();
  if (!sel) return null;
  const title = sel.kind === "pair" ? `${sel.a} ↔ ${sel.b}` : sel.id;
  return (
    <aside aria-label="Inspektor" className="flex w-[400px] shrink-0 flex-col border-l border-line bg-panel">
      <div className="flex h-14 items-center gap-2 border-b border-line px-4">
        <span className="text-xs font-medium text-subtle">Inspektor</span>
        <span className="truncate font-mono text-sm">{title}</span>
        <button type="button" onClick={() => inspector.close()} aria-label="Zamknij inspektor" className="ml-auto flex size-8 items-center justify-center rounded-md text-muted hover:bg-hover"><Icon name="close" /></button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {sel.kind === "pair" && <PairBody a={sel.a} b={sel.b} />}
        {sel.kind === "incident" && <IncidentBody id={sel.id} />}
        {sel.kind === "campaign" && <div className="p-3"><CampaignInspector id={sel.id} /></div>}
      </div>
    </aside>
  );
}
