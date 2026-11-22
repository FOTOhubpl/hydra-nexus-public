import { useState } from "react";
import { useNavigate } from "react-router";
import { CampaignInspector } from "./CampaignInspector";
import { campaignSupport, useCampaignGroups, useCampaigns, useLatestRun, type Campaign } from "../../api/task";
import { CampaignLink } from "./common";
import { DataTable, type Column } from "../../components/DataTable";
import { FamChip, KindLabel, Prov, SectionNote } from "../../components/evidence";
import { schemesPl } from "../../lib/labelsPl";
import { Panel } from "../../components/Panel";
import { StatTile } from "../../components/StatTile";
import { formatBits, formatDatePl, reasonKindLabel } from "../../lib/evidence";
import { formatCount } from "../../lib/format";
import { NewCount, NoRun, Page, QueryState } from "./common";
import { BITS_TITLE, BridgeMarker, groupEvidenceBits, SUPPORT_DISCLAIMER, WEAKEST_TITLE } from "./support";

const columns: Column<Campaign>[] = [
  { key: "id", header: "Kod", mono: true, width: "120px", sortable: true },
  {
    key: "label",
    header: "Kampania",
    sortable: true,
    width: "34%",
    render: (c) => (
      <span className="inline-flex items-center gap-2">
        <Prov value="HYP" />
        <span className="text-fg">{c.label}</span>
      </span>
    ),
  },
  { key: "group_id", header: "Grupa kampanii", sortable: true, mono: true, width: "160px", title: "grupa podobnych kampanii (wspólny kit/operator)", render: (c) => (c.group_id ? <span className="block max-w-[150px] truncate text-xs" title={`${c.group_id}: ${c.group_label ?? ""}`}>{c.group_id}</span> : <span className="text-subtle">—</span>) },
  { key: "size", header: "Incydentów", align: "right", sortable: true, mono: true, width: "96px", render: (c) => <>{c.size ?? c.members.length}<NewCount n={c.new} className="ml-1" /></> },
  { key: "scheme", header: "Schemat", sortable: true, render: (c) => c.scheme ? <span title={`kod: ${c.scheme}`}>{schemesPl(c.scheme)}</span> : <span className="text-subtle">—</span> },
  {
    key: "core",
    header: "Najmocniejsze geny rdzenia",
    render: (c) =>
      c.core_genome.length === 0 ? (
        <span className="text-subtle">—</span>
      ) : (
        <span className="flex flex-wrap gap-x-3 gap-y-0.5">
          {c.core_genome.slice(0, 3).map((g, i) => (
            <span key={i} className="inline-flex items-center gap-1">
              <FamChip family={g.family} />
              <span className="max-w-[180px] truncate font-mono text-xs text-muted" title={g.value_a ?? ""}>{g.value_a ?? reasonKindLabel(g.kind)}</span>
            </span>
          ))}
        </span>
      ),
  },
  { key: "first_seen", header: "Od", sortable: true, render: (c) => formatDatePl(c.first_seen) },
  { key: "last_seen", header: "Do", sortable: true, render: (c) => formatDatePl(c.last_seen) },
  {
    key: "median_link_bits",
    header: "Dowody (bit)",
    align: "right",
    sortable: true,
    mono: true,
    title: BITS_TITLE,
    accessor: (c) => campaignSupport(c)?.median_link_bits ?? null,
    render: (c) => <span title={BITS_TITLE}>{formatBits(campaignSupport(c)?.median_link_bits)}</span>,
  },
  {
    key: "weakest_link_bits",
    header: "Najsłabsze ogniwo",
    align: "right",
    sortable: true,
    mono: true,
    title: WEAKEST_TITLE,
    accessor: (c) => campaignSupport(c)?.weakest_link_bits ?? null,
    render: (c) => {
      const s = campaignSupport(c);
      return (
        <span className="inline-flex items-center justify-end gap-1.5">
          {s && <BridgeMarker count={s.bridges.length} />}
          <span title={WEAKEST_TITLE}>{formatBits(s?.weakest_link_bits)}</span>
        </span>
      );
    },
  },
];

export function CampaignsPage() {
  const run = useLatestRun();
  const campaigns = useCampaigns();
  const groups = useCampaignGroups();
  const navigate = useNavigate();
  const [selected, setSelected] = useState<string | null>(null);
  const counts = run.data?.counts;
  const runInfo = run.data?.run;

  return (
    <Page
      title="Kampanie"
      sub={runInfo ? <>przebieg <span className="font-mono">{runInfo.id}</span> · {formatDatePl(runInfo.finished_at ?? runInfo.created_at)}</> : undefined}
    >
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatTile label="Incydenty" value={counts ? formatCount(counts.incidents) : null} loading={run.isPending} />
        <StatTile label="Kampanie" value={counts?.campaigns !== undefined ? formatCount(counts.campaigns) : null} loading={run.isPending} />
        <StatTile label="W kampaniach" value={counts?.members !== undefined ? formatCount(counts.members) : null} hint="incydenty przypisane" loading={run.isPending} />
        <StatTile label="Pary ocenione" value={counts?.pairs !== undefined ? formatCount(counts.pairs) : null} hint="pary kandydatów z bitami" loading={run.isPending} />
        <StatTile label="Przypisania" value={counts?.assignments !== undefined ? formatCount(counts.assignments) : null} hint="nowy materiał" loading={run.isPending} />
      </div>
      <div className={selected ? "grid items-start gap-3 xl:grid-cols-[minmax(0,1fr)_360px]" : ""}>
      <Panel title="Lista kampanii" actions={<span className="text-xs text-subtle">kliknij wiersz: inspektor · dwuklik / przycisk: karta kampanii</span>} flush>
        {campaigns.data ? (
          campaigns.data.run_id === null ? (
            <NoRun what="Kampanie" />
          ) : (
            <DataTable
              columns={columns}
              rows={campaigns.data.items}
              rowKey={(c) => c.id}
              onRowClick={(c) => (selected === c.id ? navigate(`/campaigns/${encodeURIComponent(c.id)}`) : setSelected(c.id))}
              selectedKeys={selected ? new Set([selected]) : undefined}
              empty={<div className="px-3 py-6 text-sm text-subtle">Przebieg nie utworzył żadnej kampanii.</div>}
            />
          )
        ) : (
          <QueryState query={campaigns} what="kampanie" endpoint="/v1/campaigns" />
        )}
      </Panel>
      {selected && <CampaignInspector id={selected} onClose={() => setSelected(null)} />}
      </div>
      <SectionNote>Dowody (bit): mediana bitów w powiązaniach wewnątrz kampanii; najsłabsze ogniwo: najsłabsza krawędź, która utrzymuje kampanię w całości. {SUPPORT_DISCLAIMER}</SectionNote>
      {groups.data && groups.data.items.length > 0 && (
        <Panel title={`Grupy podobnych kampanii (${groups.data.items.length})`} actions={<span className="text-xs text-subtle">wspólny kit, trackery lub infrastruktura przy różnych historiach</span>}>
          <div className="space-y-2">
            {groups.data.items.map((g) => (
              <div key={g.id} className="rounded-md border border-line p-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Prov value="HYP" />
                  <span className="font-mono text-xs">{g.id}</span>
                  <span className="font-semibold">{g.label}</span>
                  <span className="ml-auto font-mono text-xs text-muted" title="suma bitów wspólnych genów grupy (lub wartość z przebiegu); heurystyka, nie prawdopodobieństwo">siła dowodów {formatBits(groupEvidenceBits(g))}</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 text-xs">{g.campaign_ids.map((cid) => <CampaignLink key={cid} id={cid} />)}</div>
                {g.shared_genes?.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted">
                    {g.shared_genes.slice(0, 6).map((sg, i) => (
                      <span key={i} className="inline-flex items-center gap-1"><FamChip family={sg.family} /> <KindLabel kind={sg.kind} />: <span className="max-w-[220px] truncate font-mono" title={sg.value_a ?? ""}>{sg.value_a ?? "—"}</span></span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Panel>
      )}
    </Page>
  );
}
