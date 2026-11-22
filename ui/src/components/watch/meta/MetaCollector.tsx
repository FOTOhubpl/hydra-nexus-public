/**
 * Collector panel of the Meta view: state, token presence (never the value), own HYDRA budget, queue, controls
 * (start / pause / resume / scan now) and the layered contract diagnostics. Counters are separate tiles, never summed.
 */
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "../../Button";
import { Panel } from "../../Panel";
import { StatTile } from "../../StatTile";
import { Badge, type BadgeVariant } from "../../Badge";
import { InfoTip } from "../../Tooltip";
import { errorPl, intervalPl, relativePl, timePl } from "../../../api/watchRt";
import { COUNTER_LABEL, DIAG_STATUS_LABEL, metaApi, metaKeys, type Diagnostics, type MetaStatus } from "../../../api/metaAds";

const STATE_VARIANT: Record<string, BadgeVariant> = {
  running: "success", idle: "info", disabled: "neutral", paused: "neutral", deferred_budget: "warning", rate_limited: "warning",
  needs_config: "danger", offline: "warning", error: "danger", engine_stopped: "neutral",
};

const DIAG_VARIANT: Record<string, BadgeVariant> = { pass: "success", fail: "danger", skipped: "neutral", not_run: "neutral" };

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 gap-2 text-sm">
      <dt className="w-40 shrink-0 text-subtle">{k}</dt>
      <dd className="min-w-0 break-words text-fg">{children}</dd>
    </div>
  );
}

export function DiagnosticsList({ d }: { d: Diagnostics }) {
  return (
    <div data-testid="meta-diagnostics" className="mt-3 rounded-lg border border-line p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-semibold text-fg">Diagnostyka kontraktu</span>
        <Badge variant={d.ok ? "success" : "danger"} dot>{d.ok ? "import sprawny" : "import niesprawny"}</Badge>
        {d.graph_api_version && <span className="text-xs text-muted">Graph API {d.graph_api_version}</span>}
        {d.requests != null && <span className="text-xs text-muted">żądań: {d.requests}</span>}
        {d.at && <span className="text-xs text-muted">{timePl(d.at)}</span>}
      </div>
      <ol className="mt-2 space-y-1">
        {d.layers.map((l, i) => (
          <li key={l.id} data-testid={`diag-${l.id}`} data-status={l.status} className="flex flex-wrap items-start gap-2 text-sm">
            <span className="w-5 shrink-0 text-subtle">{i + 1}.</span>
            <Badge variant={DIAG_VARIANT[l.status] ?? "neutral"}>{DIAG_STATUS_LABEL[l.status] ?? l.status}</Badge>
            <span className="font-medium text-fg">{l.label_pl}</span>
            <span className="min-w-0 basis-full pl-7 text-xs text-muted">{l.message_pl}</span>
          </li>
        ))}
      </ol>
      {Object.keys(d.optional_fields ?? {}).length > 0 && (
        <div className="mt-2 text-xs text-muted">
          Pola opcjonalne:{" "}
          {Object.entries(d.optional_fields).map(([f, s]) => (
            <span key={f} className="mr-2 font-mono">{f}: {s === "available" ? "dostępne" : s === "unavailable" ? "niedostępne" : "nieznane"}</span>
          ))}
        </div>
      )}
    </div>
  );
}

export function MetaCollector({ s, isAdmin }: { s: MetaStatus; isAdmin: boolean }) {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<string | null>(null);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const onStatus = (r: MetaStatus) => {
    if (r?.collector) qc.setQueryData(metaKeys.status(s.mode), r);
    setMsg(r?.run ? "Skan zlecony; wynik w kolejce i na liście reklam." : null);
    void qc.invalidateQueries({ queryKey: metaKeys.all });
  };
  const act = useMutation({ mutationFn: metaApi.collector, onSuccess: onStatus, onError: (e) => setMsg(errorPl(e)) });
  const dia = useMutation({ mutationFn: metaApi.diagnose, onSuccess: (d) => { setDiag(d); setMsg(null); void qc.invalidateQueries({ queryKey: metaKeys.status(s.mode) }); }, onError: (e) => setMsg(errorPl(e)) });
  const c = s.collector;
  const b = s.budget;
  const q = s.queue;
  const shown = diag ?? s.contract.last_diagnostics;
  const busy = act.isPending || dia.isPending;
  return (
    <Panel
      title={<span className="inline-flex items-center gap-2">Kolektor Meta Ad Library <InfoTip content="Jeden harmonogram (silnik WATCH-RT) odpytuje bibliotekę reklam w granicach własnego budżetu HYDRY. Zapis obserwacji następuje przed przesunięciem kursora; analiza działa w osobnej, trwałej kolejce." /></span>}
      actions={<Badge variant={STATE_VARIANT[c.state] ?? "neutral"} dot pulse={c.state === "running"}><span data-testid="collector-state" data-state={c.state}>{c.state_pl}</span></Badge>}
    >
      {c.message_pl && <p data-testid="collector-message" className="mb-3 text-sm text-muted">{c.message_pl}</p>}
      <div className="grid gap-4 lg:grid-cols-2">
        <dl className="space-y-1">
          <Row k="Token Meta">
            {s.token.configured ? (
              <span data-testid="token-state">skonfigurowany ({s.token.source === "env" ? "zmienna środowiskowa" : "plik sekretów"}{s.token.last4 ? `, końcówka …${s.token.last4}` : ""})</span>
            ) : (
              <span data-testid="token-state" className="text-err">brak tokenu</span>
            )}{" "}
            <Link to="/integrations" className="text-accent hover:underline">Integracje → Meta Ad Library</Link>
          </Row>
          <Row k="Silnik WATCH-RT">{c.engine_state}</Row>
          <Row k="Odpytywanie">{c.enabled ? `włączone, ${intervalPl(c.interval_s)}` : "wyłączone"}</Row>
          <Row k="Następny termin">{c.next_due ? timePl(c.next_due) : "—"}</Row>
          <Row k="Ostatnie odpytanie">{c.last_poll ? `${timePl(c.last_poll)} (${relativePl(c.last_poll)})` : "nigdy"}</Row>
          <Row k="Ostatni sukces">{c.last_success ? timePl(c.last_success) : "nigdy"}</Row>
          {c.last_error_pl && <Row k="Ostatni błąd"><span data-testid="collector-error" className="text-err">{c.last_error_pl}</span></Row>}
          <Row k="Kontrakt API">{s.contract.graph_api_version ?? "—"}{s.contract.verified_on ? ` (zweryfikowano ${s.contract.verified_on})` : ""}</Row>
        </dl>
        <dl className="space-y-1">
          <Row k="Budżet HYDRY (1 h)">
            <span data-testid="budget">{b.used_60m} / {b.limit_per_h} żądań, pozostało {b.remaining}</span>
          </Row>
          <p className="text-xs text-muted" data-testid="budget-label">{b.label_pl}</p>
          {b.deferred_until && <Row k="Wstrzymano do">{timePl(b.deferred_until)}</Row>}
          {b.last_usage && (
            <Row k="Nagłówki użycia">
              <span className="font-mono text-xs">{Object.entries(b.last_usage).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join("; ")}</span>
            </Row>
          )}
          <Row k="Kolejka analiz">
            <span data-testid="queue-counts">
              oczekuje {q.queued} · w toku {q.running} · zakończone {q.done} · częściowe {q.partial} · błędy {q.failed} · odroczone {q.deferred}
            </span>
          </Row>
          <Row k="Materiały z podglądu">{s.media_fetch_enabled ? "pobieranie włączone (opcja świadoma)" : "pobieranie wyłączone (domyślnie)"}</Row>
          {s.offline && <Row k="Sieć"><span className="text-warn">tryb offline (HYDRA_OFFLINE=1): brak połączeń</span></Row>}
        </dl>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2" data-testid="collector-controls">
        <Button variant="primary" disabled={busy || !isAdmin || (c.enabled && c.state !== "engine_stopped")} onClick={() => act.mutate("start")}>Uruchom</Button>
        <Button disabled={busy || !isAdmin || !c.enabled} onClick={() => act.mutate("pause")}>Wstrzymaj</Button>
        <Button disabled={busy || !isAdmin || c.state !== "paused"} onClick={() => act.mutate("resume")}>Wznów</Button>
        <Button disabled={busy || !isAdmin} onClick={() => act.mutate("poll_now")} data-testid="poll-now">Skanuj teraz</Button>
        <Button disabled={busy || !isAdmin} onClick={() => dia.mutate()} data-testid="diagnose">Sprawdź kontrakt</Button>
        {!isAdmin && <span className="text-xs text-muted">Sterowanie kolektorem wymaga roli administratora.</span>}
        {msg && <span role="alert" className="text-xs text-err" data-testid="collector-alert">{msg}</span>}
      </div>
      {shown && <DiagnosticsList d={shown} />}
    </Panel>
  );
}

export function MetaCounters({ s }: { s: MetaStatus }) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6" data-testid="meta-counters">
      {COUNTER_LABEL.map(([key, label, hint]) => (
        <div key={key} data-testid={`counter-${key}`} title={hint}>
          <StatTile label={label} value={s.counters[key]} />
        </div>
      ))}
    </div>
  );
}
