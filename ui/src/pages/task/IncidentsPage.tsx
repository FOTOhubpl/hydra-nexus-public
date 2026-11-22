import { useState } from "react";
import { useNavigate } from "react-router";
import { useIncidents, type IncidentSummary } from "../../api/task";
import { DataTable, type Column } from "../../components/DataTable";
import { DecisionBadge, Thumb } from "../../components/evidence";
import { Panel } from "../../components/Panel";
import { formatDatePl, ROLE_LABEL } from "../../lib/evidence";
import { CampaignLink, Page, QueryState } from "./common";

const columns: Column<IncidentSummary>[] = [
  { key: "thumb", header: "", width: "56px", render: (r) => <Thumb artifactId={r.thumbnail} alt={r.id} size={40} /> },
  { key: "id", header: "Incydent", mono: true, sortable: true },
  { key: "title", header: "Tytuł", sortable: true, width: "34%", render: (r) => r.title ?? <span className="text-subtle">—</span> },
  { key: "observed_at", header: "Data", sortable: true, width: "130px", render: (r) => formatDatePl(r.observed_at) },
  { key: "artifact_count", header: "Artefakty", align: "right", mono: true, sortable: true, title: "pliki incydentu wg roli",
    render: (r) => <span title={Object.entries(r.artifact_roles).map(([k, v]) => `${ROLE_LABEL[k] ?? k}: ${v}`).join("\n")}>{r.artifact_count}</span> },
  { key: "genes", header: "Geny (zaobs.)", render: (r) => <span className="font-mono text-xs text-muted">{Object.entries(r.observed_genes).map(([f, n]) => `${f} ${n}`).join(" · ") || "—"}</span> },
  { key: "campaign_id", header: "Kampania", sortable: true, render: (r) => <CampaignLink id={r.campaign_id} /> },
  { key: "assignment", header: "Przypisanie", width: "176px", render: (r) => (r.assignment ? <DecisionBadge value={r.assignment} /> : null) },
];

export function IncidentsPage() {
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 200;
  const query = useIncidents({ q: q.trim() || undefined, limit, offset });
  const navigate = useNavigate();
  return (
    <Page title="Incydenty" sub="jednostka analizy: jedno zgłoszenie (kreacje, nagrania, strona docelowa, HAR, metadane)">
      <input value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} placeholder="Filtruj po id, tytule, źródle…" aria-label="Filtr incydentów" className="h-8 max-w-md rounded-[4px] border border-line bg-panel px-3 text-sm focus:border-accent focus:outline-none" />
      <Panel flush title={query.data ? `${query.data.total} incydentów` : "Incydenty"} actions={query.data && query.data.total > limit && (
        <span className="flex items-center gap-2 text-xs">
          <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))} className="disabled:cursor-not-allowed disabled:text-subtle">←</button>
          <span>{offset + 1}–{Math.min(offset + limit, query.data.total)}</span>
          <button type="button" disabled={offset + limit >= query.data.total} onClick={() => setOffset(offset + limit)} className="disabled:cursor-not-allowed disabled:text-subtle">→</button>
        </span>
      )}>
        {query.data ? (
          <DataTable columns={columns} rows={query.data.items} rowKey={(r) => r.id} onRowClick={(r) => navigate(`/incident/${encodeURIComponent(r.id)}`)}
            empty={<div className="px-3 py-6 text-sm text-subtle">Brak incydentów w bazie. Zaimportuj zbiór lub przeanalizuj nowy materiał.</div>} />
        ) : <QueryState query={query} what="incydenty" endpoint="/v1/incidents" />}
      </Panel>
    </Page>
  );
}
