/**
 * "Centrum integracji": KPI row, one-click presets, filters (category chips, search, level) and the connector
 * cards grouped by category. Every value comes from GET /v1/integrations; levels and statuses are shown as the API
 * reports them (live / export-format / planned), planned connectors have no connect button.
 */
import { useMemo, useState } from "react";
import { useIntegrations, type Connector, type ConnectorLevel } from "../../api/integrations";
import { ApiErrorState } from "../../components/ApiErrorState";
import { EmptyState } from "../../components/EmptyState";
import { InfoTip } from "../../components/Tooltip";
import { Skeleton } from "../../components/Skeleton";
import { cx } from "../../lib/cx";
import { ConnectorCard } from "./hub/ConnectorCard";
import { ConnectorDrawer } from "./hub/ConnectorDrawer";
import { CATEGORY_FALLBACK, LEVEL_LABEL } from "./hub/labels";
import { Presets } from "./hub/Presets";

function Kpi({ label, value, tip, testId }: { label: string; value: number; tip: string; testId: string }) {
  return (
    <div className="surface-card flex min-w-0 flex-col gap-1 px-4 py-3" data-testid={testId}>
      <span className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-subtle"><span className="truncate">{label}</span><InfoTip content={tip} label={`Co oznacza: ${label}`} /></span>
      <span className="font-mono text-2xl leading-tight font-semibold text-fg tabular-nums">{value}</span>
    </div>
  );
}

const LEVELS: ConnectorLevel[] = ["live", "export-format", "planned"];

export function HubTab() {
  const q = useIntegrations();
  const [category, setCategory] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [level, setLevel] = useState<ConnectorLevel | "">("");
  const [openId, setOpenId] = useState<string | null>(null);
  const d = q.data;

  const catLabel = useMemo(() => {
    const m = new Map<string, string>(Object.entries(CATEGORY_FALLBACK));
    d?.categories.forEach((c) => m.set(c.id, c.label_pl));
    return m;
  }, [d]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (d?.connectors ?? []).filter((c) =>
      (!category || c.category === category) && (!level || c.level === level) &&
      (!needle || `${c.name} ${c.id} ${c.description_pl} ${catLabel.get(c.category) ?? ""}`.toLowerCase().includes(needle)));
  }, [d, category, level, search, catLabel]);

  const groups = useMemo(() => {
    const order = d?.categories.map((c) => c.id) ?? [];
    const by = new Map<string, Connector[]>();
    for (const c of filtered) by.set(c.category, [...(by.get(c.category) ?? []), c]);
    return [...by.entries()].sort((a, b) => (order.indexOf(a[0]) + 1 || 99) - (order.indexOf(b[0]) + 1 || 99));
  }, [filtered, d]);

  if (!d) return q.isError ? <ApiErrorState error={q.error} resource="katalog integracji" endpoint="/v1/integrations" /> : <Skeleton label="Wczytywanie integracji…" lines={5} />;

  const count = (lv: ConnectorLevel) => d.connectors.filter((c) => c.level === lv).length;

  return (
    <div className="flex min-w-0 flex-col gap-5" data-testid="hub-tab">
      <div className="grid min-w-0 grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
        <Kpi testId="kpi-live" label="Na żywo (live)" value={count("live")} tip={`Konektory z działającym klientem sieciowym. ${d.offline ? "Teraz zablokowane: tryb offline." : ""}`} />
        <Kpi testId="kpi-export" label="Format eksportu" value={count("export-format")} tip="Konektory, dla których HYDRA tworzy plik w formacie importu dostawcy. Działają offline." />
        <Kpi testId="kpi-planned" label="Planowane" value={count("planned")} tip="Tylko opisy; brak implementacji, brak przycisków połączenia." />
        <Kpi testId="kpi-configured" label="Skonfigurowane" value={d.connectors.filter((c) => c.status === "configured" || c.status === "offline").length} tip="Konektory z zapisanymi wymaganymi kluczami (albo bez kluczy)." />
        <Kpi testId="kpi-enabled" label="Włączone" value={d.connectors.filter((c) => c.enabled).length} tip="Konektory włączone przełącznikiem. Domyślnie wszystkie są wyłączone." />
      </div>

      <Presets presets={d.presets} connectors={d.connectors} offline={d.offline} />

      <section aria-label="Filtry" className="flex min-w-0 flex-col gap-2">
        <div className="flex min-w-0 flex-wrap gap-1.5" role="group" aria-label="Kategoria">
          {[{ id: null as string | null, label: "Wszystkie" }, ...d.categories.map((c) => ({ id: c.id as string | null, label: c.label_pl }))].map((c) => (
            <button
              key={c.id ?? "all"}
              type="button"
              aria-pressed={category === c.id}
              onClick={() => setCategory(c.id)}
              className={cx("h-7 rounded-full border px-3 text-xs font-medium transition-colors", category === c.id ? "border-accent bg-accent-soft text-fg" : "border-line text-muted hover:bg-hover hover:text-fg")}
            >
              {c.label}
            </button>
          ))}
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Szukaj integracji…"
            aria-label="Szukaj integracji"
            data-testid="hub-search"
            className="h-8 min-w-0 flex-1 basis-48 rounded-[4px] border border-line bg-raised px-2 text-sm"
          />
          <select value={level} onChange={(e) => setLevel(e.target.value as ConnectorLevel | "")} aria-label="Poziom implementacji" data-testid="hub-level" className="h-8 rounded-[4px] border border-line bg-panel px-2 text-sm">
            <option value="">Wszystkie poziomy</option>
            {LEVELS.map((lv) => <option key={lv} value={lv}>{LEVEL_LABEL[lv]}</option>)}
          </select>
          <span className="text-xs text-subtle" aria-live="polite">{filtered.length} z {d.connectors.length}</span>
        </div>
      </section>

      {groups.length === 0 ? (
        <EmptyState title="Brak integracji dla tych filtrów" description="Zmień kategorię, poziom albo wyszukiwaną frazę." compact />
      ) : groups.map(([cat, items]) => (
        <section key={cat} aria-labelledby={`cat-${cat}`} className="min-w-0 space-y-2">
          <h3 id={`cat-${cat}`} className="flex items-center gap-1.5 text-sm font-semibold text-fg">
            {catLabel.get(cat) ?? cat}
            <span className="text-xs font-normal text-subtle">{items.length}</span>
            {d.categories.find((c) => c.id === cat)?.description_pl && <InfoTip content={d.categories.find((c) => c.id === cat)!.description_pl} />}
          </h3>
          <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {items.map((c) => (
              <ConnectorCard key={c.id} c={c} categoryLabel={catLabel.get(c.category) ?? c.category} onOpen={() => setOpenId(c.id)} offline={d.offline} />
            ))}
          </div>
        </section>
      ))}

      <ConnectorDrawer id={openId} onClose={() => setOpenId(null)} offline={d.offline} />
    </div>
  );
}
