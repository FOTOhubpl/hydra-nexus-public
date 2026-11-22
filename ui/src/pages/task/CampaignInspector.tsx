import { Link } from "react-router";
import { campaignSupport, useCampaign } from "../../api/task";
import { FamChip, GeneValue, KindLabel, Prov, Thumb } from "../../components/evidence";
import { Panel } from "../../components/Panel";
import { formatBits } from "../../lib/evidence";
import { IncidentLink, QueryState } from "./common";
import { BITS_TITLE, WeakestFirst } from "./support";

/** Right-side inspector for a selected campaign (Kampanie, Graf). */
export function CampaignInspector({ id, onClose }: { id: string; onClose?: () => void }) {
  const q = useCampaign(id);
  const c = q.data;
  const s = campaignSupport(c);
  return (
    <Panel
      title="Inspektor"
      className="sticky top-0"
      actions={onClose && <button type="button" onClick={onClose} aria-label="Zamknij inspektor" className="text-subtle hover:text-fg">✕</button>}
    >
      {!c ? <QueryState query={q} what="kampanię" /> : (
        <div className="space-y-3 text-sm">
          <div>
            <div className="flex items-center gap-2"><Prov value="HYP" /><span className="font-mono text-xs">{c.id}</span></div>
            <div className="mt-1 font-semibold">{c.label}</div>
            <div className="text-xs text-muted">
              {c.members.length} incydentów
              {s && !s.singleton && <> · <span title={BITS_TITLE}>dowody {formatBits(s.median_link_bits)}</span>{s.rank !== null && s.rank !== undefined && <> · ranking heurystyczny #{s.rank} z {s.rank_of}</>}</>}
              {c.group?.label ? ` · grupa: ${c.group.label}` : ""}
            </div>
          </div>
          {s && <WeakestFirst s={s} />}
          <div className="flex flex-wrap gap-1.5">
            {c.member_incidents.slice(0, 9).map((m) => <Thumb key={m.id} artifactId={m.thumbnail} alt={m.id} size={56} to={`/incident/${encodeURIComponent(m.id)}`} />)}
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold text-muted">Najmocniejsze geny rdzenia</div>
            {c.core_genome.length === 0 ? <div className="text-xs text-subtle">brak</div> : (
              <ul className="space-y-1 text-xs">
                {[...c.core_genome].sort((a, b) => b.bits - a.bits).slice(0, 6).map((g, i) => (
                  <li key={i} className="flex items-center gap-1.5">
                    <FamChip family={g.family} /><KindLabel kind={g.kind} className="text-muted" />
                    <span className="min-w-0 flex-1 truncate"><GeneValue value={g.value_a} max={40} /></span>
                    <span className="font-mono">{formatBits(g.bits)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold text-muted">Hosty</div>
            <div className="font-mono text-xs break-all text-fg">
              {Array.from(new Set((["landing_host", "registrable_domain", "domain", "host"] as const).flatMap((k) => c.infrastructure[k] ?? []).map((h) => h.value))).slice(0, 8).join(", ") || "—"}
            </div>
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold text-muted">Członkowie</div>
            <ul className="space-y-0.5 text-xs">{c.member_incidents.slice(0, 12).map((m) => <li key={m.id} className="truncate"><IncidentLink id={m.id} /> <span className="text-muted">{m.title}</span></li>)}</ul>
          </div>
          <Link to={`/campaigns/${encodeURIComponent(c.id)}`} className="inline-block text-accent hover:underline">Otwórz kartę kampanii →</Link>
        </div>
      )}
    </Panel>
  );
}
