/**
 * Right rail of the dashboard ("Pulpit"), loaded lazily after the first paint (DashboardPage, React.lazy):
 * live stream activity, top of the analyst queue (GET /v1/review/queue), system state (GET /v1/status, /health,
 * dashboard jobs) and quick actions. Every number comes from those endpoints; empty states say what is missing.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { useHealth } from "../../api/client";
import type { LiveBlock } from "../../api/stream";
import { datasetExportUrl, useAppStatus, useReviewQueue, type DashboardData, type QueueKind } from "../../api/task";
import { ActivityRow } from "../../components/StatusBar";
import { Icon } from "../../components/Icon";
import { InfoTip, Tooltip } from "../../components/Tooltip";
import { CountUp, useFlash } from "../../components/dashboard/bento";
import { useActivity } from "../../lib/activity";
import { cx } from "../../lib/cx";
import { formatDatePl, plNumber } from "../../lib/evidence";
import { formatCount } from "../../lib/format";

const QUEUE_KIND: Record<QueueKind, { label: string; tip: string }> = {
  bridge: { label: "most", tip: "Para łączy dwie kampanie: decyzja analityka rozstrzyga, czy je scalić." },
  near_threshold: { label: "przy progu", tip: "Siła dowodu blisko progu decyzji: mała zmiana przesuwa wynik." },
  abstain: { label: "wstrzymano", tip: "System wstrzymał decyzję z braku wystarczających dowodów." },
  conflict: { label: "konflikt", tip: "Dowody różnych rodzin genów wskazują w różne strony." },
};

const JOB_STATUS: Record<string, { label: string; cls: string }> = {
  done: { label: "zakończone", cls: "text-ok" },
  failed: { label: "błąd", cls: "text-err" },
  running: { label: "w toku", cls: "text-accent" },
  queued: { label: "w kolejce", cls: "text-muted" },
};

function Section({ title, tip, to, toLabel, children, testId, flashKey }: { title: string; tip?: string; to?: string; toLabel?: string; children: React.ReactNode; testId?: string; flashKey?: unknown }) {
  const flash = useFlash(flashKey);
  return (
    <section aria-label={title} data-testid={testId} className={cx("rounded-xl border border-line bg-raised px-3 py-2.5", flash && "live-flash")}>
      <div className="mb-1.5 flex items-center gap-1.5">
        <h2 className="text-xs font-semibold text-fg">{title}</h2>
        {tip && <InfoTip content={tip} label={`Co to jest: ${title}`} />}
        {to && <Link to={to} className="ml-auto rounded-sm text-2xs text-accent hover:underline">{toLabel ?? "więcej"}</Link>}
      </div>
      {children}
    </section>
  );
}

function useNow(ms = 5000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(t); }, [ms]);
  return now;
}

function LiveActivity({ live }: { live: LiveBlock | undefined }) {
  const { entries } = useActivity();
  const now = useNow();
  return (
    <Section title="Aktywność na żywo" testId="rail-activity" flashKey={entries[0]?.id}
      tip="Zdarzenia ze strumienia GET /v1/stream odebrane w tej karcie: nowe przypisania, decyzje analityka, zadania i alerty monitoringu domen.">
      {live && (
        <div className="mb-1.5 flex items-baseline gap-2 text-xs">
          <span className="text-subtle">nowe incydenty od analizy</span>
          <span className="font-mono font-semibold text-fg tabular-nums"><CountUp value={live.new_incidents ?? 0} /></span>
          <span className="ml-auto text-2xs text-subtle">wersja {formatCount(live.version)}</span>
        </div>
      )}
      {entries.length === 0 ? (
        <p role="status" className="py-2 text-xs text-subtle">Brak zdarzeń od otwarcia karty. Nowy materiał z „Nowej analizy” lub harmonogramu pojawi się tu bez odświeżania.</p>
      ) : (
        <ul className="-mx-1 max-h-56 overflow-auto" aria-label="Ostatnie zdarzenia">
          {entries.slice(0, 8).map((e) => <li key={e.id}><ActivityRow e={e} now={now} /></li>)}
        </ul>
      )}
    </Section>
  );
}

function QueueTop() {
  const q = useReviewQueue(5);
  const items = q.data?.items ?? [];
  return (
    <Section title="Kolejka analityka" to="/queue" toLabel="cała kolejka" testId="rail-queue" flashKey={items[0]?.key}
      tip="Sprawy, w których decyzja analityka najbardziej zmieni wynik (GET /v1/review/queue). Kolejność według oczekiwanej wartości decyzji, heurystyka, nie prawdopodobieństwo.">
      {q.isPending ? <p className="py-2 text-xs text-subtle">wczytywanie kolejki…</p>
        : q.isError ? <p role="status" className="py-2 text-xs text-err">Kolejka niedostępna (GET /v1/review/queue).</p>
          : items.length === 0 ? <p role="status" className="py-2 text-xs text-subtle">Kolejka pusta: brak spraw wymagających decyzji w tym przebiegu.</p>
            : (
              <ol className="space-y-1">
                {items.map((it) => {
                  const to = it.a && it.b ? `/pair/${encodeURIComponent(it.a)}/${encodeURIComponent(it.b)}` : it.incident_id ? `/incident/${encodeURIComponent(it.incident_id)}` : "/queue";
                  return (
                    <li key={it.key}>
                      <Link to={to} className="block rounded-lg px-1.5 py-1 hover:bg-hover">
                        <span className="flex items-center gap-1.5 text-2xs">
                          {it.kinds.map((k) => (
                            <span key={k} className="rounded border border-line-strong px-1 text-muted">{QUEUE_KIND[k]?.label ?? k}</span>
                          ))}
                          <span className="ml-auto font-mono text-subtle tabular-nums" title="oczekiwana wartość decyzji (heurystyka)">EV {plNumber(it.ev, 2)}</span>
                        </span>
                        <span className="mt-0.5 block truncate font-mono text-xs text-fg">{it.a && it.b ? `${it.a} ↔ ${it.b}` : it.incident_id ?? it.key}</span>
                        <span className="block truncate text-2xs text-muted" title={it.reason_pl}>{it.reason_pl}{it.bits != null ? ` · ${plNumber(it.bits, 1)} bit` : ""}</span>
                      </Link>
                    </li>
                  );
                })}
              </ol>
            )}
      {q.data && (q.data.skipped.decided > 0 || q.data.unsure.length > 0) && (
        <div className="mt-1.5 text-2xs text-subtle">pominięte: rozstrzygnięte {formatCount(q.data.skipped.decided)} · niepewne {formatCount(q.data.unsure.length)}</div>
      )}
    </Section>
  );
}

function SystemState({ jobs }: { jobs: DashboardData["jobs"] }) {
  const status = useAppStatus();
  const health = useHealth(30_000);
  const s = status.data;
  const models = Array.isArray(health.data?.models) ? (health.data!.models as string[]) : null;
  const row = (label: string, value: React.ReactNode, tip?: string) => (
    <div className="flex items-center gap-2 py-0.5 text-xs">
      {tip ? <Tooltip content={tip} focusable className="inline-flex rounded-sm"><span className="cursor-help text-subtle decoration-dotted underline-offset-2 hover:underline">{label}</span></Tooltip> : <span className="text-subtle">{label}</span>}
      <span className="ml-auto min-w-0 truncate text-right text-fg">{value}</span>
    </div>
  );
  return (
    <Section title="Stan systemu" testId="rail-system" flashKey={s ? `${s.active_jobs}:${s.audit.events}` : undefined}
      tip="GET /v1/status i /health: zadania, łańcuch dziennika audytu, modele lokalne i tryb sieci.">
      {status.isError ? <p role="status" className="text-xs text-err">status niedostępny (backend nie odpowiada)</p> : !s ? <p className="text-xs text-subtle">wczytywanie statusu…</p> : (
        <div>
          {row("zadania w toku", <span className="font-mono tabular-nums"><CountUp value={s.active_jobs} /></span>)}
          {row("dziennik audytu", (
            <span className={cx("inline-flex items-center gap-1", s.audit.ok ? "text-ok" : "font-semibold text-err")}>
              <Icon name={s.audit.ok ? "chain" : "alert"} size={13} />
              {s.audit.ok ? `poprawny · ${formatCount(s.audit.events)} zdarzeń` : `przerwany (seq ${s.audit.first_bad_seq})`}
            </span>
          ), "Każde zdarzenie zawiera skrót SHA-256 poprzedniego (GET /v1/audit/verify): zmiana wpisu wstecz przerywa łańcuch.")}
          {row("decyzje analityka", <span className="font-mono tabular-nums"><CountUp value={s.reviews} /></span>, s.reviews_change_grouping ? "Decyzje analityka zmieniają grupowanie kampanii." : "Decyzje analityka są zapisywane jako adnotacje do przebiegu.")}
          {row("modele", models == null ? "—" : models.length ? <Tooltip content={models.join(", ")} side="left"><span tabIndex={0} className="rounded-sm font-mono">{formatCount(models.length)} lokalnie</span></Tooltip> : "brak", "Modele wczytywane z dysku (/health → models); bez pobierania z sieci.")}
          {row("tryb sieci", s.offline ? "offline (bez sieci)" : "sieć dozwolona", "HYDRA_OFFLINE: rdzeń analizy nie używa sieci.")}
          {row("wzbogacanie", s.enrichment_mode === "live" ? "na żywo" : "odtwarzanie z cache", "ADR-007: RDAP/DNS/crt.sh; replay = tylko pamięć podręczna.")}
        </div>
      )}
      {jobs.length > 0 && (
        <ul className="mt-1.5 border-t border-line pt-1.5 text-2xs" aria-label="Ostatnie zadania">
          {jobs.slice(0, 4).map((j) => {
            const st = JOB_STATUS[j.status] ?? { label: j.status, cls: "text-muted" };
            return (
              <li key={j.id} className="flex items-baseline gap-2 py-0.5">
                <span className="min-w-0 flex-1 truncate text-fg" title={`${j.kind} · ${j.id}`}>{j.kind}</span>
                {j.total > 0 && <span className="shrink-0 font-mono text-subtle tabular-nums">{formatCount(j.done)}/{formatCount(j.total)}</span>}
                <span className={cx("shrink-0", st.cls)}>{st.label}</span>
                <span className="shrink-0 text-subtle">{formatDatePl(j.created_at)}</span>
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

const act = "flex min-h-9 items-center gap-2 rounded-lg border border-line bg-panel px-2.5 text-xs text-fg hover:border-line-strong hover:bg-hover";

function QuickActions({ hasRun, onDemo }: { hasRun: boolean; onDemo: () => void }) {
  return (
    <Section title="Szybkie akcje" testId="rail-actions">
      <div className="grid grid-cols-2 gap-1.5">
        <button type="button" onClick={onDemo} className="col-span-2 flex min-h-9 items-center justify-center gap-2 rounded-lg bg-accent px-2.5 text-xs font-semibold text-canvas shadow-card hover:opacity-90"><Icon name="play" size={14} />Uruchom analizę</button>
        <Link to="/analyse" className={act}><Icon name="plus" size={14} />Nowa analiza</Link>
        <Link to="/data" className={act}><Icon name="upload" size={14} />Import</Link>
        <Link to="/queue" className={act}><Icon name="check" size={14} />Kolejka</Link>
        <Link to="/watch" className={act}><Icon name="eye" size={14} />Monitoring</Link>
        {hasRun ? (
          <Tooltip content="Wzbogacony zbiór danych przebiegu (ZIP podpisany Ed25519, GET /v1/export/dataset; weryfikacja: hydra verify-export)" className="inline-flex">
            <a href={datasetExportUrl()} download className={cx(act, "w-full")}><Icon name="download" size={14} />Eksport</a>
          </Tooltip>
        ) : (
          <Tooltip content="Eksport niedostępny: brak przebiegu analizy. Uruchom analizę, aby utworzyć zbiór do eksportu." focusable className="inline-flex rounded-lg">
            <button type="button" disabled aria-disabled="true" className={cx(act, "w-full opacity-50")}><Icon name="download" size={14} />Eksport</button>
          </Tooltip>
        )}
        <button type="button" onClick={() => window.dispatchEvent(new CustomEvent("hydra:palette"))} className={act}><Icon name="search" size={14} />Szukaj <kbd className="ml-auto font-mono text-2xs text-subtle">⌘K</kbd></button>
      </div>
    </Section>
  );
}

export default function DashboardRail({ data, onDemo }: { data: DashboardData | undefined; onDemo: () => void }) {
  return (
    <div className="space-y-2.5">
      <LiveActivity live={data?.live} />
      <QueueTop />
      <SystemState jobs={data?.jobs ?? []} />
      <QuickActions hasRun={Boolean(data?.run)} onDemo={onDemo} />
    </div>
  );
}
