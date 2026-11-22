import { useState } from "react";
import { useNavigate } from "react-router";
import { usePairs, type PairRow } from "../../api/task";
import { DataTable, type Column } from "../../components/DataTable";
import { FamChip, FamilyBars, HypothesisBadge, Probability, ReviewBadge } from "../../components/evidence";
import { Panel } from "../../components/Panel";
import { cx } from "../../lib/cx";
import { formatBits } from "../../lib/evidence";
import { NoRun, Page, pairPath, QueryState } from "./common";

const columns: Column<PairRow>[] = [
  { key: "pair", header: "Para", mono: true, width: "190px", render: (r) => `${r.a} ↔ ${r.b}` },
  { key: "dominant_family", header: "Dominująca rodzina", render: (r) => (r.dominant_family ? <FamChip family={r.dominant_family} withName /> : <span className="text-subtle">—</span>) },
  { key: "bars", header: "Bity per rodzina", render: (r) => <FamilyBars bits={r.family_bits} /> },
  { key: "hypothesis", header: "Hipoteza", width: "220px", render: (r) => <HypothesisBadge value={r.hypothesis} /> },
  { key: "total_bits", header: "Suma bitów", align: "right", mono: true, sortable: true, render: (r) => formatBits(r.total_bits) },
  { key: "probability", header: "wynik nieskalibrowany", align: "right", sortable: true, render: (r) => <Probability p={r.probability} calibrated={r.calibrated} /> },
  { key: "review", header: "Analityk", render: (r) => <ReviewBadge value={r.review} /> },
];

const MODES = [
  { key: "all", label: "Wszystkie (wg p)", params: { order: "probability" } },
  { key: "queue", label: "Kolejka analityka (0,2–0,8, najmniej pewne)", params: { order: "uncertainty", min_p: 0.2, max_p: 0.8 } },
  { key: "links", label: "Powiązania (p ≥ 0,8)", params: { order: "probability", min_p: 0.8 } },
] as const;

export function PairsPage() {
  const [mode, setMode] = useState<(typeof MODES)[number]["key"]>("all");
  const [offset, setOffset] = useState(0);
  const limit = 100;
  const params = { ...MODES.find((m) => m.key === mode)!.params, limit, offset };
  const q = usePairs(params);
  const navigate = useNavigate();
  return (
    <Page title="Pary i dowody" sub="każda ocena pary z rachunkiem bitów; kliknij wiersz, aby zobaczyć dowód">
      <div className="flex flex-wrap gap-1">
        {MODES.map((m) => (
          <button key={m.key} type="button" onClick={() => { setMode(m.key); setOffset(0); }} className={cx("h-7 rounded-[4px] border px-3 text-sm", mode === m.key ? "border-fg bg-fg text-canvas" : "border-line-strong bg-panel hover:bg-hover")}>
            {m.label}
          </button>
        ))}
      </div>
      <Panel flush title={q.data ? `${q.data.total} par` : "Pary"} actions={q.data && q.data.total > limit && (
        <span className="flex items-center gap-2 text-xs">
          <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))} className="disabled:cursor-not-allowed disabled:text-subtle">← poprzednie</button>
          <span>{offset + 1}–{Math.min(offset + limit, q.data.total)}</span>
          <button type="button" disabled={offset + limit >= q.data.total} onClick={() => setOffset(offset + limit)} className="disabled:cursor-not-allowed disabled:text-subtle">następne →</button>
        </span>
      )}>
        {q.data ? (
          q.data.run_id === null ? <NoRun what="Pary" /> : (
            <DataTable columns={columns} rows={q.data.items} rowKey={(r) => `${r.a}|${r.b}`} onRowClick={(r) => navigate(pairPath(r.a, r.b))}
              empty={<div className="px-3 py-6 text-sm text-subtle">Brak par w tym zakresie.</div>} />
          )
        ) : <QueryState query={q} what="pary" endpoint="/v1/pairs" />}
      </Panel>
      <p className="text-xs text-subtle">* wynik nieskalibrowany: ranking z bitów, nie prawdopodobieństwo.</p>
    </Page>
  );
}
