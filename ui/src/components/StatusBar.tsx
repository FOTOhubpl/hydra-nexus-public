import { Fragment, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { isApiError, useHealth } from "../api/client";
import { useLiveStatus, type LiveStatus } from "../api/stream";
import { useAppStatus, useCorpora, type CorpusRuns } from "../api/task";
import type { Health } from "../api/types";
import { cx } from "../lib/cx";
import { formatTime } from "../lib/format";
import { useSettings } from "../lib/settings";
import { statusVariant } from "../lib/status";
import { switchRun, useSelectedRun } from "../lib/runSelection";
import { toggleTheme, useTheme } from "../lib/theme";
import { activity, agoPl, useActivity, type ActivityEntry, type ActivityKind } from "../lib/activity";
import { formatDatePl } from "../lib/evidence";
import { formatCount } from "../lib/format";
import { Badge } from "./Badge";
import { Icon } from "./Icon";
import { Popover, PopoverHeader } from "./Popover";
import { Tooltip } from "./Tooltip";
import { UserMenu } from "./UserMenu";

function Field({ name, value }: { name: string; value: string }) {
  return (
    <span className="hidden items-center gap-1 whitespace-nowrap 2xl:inline-flex">
      <span className="text-subtle">{name}</span>
      <span className="font-mono text-fg">{value}</span>
    </span>
  );
}

/** Backend health summary: `status`, `version`, `device`, `offline` when present. */
export function HealthIndicator() {
  const { healthIntervalMs } = useSettings();
  const health = useHealth(healthIntervalMs);
  const data: Health | undefined = health.data;
  const failed = health.isError;
  const offline = failed && (!isApiError(health.error) || health.error.isOffline);

  let state: { label: string; dot: string; text: string };
  if (health.isPending) state = { label: "łączenie", dot: "bg-subtle animate-pulse", text: "text-muted" };
  else if (offline) state = { label: "backend niedostępny", dot: "bg-err", text: "text-err" };
  else if (failed) state = { label: "błąd backendu", dot: "bg-warn", text: "text-warn" };
  else state = { label: "backend działa", dot: "bg-ok", text: "text-fg" };

  const checkedAt = health.dataUpdatedAt || health.errorUpdatedAt;
  const title = failed
    ? `GET /api/health: ${health.error instanceof Error ? health.error.message : String(health.error)}`
    : `GET /api/health · ${checkedAt ? formatTime(checkedAt) : "—"}`;

  return (
    <div className="flex min-w-0 items-center gap-3 text-xs" title={title} data-testid="health">
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
        <span aria-hidden="true" className={cx("size-2 rounded-full", state.dot)} />
        <span className={state.text}>{state.label}</span>
      </span>
      {!failed && data && (
        <>
          {data.status !== undefined && <Badge variant={statusVariant(String(data.status))}>{String(data.status)}</Badge>}
          {data.version !== undefined && <Field name="wersja" value={String(data.version)} />}
          {data.device !== undefined && <Field name="urządzenie" value={String(data.device)} />}
          {data.offline !== undefined && (
            <Badge variant={data.offline ? "info" : "warning"} title="Polityka sieci backendu (pole offline z /health)">
              {data.offline ? "tryb offline" : "sieć włączona"}
            </Badge>
          )}
        </>
      )}
    </div>
  );
}

const LIVE_STATE: Record<Exclude<LiveStatus, "off">, { label: string; dot: string; title: string }> = {
  live: { label: "na żywo", dot: "bg-ok", title: "Połączono ze strumieniem GET /v1/stream: nowy materiał pojawia się bez odświeżania" },
  connecting: { label: "łączenie…", dot: "bg-warn motion-safe:animate-pulse", title: "Łączenie ze strumieniem GET /v1/stream" },
  polling: { label: "odświeżanie co 15 s", dot: "bg-subtle", title: "Strumień niedostępny: dane odświeżane okresowo" },
};

/** Live stream state (api/stream.ts): text plus dot, announced politely; nothing when the stream is not mounted. */
export function LiveIndicator() {
  const status = useLiveStatus();
  if (status === "off") return null;
  const s = LIVE_STATE[status];
  return (
    <span role="status" aria-live="polite" title={s.title} data-testid="live-status" data-status={status}
      aria-label={s.label} className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-full border border-line bg-panel px-2.5 text-xs whitespace-nowrap text-muted">
      <span aria-hidden="true" className={cx("size-2 rounded-full", s.dot, status === "live" && "motion-safe:shadow-[0_0_0_3px_color-mix(in_oklch,var(--color-ok)_22%,transparent)]")} />
      <span className={cx("hidden sm:inline", status === "live" && "text-fg")}>{s.label}</span>
    </span>
  );
}

/** Re-render every `ms` (relative times such as "12 s temu"). */
function useNow(ms = 5000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

/** Time of the last message received from GET /v1/stream in this tab (hello, delta, job, watch). */
export function LastEvent() {
  const { lastEventAt } = useActivity();
  const now = useNow();
  if (lastEventAt == null) return null;
  return (
    <Tooltip content={`Ostatnia wiadomość ze strumienia GET /v1/stream: ${formatTime(lastEventAt)}`} side="bottom">
      <span tabIndex={0} data-testid="last-event" className="hidden rounded-sm text-2xs whitespace-nowrap text-subtle tabular-nums 2xl:inline">{agoPl(lastEventAt, now)}</span>
    </Tooltip>
  );
}

function GlobalSearch({ compact }: { compact: boolean }) {
  return (
    <Tooltip content="Szukaj ekranów, incydentów, kampanii i wartości genów (Ctrl/⌘ K)" side="bottom" className="inline-flex min-w-0 shrink-0 sm:w-full sm:min-w-[9rem] sm:max-w-md sm:flex-1 sm:shrink">
      <button
        type="button"
        onClick={() => window.dispatchEvent(new CustomEvent("hydra:palette"))}
        className={cx("flex size-9 shrink-0 items-center justify-center gap-2 rounded-lg border border-line bg-raised text-left text-sm text-subtle transition-[color,border-color,height] duration-200 hover:border-line-strong hover:text-muted sm:w-full sm:justify-start sm:px-3", compact ? "sm:h-8" : "sm:h-9")}
        aria-label="Szukaj (⌘K)"
        aria-keyshortcuts="Control+K Meta+K"
        data-testid="global-search"
      >
        <Icon name="search" size={16} />
        {/* phones: icon only (the placeholder text was clipped mid-word at 390 px) */}
        <span className="hidden flex-1 truncate sm:inline">Szukaj incydentów, kampanii, domen, hashy, fraz…</span>
        <kbd className="hidden rounded-md border border-line bg-panel px-1.5 font-mono text-2xs text-muted sm:inline">⌘K</kbd>
      </button>
    </Tooltip>
  );
}

const CORPUS_LABEL = (c: string) => (c === "nask" ? "Tylko dane NASK" : c === "upload" ? "Materiał przesłany" : c === "mixed" ? "NASK + zewnętrzne" : c === "unknown" ? "inne przebiegi" : c);

type CorpusItem = CorpusRuns["items"][number];

function durationPl(a: string | null | undefined, b: string | null | undefined): string | null {
  if (!a || !b) return null;
  const s = (Date.parse(b) - Date.parse(a)) / 1000;
  if (!Number.isFinite(s) || s < 0) return null;
  return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1).replace(".", ",")} h`;
}

/** Popover with the details of the selected run and every run of its data set (GET /v1/corpora, GET /v1/status). */
function RunDetails({ current, selected, onPick }: { current: CorpusItem; selected: string; onPick: (run: string) => void }) {
  const status = useAppStatus();
  const run = current.runs.find((r) => r.id === selected) ?? current.runs[0];
  const s = status.data;
  return (
    <Popover label="Szczegóły przebiegu" tip="Szczegóły wybranego przebiegu analizy" testId="run-details" trigger={<Icon name="database" size={16} />}
      triggerClassName="border border-line bg-raised" panelClassName="w-[min(24rem,calc(100vw-1.5rem))]">
      {(close) => (
        <>
          <PopoverHeader title="Przebieg analizy" sub={<>zbiór: <span className="text-fg">{CORPUS_LABEL(current.corpus)}</span> · przebiegi: {formatCount(current.runs.length)}</>} />
          {run && (
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 px-4 py-3 text-xs">
              <dt className="text-subtle">identyfikator</dt><dd className="min-w-0 truncate font-mono text-fg" title={run.id}>{run.id}</dd>
              <dt className="text-subtle">utworzony</dt><dd className="text-fg">{formatDatePl(run.created_at)}</dd>
              <dt className="text-subtle">zakończony</dt><dd className="text-fg">{run.finished_at ? formatDatePl(run.finished_at) : "w toku lub brak danych"}</dd>
              {durationPl(run.created_at, run.finished_at) && <><dt className="text-subtle">czas trwania</dt><dd className="font-mono text-fg">{durationPl(run.created_at, run.finished_at)}</dd></>}
              <dt className="text-subtle">najnowszy w zbiorze</dt><dd className="text-fg">{run.id === current.latest_run ? "tak" : "nie (przebieg archiwalny)"}</dd>
              {s && <><dt className="text-subtle">wzbogacanie</dt><dd className="text-fg">{s.enrichment_mode === "live" ? "na żywo" : "odtwarzanie z cache"}</dd></>}
            </dl>
          )}
          {current.runs.length > 1 && (
            <div className="border-t border-line px-2 py-2">
              <div className="px-2 pb-1 text-2xs font-medium text-subtle">Inne przebiegi tego zbioru</div>
              <ul className="max-h-48 overflow-auto">
                {current.runs.map((r) => (
                  <li key={r.id}>
                    <button type="button" onClick={() => { onPick(r.id); close(); }} aria-current={r.id === selected ? "true" : undefined}
                      className={cx("flex min-h-8 w-full items-center gap-2 rounded-lg px-2 text-left text-xs hover:bg-hover", r.id === selected ? "bg-accent-soft text-fg" : "text-muted")}>
                      <span className="min-w-0 flex-1 truncate font-mono">{r.id}</span>
                      <span className="shrink-0 text-subtle">{formatDatePl(r.created_at)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </Popover>
  );
}

/** Data set (corpus) switch: picks the newest run of a corpus (runs.params.corpus / paths); NASK-only is one click away. */
function CorpusSwitch() {
  const corpora = useCorpora();
  const sel = useSelectedRun();
  const client = useQueryClient();
  const items = corpora.data?.items ?? [];
  if (items.length === 0) return null;
  const current = items.find((c) => c.runs.some((r) => r.id === sel)) ?? items[0]!;
  // Cancel the old run's in-flight requests; both runs keep their cache entries (runs are immutable).
  const pick = (run: string | null) => switchRun(client, run);
  const ordered = [...items].sort((a, b) => (a.corpus === "nask" ? -1 : b.corpus === "nask" ? 1 : a.corpus.localeCompare(b.corpus)));
  return (
    <div className="hidden min-w-0 shrink items-center gap-1.5 lg:flex">
      <div className="flex min-w-[8.5rem] max-w-[24rem] gap-0.5 overflow-x-auto rounded-lg border border-line bg-hover p-0.5 [scrollbar-width:none]" role="group" aria-label="Zbiór danych" data-testid="corpus-switch">
        {ordered.map((c) => (
          <Tooltip key={c.corpus} side="bottom" content={`${CORPUS_LABEL(c.corpus)}: najnowszy przebieg ${c.latest_run} · przebiegów ${c.runs.length}`}>
            <button type="button" onClick={() => pick(c.latest_run)} aria-pressed={current.corpus === c.corpus}
              className={cx("h-8 shrink-0 rounded-md px-3 text-sm whitespace-nowrap transition-colors", current.corpus === c.corpus ? "bg-panel font-medium text-fg shadow-card" : "text-muted hover:text-fg")}>
              {CORPUS_LABEL(c.corpus)}
            </button>
          </Tooltip>
        ))}
      </div>
      {current.runs.length > 1 && (
        <select value={sel ?? current.latest_run} title={sel ?? current.latest_run} onChange={(e) => pick(e.target.value)} aria-label="Przebieg analizy" data-testid="run-select" className="h-9 w-32 min-w-[6rem] shrink rounded-lg border border-line bg-raised px-2 font-mono text-xs text-fg 2xl:w-40">
          {current.runs.map((r) => <option key={r.id} value={r.id}>{r.id}</option>)}
        </select>
      )}
      <RunDetails current={current} selected={sel ?? current.latest_run} onPick={pick} />
    </div>
  );
}

const TONE_DOT: Record<string, string> = { ok: "bg-ok", info: "bg-accent", warn: "bg-warn", err: "bg-err" };
const KIND_LABEL: Record<ActivityKind, string> = { assign: "przypisanie", review: "decyzja", export: "eksport", batch: "partia", job: "zadanie", watch: "monitoring", reset: "przebieg" };

/** One activity row (notifications popover and the dashboard rail). */
export function ActivityRow({ e, now, fresh = false, onNavigate }: { e: ActivityEntry; now: number; fresh?: boolean; onNavigate?: () => void }) {
  const body = (
    <>
      <span aria-hidden="true" className={cx("mt-1.5 size-2 shrink-0 rounded-full", TONE_DOT[e.tone ?? "info"])} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs text-fg">{e.title}</span>
        <span className="flex gap-2 text-2xs text-subtle">
          <span>{KIND_LABEL[e.kind]}</span>
          {e.detail && <span className="min-w-0 truncate">{e.detail}</span>}
          <span className="ml-auto shrink-0 tabular-nums" title={formatTime(e.at)}>{agoPl(e.at, now)}</span>
        </span>
      </span>
      {fresh && <span className="sr-only">(nowe)</span>}
    </>
  );
  const cls = cx("flex w-full items-start gap-2.5 rounded-lg px-2 py-1.5 text-left", fresh && "bg-accent-soft");
  return e.to ? <Link to={e.to} onClick={onNavigate} className={cx(cls, "hover:bg-hover")}>{body}</Link> : <div className={cls}>{body}</div>;
}

/** Notifications fed by the live stream (lib/activity.ts): new assignments, jobs, watch alerts; unread count. */
function Notifications() {
  const { entries, unread, seenAt } = useActivity();
  const now = useNow();
  const [openedSeen, setOpenedSeen] = useState(seenAt);
  return (
    <Popover label="Powiadomienia" badge={unread ? `nieprzeczytane: ${unread}` : undefined} testId="notifications"
      tip={unread ? `Nowe zdarzenia ze strumienia: ${unread}` : "Powiadomienia ze strumienia na żywo"}
      onOpenChange={(o) => { if (o) { setOpenedSeen(seenAt); activity.markSeen(); } }}
      trigger={<>
        <Icon name="bell" />
        {unread > 0 && (
          <span data-testid="notifications-count" className="absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-err px-1 font-mono text-[9px] leading-none font-semibold text-white ring-2 ring-panel motion-safe:animate-[pop-in_200ms_var(--ease-out-soft)]">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </>}>
      {(close) => (
        <>
          <PopoverHeader title="Aktywność na żywo" sub="zdarzenia ze strumienia GET /v1/stream od otwarcia karty" />
          {entries.length === 0 ? (
            <div role="status" className="px-4 py-6 text-center text-xs text-subtle">Brak zdarzeń od otwarcia karty. Nowe przypisania, zadania i alerty monitoringu pojawią się tutaj.</div>
          ) : (
            <ul className="max-h-[min(24rem,60vh)] overflow-auto p-1.5" aria-label="Zdarzenia">
              {entries.slice(0, 30).map((e) => <li key={e.id}><ActivityRow e={e} now={now} fresh={e.at > openedSeen} onNavigate={close} /></li>)}
            </ul>
          )}
          <div className="flex gap-3 border-t border-line px-4 py-2 text-xs">
            <Link to="/queue" onClick={close} className="rounded-sm text-accent hover:underline">kolejka analityka</Link>
            <Link to="/watch" onClick={close} className="rounded-sm text-accent hover:underline">monitoring domen</Link>
          </div>
        </>
      )}
    </Popover>
  );
}

export const SHORTCUTS: { keys: string; label: string }[] = [
  { keys: "Ctrl/⌘ K", label: "Paleta poleceń i wyszukiwanie" },
  { keys: "Ctrl/⌘ B", label: "Zwiń lub rozwiń menu boczne" },
  { keys: "Ctrl/⌘ J", label: "Pokaż lub ukryj panel boczny pulpitu" },
  { keys: "?", label: "Ta lista skrótów" },
  { keys: "Esc", label: "Zamknij okno, podpowiedź lub panel" },
  { keys: "↑ ↓ Enter", label: "Wybór w palecie poleceń" },
];

/** Opens on "?" (Shift+/) outside text fields. */
function Shortcuts() {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key !== "?" || e.metaKey || e.ctrlKey || e.altKey || (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)))) return;
      const btn = root.current?.querySelector<HTMLButtonElement>("button[aria-haspopup]");
      if (btn && btn.getAttribute("aria-expanded") !== "true") { e.preventDefault(); btn.click(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div ref={root} className="hidden sm:block">
      <Popover label="Skróty klawiszowe" tip="Skróty klawiszowe (?)" testId="shortcuts" trigger={<Icon name="keyboard" />}>
        <PopoverHeader title="Skróty klawiszowe" sub="działają poza polami tekstowymi" />
        <dl className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2 px-4 py-3 text-xs">
          {SHORTCUTS.map((s) => (
            <Fragment key={s.keys}>
              <dt><kbd className="rounded-md border border-line bg-raised px-1.5 py-0.5 font-mono text-2xs whitespace-nowrap text-fg">{s.keys}</kbd></dt>
              <dd className="text-muted">{s.label}</dd>
            </Fragment>
          ))}
        </dl>
      </Popover>
    </div>
  );
}

function ThemeToggle() {
  const theme = useTheme();
  const label = theme === "dark" ? "Tryb jasny" : "Tryb ciemny";
  return (
    <Tooltip content={`${label} (motyw zapisywany w przeglądarce)`} side="bottom">
      <button type="button" onClick={toggleTheme} aria-label={label}
        className="flex size-9 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-hover hover:text-fg">
        <Icon name={theme === "dark" ? "sun" : "moon"} />
      </button>
    </Tooltip>
  );
}

export interface Crumb {
  label: string;
  to?: string;
}

/**
 * Floating top bar (same style as the inset sidebar): breadcrumb + page title (the page h1), search (⌘K palette),
 * data set / run switch with run details, live state with last-event time, notifications, shortcuts, theme.
 * `scrolled` (the main view scrolled down): the bar gets compact and lifts (stronger shadow, denser glass).
 */
export function StatusBar({ title, crumbs = [], onMenu, scrolled = false }: { title: string; crumbs?: Crumb[]; onMenu?: () => void; scrolled?: boolean }) {
  return (
    <div className="relative z-20 shrink-0 px-2 pt-2 sm:px-3 sm:pt-3 md:pl-3">
      <header
        data-testid="top-bar"
        data-scrolled={scrolled ? "true" : "false"}
        className={cx(
          "flex items-center gap-2 rounded-2xl border px-2 backdrop-blur-md transition-[height,box-shadow,background-color,border-color] duration-200 ease-[var(--ease-out-soft)] sm:gap-3 sm:px-3",
          scrolled ? "h-12 border-line-strong bg-chrome-strong shadow-float" : "h-14 border-line bg-chrome shadow-card",
        )}
      >
        {onMenu && (
          <button type="button" onClick={onMenu} aria-label="Otwórz menu" data-testid="menu-button" className="flex size-9 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg md:hidden">
            <Icon name="menu" />
          </button>
        )}
        <div className="min-w-0 flex-1 sm:w-44 sm:flex-none sm:shrink-0 2xl:w-60">
          {crumbs.length > 0 && (
            <nav aria-label="Ścieżka" className={cx("hidden overflow-hidden transition-[max-height,opacity] duration-200 sm:block", scrolled ? "max-h-0 opacity-0" : "max-h-4 opacity-100")}>
              <ol className="flex min-w-0 items-center gap-1 text-2xs text-subtle">
                {crumbs.map((c, i) => (
                  <li key={`${c.label}-${i}`} className="flex min-w-0 items-center gap-1">
                    {i > 0 && <span aria-hidden="true">/</span>}
                    {c.to ? <Link to={c.to} className="truncate hover:text-fg hover:underline">{c.label}</Link> : <span className="truncate">{c.label}</span>}
                  </li>
                ))}
              </ol>
            </nav>
          )}
          <h1 className={cx("truncate leading-tight font-semibold tracking-tight text-fg transition-[font-size] duration-200", scrolled ? "text-base" : "text-base sm:text-lg")}>{title}</h1>
        </div>
        <GlobalSearch compact={scrolled} />
        <div className="ml-auto flex min-w-0 shrink-0 items-center gap-1 sm:shrink sm:gap-2">
          <span className="inline-flex shrink-0 items-center gap-1.5">
            <LiveIndicator />
            <LastEvent />
          </span>
          <CorpusSwitch />
          <div className="hidden 2xl:block"><HealthIndicator /></div>
          <span aria-hidden="true" className="mx-0.5 hidden h-5 w-px bg-line sm:block" />
          <Notifications />
          <Shortcuts />
          <ThemeToggle />
          <UserMenu />
        </div>
      </header>
    </div>
  );
}

/** Bottom bar (DESIGN.md §2): offline / enrichment mode, run id, audit chain, jobs. */
export function BottomStatusBar() {
  const status = useAppStatus();
  const s = status.data;
  const sep = <span aria-hidden="true" className="text-line-strong">│</span>;
  return (
    <footer className="flex h-8 shrink-0 items-center gap-3 overflow-hidden px-4 text-xs whitespace-nowrap text-muted sm:px-5" data-testid="bottom-status">
      {status.isError ? (
        <span className="text-err">status niedostępny (backend nie odpowiada)</span>
      ) : !s ? (
        <span className="text-subtle">ładowanie statusu…</span>
      ) : (
        <>
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className={cx("size-1.5 rounded-full", s.active_jobs > 0 ? "animate-pulse bg-accent" : "bg-ok")} />
            {s.active_jobs > 0 ? `zadania w toku: ${s.active_jobs}` : "kolejka zadań pusta"}
          </span>
          {/* phones: only jobs + audit state; mode / run / contract details from sm up */}
          <span className="hidden items-center gap-3 sm:inline-flex" data-testid="status-details">
            {sep}
            <span title="HYDRA_OFFLINE: rdzeń analizy nie używa sieci">{s.offline ? "offline (bez sieci)" : "sieć dozwolona"}</span>
            {sep}
            <span title="ADR-007: wzbogacanie RDAP/DNS/crt.sh; replay = tylko pamięć podręczna">
              wzbogacanie: {s.enrichment_mode === "live" ? "na żywo" : "odtwarzanie z cache"}
            </span>
            {sep}
            <span>
              przebieg:{" "}
              {s.run_id ? <span className="font-mono text-fg">{s.run_id}</span> : <span className="text-subtle">brak (nie uruchomiono analizy)</span>}
            </span>
            {sep}
            <span title={`Wersja kontraktów API: ${s.contracts_version}`}>HYDRA NEXUS v{s.version ?? "0.1.0"}</span>
          </span>
          <span className="ml-auto inline-flex items-center gap-1.5" title="Dziennik zdarzeń z łańcuchem SHA-256 (GET /v1/audit/verify)">
            <Icon name={s.audit.ok ? "chain" : "alert"} size={13} className={s.audit.ok ? "text-ok" : "text-err"} />
            <span className={s.audit.ok ? "text-fg" : "font-semibold text-err"}>
              <span className="hidden sm:inline">dziennik: {s.audit.events} zdarzeń · </span>{s.audit.ok ? "łańcuch poprawny" : `łańcuch przerwany (seq ${s.audit.first_bad_seq})`}
            </span>
            <span className="hidden items-center gap-1.5 sm:inline-flex">
              {sep}
              <Link to="/methodology" className="text-accent hover:underline">decyzje analityka: {s.reviews}</Link>
            </span>
          </span>
        </>
      )}
    </footer>
  );
}
