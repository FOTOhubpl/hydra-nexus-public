/**
 * Watchlist matches (WATCH-RT): list from GET /v1/watch/rt/matches plus `watch.match` stream events, and the detail
 * of the selected match: indicator, linked campaign, rarity in the corpus (bits / df, informative, not a probability),
 * evidence, and the analyst actions (confirm, dismiss, open campaign, create incident through the open-set assigner).
 */
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Badge, type BadgeVariant } from "../Badge";
import { Button } from "../Button";
import { InfoTip } from "../Tooltip";
import { PropertyList } from "../PropertyList";
import { DecisionBadge } from "../evidence";
import { ApiErrorState } from "../ApiErrorState";
import { cx } from "../../lib/cx";
import { plNumber } from "../../lib/evidence";
import {
  MATCH_STATUS_LABEL, ORIGIN_LABEL, SOURCE_SHORT, STRENGTH_LABEL, errorPl, kindLabel, rtApi, rtKeys, timePl, useRtMatches, watchRtBus,
  type MatchStatus, type RtAssign, type RtMatch,
} from "../../api/watchRt";
import { ReplayTag, Term } from "./parts";

const STATUS_VARIANT: Record<string, BadgeVariant> = { new: "danger", acknowledged: "success", dismissed: "neutral" };

/** The shared decision pill for assign / new_campaign / abstain; other outcomes (error, unavailable) as plain badges. */
function Decision({ value }: { value: string }) {
  if (value === "assign" || value === "new_campaign" || value === "abstain") return <DecisionBadge value={value} />;
  return <Badge variant={value === "error" ? "danger" : "neutral"}>{value === "error" ? "błąd przypisania" : value}</Badge>;
}

export function MatchPanel({ selectedId, onSelect }: { selectedId: number | null; onSelect: (id: number | null) => void }) {
  const [status, setStatus] = useState("");
  const q = useRtMatches(status);
  const [live, setLive] = useState<RtMatch[]>([]);
  useEffect(() => watchRtBus.subscribe((e) => {
    if (e.type === "match") setLive((prev) => [e.match, ...prev.filter((m) => m.id !== e.match.id)].slice(0, 500));
  }), []);
  const matches = useMemo(() => {
    const byId = new Map<number, RtMatch>();
    for (const m of live) if (!status || m.status === status) byId.set(m.id, m);
    for (const m of q.data?.matches ?? []) byId.set(m.id, m); // server copy wins (status changes)
    return [...byId.values()].sort((a, b) => b.id - a.id);
  }, [q.data, live, status]);
  const selected = matches.find((m) => m.id === selectedId) ?? live.find((m) => m.id === selectedId) ?? null;

  return (
    <section className="surface-card flex min-w-0 flex-col" aria-labelledby="rt-matches-title">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3 sm:px-5">
        <h2 id="rt-matches-title" className="text-sm font-semibold text-fg">Dopasowania watchlisty</h2>
        <InfoTip content="Pozycja ze strumienia zawiera wskaźnik (domenę, telefon, IBAN, identyfikator…) zapisany na watchliście, zwykle wyciągnięty z przeanalizowanej kampanii. Dopasowanie to kandydat do weryfikacji, nie potwierdzone przypisanie." />
        <select aria-label="Filtr statusu dopasowań" value={status} onChange={(e) => setStatus(e.target.value)} className="ml-auto h-7 rounded-[4px] border border-line bg-panel px-2 text-sm" data-testid="match-status-filter">
          <option value="">wszystkie</option>
          <option value="new">nowe</option>
          <option value="acknowledged">potwierdzone</option>
          <option value="dismissed">odrzucone</option>
        </select>
      </header>
      {q.isError && !q.data ? (
        <div className="p-3"><ApiErrorState error={q.error} resource="dopasowania" endpoint="/v1/watch/rt/matches" compact /></div>
      ) : matches.length === 0 ? (
        <p className="p-4 text-sm text-subtle" data-testid="matches-empty">{q.isPending ? "Wczytywanie dopasowań…" : "Brak dopasowań."}</p>
      ) : (
        <ul className="max-h-60 overflow-y-auto" aria-label="Lista dopasowań" data-testid="match-list">
          {matches.map((m) => (
            <li key={m.id}>
              <button
                type="button"
                aria-pressed={m.id === selectedId}
                onClick={() => onSelect(m.id)}
                className={cx("flex w-full min-w-0 flex-wrap items-center gap-2 border-b border-line px-4 py-2 text-left text-xs hover:bg-hover sm:px-5", m.id === selectedId && "bg-accent-soft")}
                data-testid="match-row"
              >
                <span className="shrink-0 text-subtle">{kindLabel(m.indicator_kind)}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-fg">{m.indicator_display}</span>
                {m.campaign_id && <span className="shrink-0 font-mono text-muted">{m.campaign_id}</span>}
                <Badge variant={STATUS_VARIANT[m.status] ?? "neutral"}>{MATCH_STATUS_LABEL[m.status] ?? m.status}</Badge>
                {m.replay && <ReplayTag />}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="border-t border-line p-4 sm:p-5">
        {selected ? <MatchDetail key={selected.id} m={selected} /> : <p className="text-sm text-subtle">Wybierz dopasowanie z listy albo wiersz strumienia oznaczony „dopasowanie”.</p>}
      </div>
    </section>
  );
}

function MatchDetail({ m }: { m: RtMatch }) {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<string | null>(null);
  const [result, setResult] = useState<RtAssign | null>(m.assign ?? null);
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["watch-rt", "matches"] });
    void qc.invalidateQueries({ queryKey: rtKeys.status });
  };
  const setStatus = useMutation({
    mutationFn: (s: MatchStatus) => rtApi.setMatch(m.id, s),
    onSuccess: (r) => { setMsg(`Zapisano: ${MATCH_STATUS_LABEL[r?.status ?? ""] ?? r?.status ?? "zmiana statusu"}.`); refresh(); },
    onError: (e) => setMsg(errorPl(e)),
  });
  const incident = useMutation({
    mutationFn: () => rtApi.incident(m.id),
    onSuccess: (r) => { setResult(r?.assign ?? r?.match?.assign ?? null); setMsg(r?.assign?.message_pl ?? "Utworzono incydent."); refresh(); },
    onError: (e) => setMsg(errorPl(e)),
  });
  const busy = setStatus.isPending || incident.isPending;
  const evidence = m.evidence && typeof m.evidence === "object" ? m.evidence : null;

  return (
    <div className="space-y-3" data-testid="match-detail">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="info">{kindLabel(m.indicator_kind)}</Badge>
        <span className="min-w-0 font-mono text-sm break-all text-fg">{m.indicator_display}</span>
        {m.replay && <ReplayTag />}
      </div>
      <dl className="grid grid-cols-[minmax(7rem,max-content)_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-xs">
        <dt className="text-subtle">Pozycja</dt>
        <dd className="min-w-0 font-mono break-all text-fg">{m.item?.value_defanged ?? "—"}</dd>
        <dt className="text-subtle">Źródło</dt>
        <dd className="text-fg">{SOURCE_SHORT[m.source] ?? m.source} · {timePl(m.item?.first_seen ?? m.created_at)}</dd>
        <dt className="text-subtle"><Term tip="Skąd pochodzi wpis watchlisty: z przeanalizowanej kampanii, dodany ręcznie przez analityka albo z odtwarzanego zestawu.">Pochodzenie</Term></dt>
        <dd className="text-fg">{ORIGIN_LABEL[m.origin] ?? m.origin}</dd>
        <dt className="text-subtle">Kampania</dt>
        <dd className="flex min-w-0 flex-wrap items-center gap-2 text-fg">
          {m.campaign_id ? (
            <Link to={`/campaigns/${encodeURIComponent(m.campaign_id)}`} className="text-accent hover:underline">
              <span className="font-mono">{m.campaign_id}</span>{m.campaign_label ? <span className="ml-1 text-fg">{m.campaign_label}</span> : null}
            </Link>
          ) : m.campaign_label ? (
            <span className="text-fg">{m.campaign_label}{m.origin === "replay" ? <span className="text-subtle"> (z odtworzenia, bez kampanii w korpusie)</span> : null}</span>
          ) : <span className="text-subtle">{m.origin === "replay" ? "brak (wpis z odtworzenia)" : "brak (wpis ręczny)"}</span>}
          {result?.decision && <Decision value={result.decision} />}
        </dd>
        <dt className="text-subtle"><Term tip="Silne: wskaźnik jednoznaczny (np. pełny URL, IBAN, identyfikator reklamy). Słabe: wskaźnik ogólniejszy (np. słowo kluczowe w certyfikacie), wymaga weryfikacji.">Siła</Term></dt>
        <dd><Badge variant={m.strength === "strong" ? "danger" : "warning"}>{STRENGTH_LABEL[m.strength] ?? m.strength}</Badge></dd>
        <dt className="text-subtle"><Term tip="Bity mierzą, jak rzadki jest ten wskaźnik w przeanalizowanym korpusie (df = w ilu incydentach wystąpił). To informacja pomocnicza, nie prawdopodobieństwo przynależności.">Rzadkość</Term></dt>
        <dd className="text-fg" data-testid="match-rarity">
          rzadkość w korpusie (informacyjnie, to nie prawdopodobieństwo):{" "}
          <span className="font-mono">{m.bits != null ? `${plNumber(m.bits, 1)} bit` : "—"}</span>
          {m.df != null && <span className="font-mono"> · df {m.df}{m.n_incidents != null ? ` z ${m.n_incidents} incydentów` : ""}</span>}
        </dd>
        <dt className="text-subtle">Status</dt>
        <dd><Badge variant={STATUS_VARIANT[m.status] ?? "neutral"}>{MATCH_STATUS_LABEL[m.status] ?? m.status}</Badge></dd>
        {m.incident_ids && m.incident_ids.length > 0 && (
          <>
            <dt className="text-subtle">Incydenty kampanii</dt>
            <dd className="flex flex-wrap gap-1">
              {m.incident_ids.slice(0, 8).map((id) => <Link key={id} to={`/incident/${encodeURIComponent(id)}`} className="font-mono text-accent hover:underline">{id}</Link>)}
              {m.incident_ids.length > 8 && <span className="text-subtle">+{m.incident_ids.length - 8}</span>}
            </dd>
          </>
        )}
      </dl>
      <div>
        <div className="mb-1 text-xs font-medium text-subtle">Dowody</div>
        {evidence ? <div className="overflow-hidden rounded-lg border border-line"><PropertyList data={evidence} /></div> : <p className="text-xs text-subtle">Brak zapisanych dowodów.</p>}
      </div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="primary" disabled={busy || m.status === "acknowledged"} onClick={() => setStatus.mutate("acknowledged")}>Potwierdź</Button>
        <Button size="sm" disabled={busy || m.status === "dismissed"} onClick={() => setStatus.mutate("dismissed")}>Odrzuć</Button>
        {m.campaign_id ? (
          <Link to={`/campaigns/${encodeURIComponent(m.campaign_id)}`} className="inline-flex h-6 shrink-0 items-center rounded-[4px] border border-line-strong bg-raised px-2 text-xs font-medium text-fg hover:bg-hover">Otwórz kampanię</Link>
        ) : null}
        <Button size="sm" disabled={busy} onClick={() => incident.mutate()}>{incident.isPending ? "Tworzę…" : "Utwórz incydent"}</Button>
      </div>
      {(result || msg) && (
        <div role="status" aria-live="polite" className="space-y-1 rounded-lg border border-line bg-raised p-3 text-xs" data-testid="match-result">
          {result && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-subtle">Wynik przypisania:</span>
              <Decision value={result.decision} />
              {result.campaign_id && <Link to={`/campaigns/${encodeURIComponent(result.campaign_id)}`} className="font-mono text-accent hover:underline">{result.campaign_id}</Link>}
              {result.new_pattern_id && <span>nowy wzorzec <span className="font-mono">{result.new_pattern_id}</span></span>}
            </div>
          )}
          {msg && <p className="text-muted">{msg}</p>}
        </div>
      )}
    </div>
  );
}
