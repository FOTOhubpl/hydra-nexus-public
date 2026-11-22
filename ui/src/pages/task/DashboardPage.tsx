import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { useCorpora, datasetExportUrl, FAMILIES, useAnalytics, useAssignments, useDashboard, type DashboardData } from "../../api/task";
import { Sankey, Sparkline, Timeline } from "../../components/charts";
import { FamChip, Prov, Thumb } from "../../components/evidence";
import { Card, CardSkeleton, Empty, Kpi, MiniBars, StackBar } from "../../components/dashboard/bento";
import { Icon } from "../../components/Icon";
import { Tooltip } from "../../components/Tooltip";
import { RAIL_TOGGLE_EVENT } from "../../components/CommandPalette";
import { useActivity } from "../../lib/activity";
import { useMediaQuery } from "../../lib/media";
import { readJson, writeJson } from "../../lib/storage";
import { cx } from "../../lib/cx";
import { FAMILY_STACK_ORDER, FAMILY_VAR, HYPOTHESIS_LABEL, familyColors, familyLabel, formatDatePl, plNumber } from "../../lib/evidence";
import { extSource } from "../../lib/ext";
import { formatCount } from "../../lib/format";
import { assignGroupColors, GROUP_PALETTE, GROUP_PALETTE_DARK, UNGROUPED_COLOR } from "../../lib/graph";
import { inspector } from "../../lib/inspector";
import { useTheme, type Theme } from "../../lib/theme";
import { MaterialsStrip } from "../../components/artifacts/MaterialsStrip";
import { SkeletonBlock, SlowHint } from "../../components/Skeleton";
import { DemoLauncher } from "./DemoLauncher";
import { NewBadge, NewCount, NoRun, QueryState, StartImport } from "./common";
import { polishReason } from "../../lib/displayPl";

/** Multi-member campaigns get palette slots (alphabetical, as in the graph); singletons stay neutral. */
export function campaignColors(campaigns: { id: string; size: number }[], theme: Theme = "light"): Record<string, string> {
  const multi = assignGroupColors(campaigns.filter((c) => c.size > 1).map((c) => c.id), {}, theme === "dark" ? GROUP_PALETTE_DARK : GROUP_PALETTE);
  return Object.fromEntries(campaigns.map((c) => [c.id, multi.get(c.id) ?? UNGROUPED_COLOR]));
}

const COVERAGE_ROWS = 12;

function Coverage({ rows }: { rows: DashboardData["coverage"] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-separate border-spacing-[2px] text-xs">
        <thead>
          <tr><th className="text-left font-normal text-subtle">incydent</th>{FAMILIES.map((f) => <th key={f} className="font-normal"><FamChip family={f} /></th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.incident_id}>
              <td className="pr-2"><button type="button" className="rounded-sm font-mono text-accent hover:underline" onClick={() => inspector.open({ kind: "incident", id: r.incident_id })}>{r.incident_id}</button></td>
              {FAMILIES.map((f) => {
                const c = r.families[f] ?? {};
                const obs = c.observed ?? 0;
                const lvl = obs === 0 ? 0 : Math.min(4, Math.ceil(Math.log10(obs + 1)));
                const bg = ["bg-hover", "bg-for/25", "bg-for/45", "bg-for/70", "bg-for"][lvl];
                const t = `${f}: zaobserwowano ${obs}, niedostępne ${c.unavailable ?? 0}, błąd ${c.failed ?? 0}`;
                return <td key={f} title={t} className={cx("h-6 rounded-[3px] text-center font-mono", bg, lvl >= 3 ? "text-white" : "text-fg")}>{obs ? formatCount(obs) : "—"}{c.failed ? "!" : ""}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Page frame while /v1/dashboard loads: KPI and card placeholders instead of a blank page. */
function DashboardSkeleton() {
  return (
    <div role="status" aria-busy="true" aria-live="polite" data-testid="dashboard-skeleton" className="space-y-4">
      <span className="sr-only">Ładowanie: pulpit…</span>
      <SlowHint className="text-sm" />
      <div className="grid grid-cols-2 gap-3 @@5xl:grid-cols-3 @3xl:grid-cols-4 @5xl:grid-cols-7">
        {Array.from({ length: 7 }, (_, i) => <SkeletonBlock key={i} className="h-[92px] rounded-xl" />)}
      </div>
      <div className="grid gap-4 @3xl:grid-cols-6 @5xl:grid-cols-12">
        <SkeletonBlock className="h-[320px] rounded-xl @3xl:col-span-6 @5xl:col-span-5" />
        <SkeletonBlock className="h-[320px] rounded-xl @3xl:col-span-3 @5xl:col-span-4" />
        <SkeletonBlock className="h-[320px] rounded-xl @3xl:col-span-3 @5xl:col-span-3" />
      </div>
      <div className="grid gap-4 @3xl:grid-cols-6 @5xl:grid-cols-12">
        <SkeletonBlock className="h-[280px] rounded-xl @3xl:col-span-6 @5xl:col-span-7" />
        <SkeletonBlock className="h-[280px] rounded-xl @3xl:col-span-6 @5xl:col-span-5" />
      </div>
    </div>
  );
}

const INFRA_LABEL: Record<string, string> = { registrar: "rejestrator", ns_set: "para NS", asn: "ASN", as_org: "hosting (AS)", cert_pl_listed_host: "lista ostrzeżeń CERT PL" };
const DECISION_SHORT: Record<string, { label: string; cls: string }> = {
  assign: { label: "przypisano", cls: "border-ok/35 bg-ok/10 text-ok" },
  new_campaign: { label: "nowa kampania", cls: "border-info/35 bg-info/10 text-info" },
  abstain: { label: "wstrzymano", cls: "border-warn/35 bg-warn/10 text-warn" },
};
const TIER_SERIES = [
  { key: "campaign_bits", label: "kampania / operator", color: "var(--color-for)" },
  { key: "kit_bits", label: "szablon / kit", color: "var(--color-fam-meta)" },
  { key: "asset_bits", label: "wspólny zasób", color: "var(--color-fam-vis)" },
] as const;
const btn = "inline-flex h-9 items-center gap-1.5 rounded-lg border border-line bg-panel px-3 text-sm text-fg shadow-card hover:border-line-strong hover:bg-hover";

// Lazy chunk: the rail never delays the first paint of the KPIs (837-incident run budget, docs/ui/UI_LOG.md).
const DashboardRail = lazy(() => import("./DashboardRail"));
const LG_UP = "(min-width: 64rem)";
const isTyping = (t: EventTarget | null) => t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));

function RailFallback() {
  return (
    <div aria-hidden="true" className="space-y-2.5">
      {["h-28", "h-40", "h-36", "h-28"].map((h, i) => <SkeletonBlock key={i} className={cx("rounded-xl", h)} />)}
    </div>
  );
}

/**
 * Right rail in the floating-inset style of the sidebar. lg and up: a sticky column that collapses to a slim strip
 * (Ctrl/⌘ J, state remembered); below lg: a bottom sheet opened from a floating button.
 */
function RightRail({ data, onDemo }: { data: DashboardData | undefined; onDemo: () => void }) {
  const wide = useMediaQuery(LG_UP, true);
  const [open, setOpen] = useState<boolean>(() => readJson("dash.rail", true));
  const [sheet, setSheet] = useState(false);
  const { unread } = useActivity();
  const sheetRef = useRef<HTMLDivElement>(null);
  const toggle = useCallback(() => {
    if (window.matchMedia?.(LG_UP).matches ?? true) setOpen((v) => { writeJson("dash.rail", !v); return !v; });
    else setSheet((v) => !v);
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "j" && !isTyping(e.target)) { e.preventDefault(); toggle(); }
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener(RAIL_TOGGLE_EVENT, toggle);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener(RAIL_TOGGLE_EVENT, toggle); };
  }, [toggle]);
  useEffect(() => {
    if (!sheet) return;
    const prev = document.activeElement as HTMLElement | null;
    sheetRef.current?.querySelector<HTMLElement>("button")?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setSheet(false); };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); prev?.focus?.(); };
  }, [sheet]);
  useEffect(() => { if (wide) setSheet(false); }, [wide]);

  const toggleBtn = (label: string, glyph: string) => (
    <Tooltip content={`${label} (Ctrl/⌘ J)`} side="left">
      <button type="button" onClick={toggle} aria-label={label} aria-expanded={open} aria-keyshortcuts="Control+J Meta+J" data-testid="rail-toggle"
        className="flex size-8 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg">
        <Icon name={glyph} size={16} />
      </button>
    </Tooltip>
  );

  if (wide) {
    return (
      <aside aria-label="Panel pulpitu" data-testid="dash-rail" data-state={open ? "expanded" : "collapsed"}
        className={cx("sticky top-0 flex max-h-[calc(100dvh-7.5rem)] shrink-0 flex-col self-start overflow-hidden rounded-2xl border border-line bg-panel shadow-float", open ? "w-80 motion-safe:animate-[rail-in_200ms_var(--ease-out-soft)]" : "w-12")}>
        {open ? (
          <>
            <div className="flex h-11 shrink-0 items-center gap-2 border-b border-line pr-2 pl-4">
              <span className="text-sm font-semibold text-fg">Panel pulpitu</span>
              <span className="text-2xs text-subtle">na żywo</span>
              <span className="ml-auto" />
              {toggleBtn("Ukryj panel pulpitu", "rail")}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-2.5">
              <Suspense fallback={<RailFallback />}><DashboardRail data={data} onDemo={onDemo} /></Suspense>
            </div>
          </>
        ) : (
          <div className="flex flex-col items-center gap-1 py-2">
            {toggleBtn("Pokaż panel pulpitu", "rail")}
            {unread > 0 && <span className="mt-1 rounded-full bg-err px-1.5 font-mono text-[9px] font-semibold text-white" aria-label={`nowe zdarzenia: ${unread}`}>{unread}</span>}
          </div>
        )}
      </aside>
    );
  }
  return (
    <>
      <button type="button" onClick={() => setSheet(true)} data-testid="rail-sheet-button" aria-label={unread ? `Panel pulpitu (nowe zdarzenia: ${unread})` : "Panel pulpitu"} aria-haspopup="dialog"
        className="fixed right-3 bottom-11 z-30 flex h-11 items-center gap-2 rounded-full border border-line bg-panel px-4 text-sm font-medium text-fg shadow-float hover:bg-hover">
        <Icon name="activity" size={16} className="text-accent" />Aktywność
        {unread > 0 && <span className="rounded-full bg-err px-1.5 font-mono text-[10px] font-semibold text-white">{unread}</span>}
      </button>
      {sheet && (
        <div className="fixed inset-0 z-40" role="presentation">
          <div className="absolute inset-0 bg-fg/30 motion-safe:animate-[fade-in_150ms_ease-out]" onClick={() => setSheet(false)} aria-hidden="true" />
          <div ref={sheetRef} role="dialog" aria-modal="true" aria-label="Panel pulpitu" data-testid="rail-sheet"
            className="absolute inset-x-2 bottom-2 flex max-h-[82dvh] flex-col overflow-hidden rounded-2xl border border-line bg-panel shadow-float motion-safe:animate-[sheet-in_220ms_var(--ease-out-soft)]">
            <div className="flex shrink-0 justify-center pt-2" aria-hidden="true"><span className="h-1 w-10 rounded-full bg-line-strong" /></div>
            <div className="flex h-11 shrink-0 items-center gap-2 border-b border-line pr-2 pl-4">
              <span className="text-sm font-semibold text-fg">Panel pulpitu</span>
              <button type="button" onClick={() => setSheet(false)} aria-label="Zamknij panel" className="ml-auto flex size-9 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg"><Icon name="close" /></button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-2.5">
              <Suspense fallback={<RailFallback />}><DashboardRail data={data} onDemo={() => { setSheet(false); onDemo(); }} /></Suspense>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export function DashboardPage() {
  const q = useDashboard();
  const an = useAnalytics();
  const assignments = useAssignments({ limit: 8 });
  const corpora = useCorpora();
  const theme = useTheme();
  const [demoOpen, setDemoOpen] = useState(false);
  const d = q.data;
  const colors = useMemo(() => campaignColors(d?.campaigns ?? [], theme), [d, theme]);
  const colorOf = (cid: string | null | undefined) => (cid && colors[cid]) || UNGROUPED_COLOR;
  const fam = familyColors(theme);
  const sources = useMemo(() => Object.fromEntries((d?.previews ?? []).map((p) => [p.id, p.source ?? null])), [d]);
  const labels = useMemo(() => Object.fromEntries((d?.campaigns ?? []).map((c) => [c.id, c.label])), [d]);

  const header = (
    <div className="flex flex-wrap items-center gap-3">
      <div className="min-w-0">
        <div className="text-sm font-medium text-muted">Bieżący przebieg</div>
        <div className="mt-0.5 text-sm text-muted">
          {!d ? "wczytywanie przebiegu…" : d.run ? (
            <>przebieg <span className="font-mono text-fg">{d.run.id}</span> · {formatDatePl(d.run.finished_at ?? d.run.created_at)} · scorer <span className="font-mono">{d.run.scorer ?? "—"}</span></>
          ) : "brak przebiegu analizy"}
        </div>
      </div>
      <div className="ml-auto flex flex-wrap gap-2">
        <Link to="/analyse" className={btn}>Nowa analiza</Link>
        <Link to="/data" className={btn}>Import</Link>
        {d?.run ? (
          <Tooltip content="Wzbogacony zbiór danych przebiegu (ZIP podpisany Ed25519, GET /v1/export/dataset; weryfikacja: hydra verify-export)"><a href={datasetExportUrl()} download className={btn}>Eksport</a></Tooltip>
        ) : (
          <Tooltip focusable className="inline-flex rounded-lg" content={d ? "Eksport niedostępny: brak przebiegu analizy. Uruchom analizę, aby utworzyć zbiór do eksportu." : "Eksport będzie dostępny po wczytaniu przebiegu."}>
            <button type="button" disabled className={cx(btn, "opacity-50")}>Eksport</button>
          </Tooltip>
        )}
        <Tooltip content="Analiza zbioru na żywo: import, ekstrakcja genów, porównanie par i grupowanie w kampanie">
          <button type="button" onClick={() => setDemoOpen(true)} className="inline-flex h-9 items-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-canvas shadow-card hover:opacity-90">▶ Uruchom analizę</button>
        </Tooltip>
      </div>
    </div>
  );

  const openDemo = useCallback(() => setDemoOpen(true), []);
  const frame = (body: React.ReactNode) => (
    <div className="flex gap-3 p-3 pb-16 sm:p-4 sm:pb-16 lg:pb-4">
      <div className="@container mx-auto min-w-0 max-w-[1840px] flex-1 space-y-4">
        {demoOpen && <DemoLauncher onClose={() => setDemoOpen(false)} />}
        {header}
        {body}
      </div>
      <RightRail data={d} onDemo={openDemo} />
    </div>
  );

  if (!d) {
    if (q.isError) return <div className="p-4 sm:p-6"><QueryState query={q} what="pulpit" endpoint="/v1/dashboard" /></div>;
    return frame(<DashboardSkeleton />);
  }

  const k = d.kpis;
  const famObs = (f: string) => k.genes_by_family[f]?.observed ?? 0;
  const timeline = an.data?.timeline ?? [];
  const perDay = (() => {
    const m = new Map<string, number>();
    for (const t of timeline) if (t.observed_at) m.set(t.observed_at.slice(0, 10), (m.get(t.observed_at.slice(0, 10)) ?? 0) + 1);
    return [...m.entries()].sort().map(([, v]) => v);
  })();
  const roles = Object.entries(k.artifacts_by_role).sort((a, b) => b[1] - a[1]);
  const live = d.live;
  const byDecision = live?.by_decision ?? {};

  const campaignRows = [...d.campaigns].sort((a, b) => b.size - a.size || a.id.localeCompare(b.id)).slice(0, 8);
  const familyBits = (() => {
    const m = new Map<string, number>();
    for (const l of an.data?.sankey?.links ?? []) m.set(l.family, (m.get(l.family) ?? 0) + l.bits);
    return FAMILY_STACK_ORDER.filter((f) => m.has(f)).map((f) => ({ f, bits: m.get(f)! })).sort((a, b) => b.bits - a.bits);
  })();
  const tierLinks = an.data?.tiers?.links ?? [];
  const tierTotals = TIER_SERIES.map((s) => ({ ...s, bits: tierLinks.reduce((sum, t) => sum + Math.max(0, Number(t[s.key] ?? 0)), 0) }));
  const indicators = Object.entries(an.data?.infrastructure ?? {})
    .flatMap(([kind, vals]) => vals.map((v) => ({ kind, ...v })))
    .sort((a, b) => b.df - a.df || a.value.localeCompare(b.value))
    .slice(0, 8);
  const hypotheses = Object.entries(d.run?.hypotheses ?? {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  const assignItems = assignments.data?.items ?? [];
  const corpusItems = corpora.data?.items ?? [];

  return frame(
    <>
      <div className="grid grid-cols-2 gap-3 @@5xl:grid-cols-3 @3xl:grid-cols-4 @5xl:grid-cols-7">
        <Kpi testId="kpi-incidents" label="Incydenty" count={k.incidents} to="/incidents"
          tip="Zgłoszenia (incydenty) zapisane w bazie dla tego przebiegu. Wykres: liczba incydentów na dzień obserwacji z materiału."
          hint={perDay.length > 1 ? "na dzień obserwacji" : "zgłoszenia w bazie"}
          aside={<Sparkline values={perDay} label="incydenty na dzień obserwacji" width={72} />} />
        <Kpi testId="kpi-campaigns" label="Kampanie" count={k.campaigns} hint={`${formatCount(k.multi_member_campaigns)} z ≥ 2 incydentami`} to="/campaigns"
          tip="Grupy incydentów, które system łączy wspólnymi rzadkimi genami w jedną kampanię. Pojedynczy incydent bez powiązań też jest osobną kampanią." />
        <Kpi label="Artefakty" count={k.artifacts} hint={roles.slice(0, 2).map(([r, n]) => `${r} ${formatCount(n)}`).join(" · ") || undefined} to="/data"
          tip="Pliki materiału (zrzuty, strony, wideo, teksty) zapisane w magazynie treści według skrótu SHA-256." />
        <Kpi label="Geny" count={k.genes}
          tip="Gen = pojedyncza obserwacja z materiału (np. identyfikator śledzący, fraza, logo, serwer nazw). Pasek: udział rodzin genów (VIS, TXT, INF, ID, NAR, META)."
          hint={FAMILIES.filter((f) => famObs(f)).sort((a, b) => famObs(b) - famObs(a)).slice(0, 3).map((f) => `${f} ${formatCount(famObs(f))}`).join(" · ") || undefined}
          foot={<StackBar label={`Geny zaobserwowane według rodzin: ${FAMILY_STACK_ORDER.map((f) => `${f} ${famObs(f)}`).join(", ")}`}
            parts={FAMILY_STACK_ORDER.map((f) => ({ key: f, value: famObs(f), color: fam[f] ?? UNGROUPED_COLOR, title: `${f} · ${familyLabel(f)}: ${formatCount(famObs(f))}` }))} />} />
        <Kpi label="Przypisania" count={k.assignments} hint="nowy materiał do kampanii" to="/analyse"
          tip="Decyzje dla nowego materiału: przypisano do kampanii, nowa kampania albo wstrzymano (za mało dowodów, by zdecydować)." />
        <Kpi label="Wzbogacenie" count={k.enriched_incidents} format={(n) => `${formatCount(n)}/${formatCount(k.incidents)}`} hint="incydenty z faktami RDAP/DNS/CT"
          tip="Incydenty, dla których zapisano fakty o domenie: rejestracja (RDAP), DNS i certyfikaty (CT). W trybie offline odtwarzane z pamięci podręcznej." />
        <Kpi label="Nowe od przebiegu" accent={Boolean(live?.new_incidents)} count={live ? live.new_incidents ?? 0 : undefined} value={live ? undefined : "—"}
          tip="Materiał dodany po zakończeniu analizy i dołączony do niej na żywo (strumień GET /v1/stream), bez ponownej analizy całego zbioru."
          hint={live ? (Object.keys(byDecision).length ? Object.entries(byDecision).map(([dk, n]) => `${DECISION_SHORT[dk]?.label ?? dk} ${formatCount(n)}`).join(" · ") : "strumień na żywo, bez nowych") : "brak danych strumienia"} />
      </div>

      {!d.run ? (
        <>
          {k.incidents === 0 && <StartImport />}
          <Card title="Źródła danych" sub="zaimportowane artefakty według typu i zbioru" to="/data"><MaterialsStrip /></Card>
          <NoRun what="Wyniki" />
        </>
      ) : (
        <>
          <div className="grid gap-4 @3xl:grid-cols-6 @5xl:grid-cols-12">
            <Card title="Kampanie" flashKey={campaignRows.map((c) => c.size).join(",")} sub="największe według liczby incydentów" to="/campaigns" className="@3xl:col-span-6 @5xl:col-span-5">
              {campaignRows.length ? (
                <MiniBars max={campaignRows[0]!.size} rows={campaignRows.map((c) => ({
                  id: c.id,
                  value: c.size,
                  valueLabel: `${formatCount(c.size)} inc.`,
                  color: colorOf(c.id),
                  title: `${c.id} · ${c.label} · ${c.size} incydentów`,
                  onPick: () => inspector.open({ kind: "campaign", id: c.id }),
                  label: <span className="flex min-w-0 items-center gap-2"><span aria-hidden="true" className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: colorOf(c.id) }} /><span className="shrink-0 font-mono text-accent">{c.id}</span><span className="truncate text-fg">{c.label}</span></span>,
                  extra: <>{<NewCount n={c.new} />}{c.members.some((m) => extSource(sources[m.id])) && <span className="rounded border border-line px-1 text-2xs">ext</span>}</>,
                }))} />
              ) : <Empty>Brak kampanii w tym przebiegu.</Empty>}
            </Card>

            <Card title="Dowody" tip="Siła dowodu w bitach: każdy wspólny gen dodaje tyle bitów, ile jest rzadki (rzadszy = więcej). Bity to miara heurystyczna, nie prawdopodobieństwo." flashKey={an.dataUpdatedAt} sub="bity powiązań wewnątrz kampanii według rodziny genów" className="@3xl:col-span-3 @5xl:col-span-4">
              {an.isPending ? <CardSkeleton /> : familyBits.length ? (
                <MiniBars rows={familyBits.map(({ f, bits }) => ({ id: f, value: bits, valueLabel: plNumber(bits, 1), color: FAMILY_VAR[f] ?? UNGROUPED_COLOR, title: `${f} · ${familyLabel(f)}: ${plNumber(bits, 1)} bitów`, label: <FamChip family={f} withName /> }))} unit="b" />
              ) : <Empty>Brak powiązań wewnątrz kampanii w tym przebiegu.</Empty>}
              {tierLinks.length > 0 && (
                <div className="mt-4 border-t border-line pt-3">
                  <div className="mb-2 flex items-baseline gap-2 text-xs"><span className="font-medium text-muted">Poziomy dowodu par</span><Link to="/pairs" className="ml-auto rounded-sm text-accent hover:underline">pary</Link></div>
                  <StackBar label={`Suma bitów ${tierLinks.length} najsilniejszych par: ${tierTotals.map((t) => `${t.label} ${plNumber(t.bits, 1)}`).join(", ")}`}
                    className="h-2" parts={tierTotals.map((t) => ({ key: t.key, value: t.bits, color: t.color, title: `${t.label}: ${plNumber(t.bits, 1)} bitów` }))} />
                  <ul className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
                    {tierTotals.map((t) => <li key={t.key} className="inline-flex items-center gap-1.5"><span aria-hidden="true" className="size-2 rounded-[2px]" style={{ backgroundColor: t.color }} />{t.label} <span className="font-mono text-fg">{plNumber(t.bits, 1)} b</span></li>)}
                  </ul>
                  <div className="mt-1 text-2xs text-subtle">suma z {formatCount(tierLinks.length)} najsilniejszych par</div>
                </div>
              )}
            </Card>

            <Card title="Graf powiązań" sub="podsumowanie przebiegu" className="@3xl:col-span-3 @5xl:col-span-3">
              <dl className="grid grid-cols-2 gap-2 text-xs">
                {([["kampanie ≥ 2 inc.", k.multi_member_campaigns], ["grupy kampanii", k.groups], ["pary kandydackie", d.run.candidate_pairs ?? null], ["pary ocenione", hypotheses.length ? hypotheses.reduce((a, [, n]) => a + n, 0) : null]] as const).map(([label, v]) => (
                  <div key={label} className="rounded-lg bg-raised px-2.5 py-2"><dt className="text-subtle">{label}</dt><dd className="font-mono text-lg font-semibold text-fg">{formatCount(v)}</dd></div>
                ))}
              </dl>
              {hypotheses.length > 0 && (
                <div className="mt-3">
                  <MiniBars rows={hypotheses.map(([h, n]) => ({ id: h, value: n, valueLabel: formatCount(n), label: HYPOTHESIS_LABEL[h] ?? h, color: h === "same_campaign" ? "var(--color-for)" : h === "abstain" ? "var(--color-abstain)" : h === "independent" ? "var(--color-unknown)" : "var(--color-fam-meta)" }))} />
                </div>
              )}
              <Link to="/graph" className={cx(btn, "mt-3 w-full justify-center")}>Otwórz graf</Link>
            </Card>
          </div>

          <div className="grid gap-4 @3xl:grid-cols-6 @5xl:grid-cols-12">
            <Card title="Ostatnie przypisania" tip="Decyzja dla nowego materiału: „przypisano” (dowody wskazują kampanię), „nowa kampania” (brak pasującej), „wstrzymano” (dowody niewystarczające, decyzja dla analityka)." flashKey={assignItems[0]?.created_at} sub="decyzja i uzasadnienie dla nowego materiału" to="/analyse" toLabel="nowa analiza" className="@3xl:col-span-6 @5xl:col-span-7">
              {assignments.isPending ? <CardSkeleton lines={5} /> : assignments.isError ? <Empty>Nie udało się pobrać przypisań (/v1/assignments).</Empty> : assignItems.length === 0 ? <Empty>Brak przypisań. Dodaj materiał przez „Nowa analiza”.</Empty> : (
                <ul className="divide-y divide-line">
                  {assignItems.map((a) => {
                    const dec = DECISION_SHORT[a.decision] ?? { label: a.decision, cls: "border-line-strong bg-hover text-muted" };
                    return (
                      <li key={`${a.incident_id}-${a.created_at ?? ""}`} className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 py-2 text-xs">
                        <span className={cx("inline-flex h-5 shrink-0 items-center rounded-md border px-1.5 text-2xs font-medium", dec.cls)}>{dec.label}</span>
                        <button type="button" onClick={() => inspector.open({ kind: "incident", id: a.incident_id })} className="shrink-0 rounded-sm font-mono text-accent hover:underline">{a.incident_id}</button>
                        {a.campaign_id && <><span aria-hidden="true" className="text-subtle">→</span><Link to={`/campaigns/${encodeURIComponent(a.campaign_id)}`} className="min-w-0 truncate rounded-sm text-accent hover:underline"><span className="font-mono">{a.campaign_id}</span>{labels[a.campaign_id] ? <span className="ml-1 text-fg">{labels[a.campaign_id]}</span> : null}</Link></>}
                        {a.created_at && <span className="ml-auto shrink-0 text-subtle">{formatDatePl(a.created_at)}</span>}
                        <span className="w-full truncate text-muted" title={polishReason(a.reason_pl)}>{polishReason(a.reason_pl) || "—"}</span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </Card>

            <Card title="Wspólne wskaźniki" sub="wartości infrastruktury wspólne dla wielu incydentów · geny INF" to="/search" toLabel="szukaj" className="@3xl:col-span-6 @5xl:col-span-5">
              {an.isPending ? <CardSkeleton lines={6} /> : indicators.length ? (
                <ul className="space-y-1 text-xs">
                  {indicators.map((v) => (
                    <li key={`${v.kind}:${v.value}`} className="flex min-w-0 items-center gap-2 rounded-md px-1 py-1 hover:bg-hover">
                      <Prov value="OBS" />
                      <span className="w-24 shrink-0 truncate text-subtle">{INFRA_LABEL[v.kind] ?? v.kind}</span>
                      <Link to={`/search?q=${encodeURIComponent(v.value.slice(0, 200))}`} className="min-w-0 flex-1 truncate rounded-sm font-mono text-fg hover:text-accent hover:underline" title={v.value}>{v.value}</Link>
                      <span className="shrink-0 font-mono text-muted" title={v.incidents.join(", ")}>{formatCount(v.df)} inc.</span>
                    </li>
                  ))}
                </ul>
              ) : <Empty>Brak wspólnych genów infrastruktury w tym przebiegu.</Empty>}
            </Card>
          </div>

          <div className="grid gap-4 @3xl:grid-cols-6 @5xl:grid-cols-12">
            <Card title="Oś czasu" sub="data obserwacji z materiału · kolor = kampania" to="/incidents" className="@3xl:col-span-6 @5xl:col-span-12">
              {an.isPending ? <CardSkeleton lines={3} /> : timeline.length ? (
                <Timeline items={timeline} colorOf={colorOf} onPick={(id) => inspector.open({ kind: "incident", id })} legend={d.campaigns.filter((c) => c.size > 1).map((c) => ({ id: c.id, label: c.label }))} />
              ) : <Empty>Brak dat obserwacji w materiale.</Empty>}
              {d.previews.length > 0 && (
                <div className="mt-4 grid grid-cols-[repeat(auto-fill,minmax(104px,1fr))] gap-3">
                  {d.previews.map((p) => (
                    <button key={p.id} type="button" onClick={() => inspector.open({ kind: "incident", id: p.id })} className="min-w-0 rounded-lg text-left">
                      <span className="relative block overflow-hidden rounded-lg">
                        <Thumb artifactId={p.thumbnail} alt={p.title ?? p.id} size={104} className="!h-20 !w-full" />
                        <span className="absolute top-1 left-1 size-3 rounded-full ring-2 ring-panel" style={{ backgroundColor: colorOf(p.campaign_id) }} />
                      </span>
                      <span className="mt-1 flex items-center gap-1 text-xs"><span className="truncate font-mono text-fg">{p.id}</span>{extSource(sources[p.id]) && <span className="rounded border border-line px-1 text-2xs">{extSource(sources[p.id])}</span>}</span>
                      {p.new && <NewBadge className="mt-0.5" />}
                    </button>
                  ))}
                </div>
              )}
            </Card>

          </div>

          <div className="grid gap-4 @3xl:grid-cols-6 @5xl:grid-cols-12">
            <Card title="Rodziny genów → kampanie" tip="Przepływ bitów dowodu od rodzin genów do kampanii, w których połączyły incydenty. Szerokość pasma = suma bitów." sub="bity powiązań wewnątrz kampanii" className="@3xl:col-span-6 @5xl:col-span-7">
              {an.isPending ? <CardSkeleton lines={6} /> : an.data?.sankey && an.data.sankey.links.length > 0 ? (
                <div className="overflow-x-auto">
                  <Sankey links={an.data.sankey.links.map((l) => ({ left: l.family, right: l.campaign_id, value: l.bits }))} leftOrder={FAMILY_STACK_ORDER} leftColor={(f) => fam[f] ?? UNGROUPED_COLOR} leftLabel={(f) => `${f} ${familyLabel(f)}`} rightLabel={(c) => c} unit="bitów" onPickRight={(c) => inspector.open({ kind: "campaign", id: c })} />
                </div>
              ) : <Empty>Brak powiązań wewnątrz kampanii w tym przebiegu.</Empty>}
            </Card>
            <Card title="Pokrycie rodzin genów" tip="Ile genów każdej rodziny zaobserwowano w incydencie. — oznacza brak obserwacji (nieznane), co nie obniża wyniku; ! oznacza błąd ekstrakcji." sub={d.coverage.length > COVERAGE_ROWS ? `pierwsze ${COVERAGE_ROWS} z ${formatCount(d.coverage.length)} incydentów · — = nieznane` : "geny zaobserwowane · — = nieznane"} className="@3xl:col-span-6 @5xl:col-span-5">
              {d.coverage.length ? <Coverage rows={d.coverage.slice(0, COVERAGE_ROWS)} /> : <Empty>Brak danych o pokryciu.</Empty>}
            </Card>
          </div>

          <Card title="Źródła danych" sub="artefakty według typu i zbioru · najnowsze materiały" to="/data" toLabel="import danych">
            {corpusItems.length > 0 && (
              <ul className="mb-3 flex flex-wrap gap-2 text-xs" aria-label="Zbiory danych i przebiegi">
                {corpusItems.map((c) => (
                  <li key={c.corpus} className="rounded-lg border border-line bg-raised px-2.5 py-1.5">
                    <span className="font-mono text-fg">{c.corpus}</span>
                    <span className="ml-2 text-muted">przebiegi: <b className="font-mono text-fg">{formatCount(c.runs.length)}</b></span>
                  </li>
                ))}
              </ul>
            )}
            <MaterialsStrip />
          </Card>
        </>
      )}
    </>,
  );
}
