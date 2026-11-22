/**
 * Watchlist manager (WATCH-RT): indicators the engine looks for in every polled item, grouped by kind. Entries come
 * from analysed campaigns ("Przebuduj z kampanii"), from the analyst (manual, deletable) or from a replay fixture.
 */
import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Badge, type BadgeVariant } from "../Badge";
import { Button } from "../Button";
import { InfoTip } from "../Tooltip";
import { ApiErrorState } from "../ApiErrorState";
import { CampaignLink } from "../../pages/task/common";
import { KIND_LABEL, ORIGIN_LABEL, WATCH_KINDS, errorPl, kindLabel, rtApi, useRtWatchlist, type WatchEntry } from "../../api/watchRt";

const ORIGIN_VARIANT: Record<string, BadgeVariant> = { campaign: "info", manual: "neutral", replay: "warning" };
const GROUP_CAP = 50;

export function Watchlist() {
  const qc = useQueryClient();
  const [kind, setKind] = useState("");
  const [origin, setOrigin] = useState("");
  const [q, setQ] = useState("");
  const list = useRtWatchlist(kind, origin, q.trim());
  const [form, setForm] = useState({ kind: "domain", value: "", label: "" });
  const [msg, setMsg] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["watch-rt", "watchlist"] }); void qc.invalidateQueries({ queryKey: ["watch-rt", "status"] }); };

  const add = useMutation({
    mutationFn: () => rtApi.addWatch({ kind: form.kind, value: form.value.trim(), ...(form.label.trim() ? { label: form.label.trim() } : {}) }),
    onSuccess: (e) => { setMsg({ tone: "ok", text: `Dodano: ${kindLabel(e?.kind ?? form.kind)} ${e?.value_display ?? form.value.trim()}.` }); setForm((f) => ({ ...f, value: "", label: "" })); refresh(); },
    onError: (e) => setMsg({ tone: "err", text: errorPl(e, "Nie udało się dodać wpisu.") }),
  });
  const del = useMutation({
    mutationFn: (e: WatchEntry) => rtApi.deleteWatch(e.id),
    onSuccess: (_r, e) => { setMsg({ tone: "ok", text: `Usunięto: ${e.value_display}.` }); refresh(); },
    onError: (e) => setMsg({ tone: "err", text: errorPl(e, "Nie udało się usunąć wpisu.") }),
  });
  const rebuild = useMutation({
    mutationFn: () => rtApi.rebuild(),
    onSuccess: (r) => { setMsg({ tone: "ok", text: `Przebudowano z kampanii: dodano ${r?.added ?? 0}, usunięto ${r?.removed ?? 0}, razem ${r?.total ?? "—"}.` }); refresh(); },
    onError: (e) => setMsg({ tone: "err", text: errorPl(e, "Przebudowa nie powiodła się.") }),
  });

  const submit = (ev: FormEvent) => {
    ev.preventDefault();
    if (!form.value.trim()) { setMsg({ tone: "err", text: "Podaj wartość wskaźnika." }); return; }
    add.mutate();
  };

  const entries = list.data?.entries ?? [];
  const counts = list.data?.counts ?? {};
  const kinds = [...WATCH_KINDS, ...Object.keys(counts).filter((k) => !(WATCH_KINDS as readonly string[]).includes(k))];
  const groups = kinds.map((k) => ({ k, rows: entries.filter((e) => e.kind === k) })).filter((g) => g.rows.length > 0);
  const total = Object.values(counts).reduce((s, n) => s + n, 0);
  const field = "h-7 min-w-0 rounded-[4px] border border-line bg-panel px-2 text-sm";

  return (
    <section className="surface-card flex min-w-0 flex-col" aria-labelledby="rt-watchlist-title" data-testid="watchlist">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3 sm:px-5">
        <h2 id="rt-watchlist-title" className="text-sm font-semibold text-fg">Watchlista</h2>
        <InfoTip content="Wskaźniki, których silnik szuka w każdej nowej pozycji. Wpisy „z kampanii” powstają z przeanalizowanych kampanii; wpisy ręczne dodaje analityk i tylko te można usuwać." />
        <span className="font-mono text-xs text-muted">{total} wpisów</span>
        <Button size="sm" className="ml-auto" disabled={rebuild.isPending} onClick={() => rebuild.mutate()}>{rebuild.isPending ? "Przebudowuję…" : "Przebuduj z kampanii"}</Button>
      </header>
      <form onSubmit={submit} className="grid grid-cols-1 gap-2 border-b border-line px-4 py-3 sm:grid-cols-[auto_minmax(0,1fr)_minmax(0,12rem)_auto] sm:px-5" aria-label="Dodaj wpis do watchlisty">
        <label className="sr-only" htmlFor="wl-kind">Rodzaj wskaźnika</label>
        <select id="wl-kind" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })} className={field}>
          {WATCH_KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
        </select>
        <label className="sr-only" htmlFor="wl-value">Wartość</label>
        <input id="wl-value" value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} placeholder="wartość, np. przyklad.pl" className={field} autoComplete="off" spellCheck={false} />
        <label className="sr-only" htmlFor="wl-label">Etykieta (opcjonalnie)</label>
        <input id="wl-label" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} placeholder="etykieta (opcjonalnie)" className={field} autoComplete="off" />
        <Button type="submit" variant="primary" disabled={add.isPending}>{add.isPending ? "Dodaję…" : "Dodaj"}</Button>
      </form>
      <p role="status" aria-live="polite" className={`px-4 text-xs sm:px-5 ${msg ? "pt-2" : ""} ${msg?.tone === "err" ? "text-err" : "text-muted"}`} data-testid="watchlist-msg">{msg?.text}</p>
      <div className="grid grid-cols-2 gap-2 px-4 py-2 sm:flex sm:flex-wrap sm:px-5">
        <select aria-label="Filtr rodzaju wpisów" value={kind} onChange={(e) => setKind(e.target.value)} className={field}>
          <option value="">wszystkie rodzaje</option>
          {WATCH_KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}{counts[k] != null ? ` (${counts[k]})` : ""}</option>)}
        </select>
        <select aria-label="Filtr pochodzenia wpisów" value={origin} onChange={(e) => setOrigin(e.target.value)} className={field}>
          <option value="">każde pochodzenie</option>
          {Object.entries(ORIGIN_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <input type="search" aria-label="Szukaj na watchliście" placeholder="Szukaj" value={q} onChange={(e) => setQ(e.target.value)} className={`${field} col-span-2 sm:w-56`} />
      </div>
      <div className="max-h-[28rem] overflow-y-auto px-4 pb-4 sm:px-5">
        {list.isError && !list.data ? <ApiErrorState error={list.error} resource="watchlista" endpoint="/v1/watch/rt/watchlist" compact />
          : list.isPending ? <p className="text-sm text-subtle">Wczytywanie watchlisty…</p>
          : groups.length === 0 ? <p className="text-sm text-subtle">Brak wpisów. Użyj „Przebuduj z kampanii” albo dodaj wpis ręcznie.</p>
          : groups.map((g) => {
            const all = expanded[g.k];
            const rows = all ? g.rows : g.rows.slice(0, GROUP_CAP);
            return (
              <details key={g.k} open className="border-b border-line py-2 last:border-b-0" data-testid={`watch-group-${g.k}`}>
                <summary className="cursor-pointer text-sm font-medium text-fg">{kindLabel(g.k)} <span className="font-mono text-xs text-muted">({counts[g.k] ?? g.rows.length})</span></summary>
                <ul className="mt-1">
                  {rows.map((e) => (
                    <li key={e.id} className="flex min-w-0 flex-wrap items-center gap-2 py-1 text-xs">
                      <span className="min-w-0 flex-1 truncate font-mono text-fg" title={e.value_display}>{e.value_display}</span>
                      <Badge variant={ORIGIN_VARIANT[e.origin] ?? "neutral"}>{ORIGIN_LABEL[e.origin] ?? e.origin}</Badge>
                      {e.campaign_id ? <CampaignLink id={e.campaign_id} label={e.campaign_label ? e.campaign_label.slice(0, 32) : null} /> : null}
                      {e.label && <span className="truncate text-muted">{e.label}</span>}
                      <span className="shrink-0 font-mono text-subtle" title="liczba dopasowań tego wpisu">trafienia: {e.hits ?? 0}</span>
                      {e.origin === "manual" && (
                        <Button size="sm" variant="ghost" aria-label={`Usuń wpis ${e.value_display}`} disabled={del.isPending} onClick={() => del.mutate(e)}>Usuń</Button>
                      )}
                    </li>
                  ))}
                </ul>
                {g.rows.length > GROUP_CAP && (
                  <Button size="sm" variant="ghost" onClick={() => setExpanded({ ...expanded, [g.k]: !all })}>{all ? "Pokaż mniej" : `Pokaż wszystkie (${g.rows.length})`}</Button>
                )}
              </details>
            );
          })}
      </div>
    </section>
  );
}
