/**
 * /watch?view=auto "Automatyczne wyszukiwanie" (BONUS-AUTO): the whole automatic search loop on one page, in Polish.
 * Sources -> rules -> schedule and own budget -> cycles (new / changed / unchanged / ended ads) -> analysis (assign to
 * a known campaign / new pattern / abstain, triage) -> alerts and "nowe materiały powiązane z kampaniami". Every
 * number comes from GET /v1/watch/rt/meta/auto (meta_* and watch_* tables); the data modes are never mixed.
 */
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCanWrite } from "../../api/auth";
import { useAutoOverview, PRIORITY_PL, type AutoOverview } from "../../api/autoSearch";
import { META_MODES, MODE_TAB, parseMode, type MetaMode } from "../../api/metaAds";
import { errorPl, rtApi } from "../../api/watchRt";
import { Button } from "../../components/Button";
import { Panel } from "../../components/Panel";
import { StatTile } from "../../components/StatTile";
import { LinkedAdsTable, WatchLinksTable } from "../../components/watch/meta/AutoLinks";
import { ModeBadge, ModeBanner } from "../../components/watch/meta/common";
import { cx } from "../../lib/cx";
import { QueryState } from "./common";

const DEMO_FIXTURE = "auto-search-demo";

function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("pl-PL", { dateStyle: "short", timeStyle: "medium" });
}

const ACTIVE_PL: Record<string, string> = { ALL: "wszystkie", ACTIVE: "tylko aktywne", INACTIVE: "tylko nieaktywne" };

function Rules({ r }: { r: AutoOverview["rules"] }) {
  const row = (k: string, v: React.ReactNode, phrase: string) => (
    <tr className="border-t border-line align-top">
      <td className="py-1 pr-3 text-subtle">{phrase}</td>
      <td className="py-1 pr-3 font-medium text-fg">{k}</td>
      <td className="py-1 text-fg">{v}</td>
    </tr>
  );
  const listOr = (xs: string[], empty: string) => (xs.length ? xs.join(", ") : <span className="text-muted">{empty}</span>);
  return (
    <table className="w-full text-xs" data-testid="auto-rules">
      <thead className="text-left text-subtle"><tr><th className="py-1 pr-3 font-medium">Zadanie NASK</th><th className="py-1 pr-3 font-medium">Reguła</th><th className="py-1 font-medium">Bieżąca wartość</th></tr></thead>
      <tbody>
        {row("Hasła wyszukiwania (do Meta)", listOr(r.keywords_sent, "brak: dodaj w Reklamy Meta → Konfiguracja"), "słowa kluczowe")}
        {row("Strony reklamodawców (page_id)", listOr(r.page_ids_sent, "brak"), "słowa kluczowe")}
        {row("Słowa-przynęty i podszycia (lokalnie)", <>{r.local_keywords_default} wbudowanych{r.local_keywords.length ? ` + własne: ${r.local_keywords.join(", ")}` : ""}{r.celebrities.length ? ` · nazwiska/marki: ${r.celebrities.join(", ")}` : ""}</>, "słowa kluczowe")}
        {row("Okno dat", `ostatnie ${r.date_window_days} dni (nakładka ${r.overlap_days} dni przy kolejnym obiegu)`, "data wystąpienia")}
        {row("Zakres dat od–do", r.date_from || r.date_to ? `${r.date_from ?? "…"} – ${r.date_to ?? "…"}` : <span className="text-muted">bez zakresu</span>, "data wystąpienia")}
        {row("Kraj dotarcia / status", `${r.countries.join(", ")} · ${ACTIVE_PL[r.ad_active_status] ?? r.ad_active_status}`, "inne reguły")}
        {row("Platformy", listOr(r.platforms, "wszystkie"), "inne reguły")}
        {row("Języki", listOr(r.languages, "bez filtra"), "inne reguły")}
        {row("Wzorce nazwy strony", listOr(r.page_name_patterns, "brak"), "inne reguły")}
        {row("Ten sam tekst na wielu stronach", `≥ ${r.same_text_min_pages} strony`, "inne reguły")}
        {row("Próg alertu", `priorytet ${PRIORITY_PL[r.alert_min_priority] ?? r.alert_min_priority} i wyżej; przypisanie do znanej kampanii zawsze`, "inne reguły")}
        {row("Rozszerzanie o inne reklamy tej samej strony", r.expansion.enabled ? `włączone (głębokość ${r.expansion.max_depth}, stron ${r.expansion.max_pages})` : "wyłączone", "powiązane materiały")}
      </tbody>
    </table>
  );
}

export function WatchAuto() {
  const [params, setParams] = useSearchParams();
  const mode = parseMode(params.get("mode"));
  const q = useAutoOverview(mode);
  const canWrite = useCanWrite();
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const setMode = (m: MetaMode) => {
    const next = new URLSearchParams(params);
    next.set("view", "auto");
    if (m === "live") next.delete("mode"); else next.set("mode", m);
    setParams(next, { replace: true });
  };
  const demo = useMutation({
    mutationFn: async () => {
      try { await rtApi.rebuild(); } catch { /* the watchlist rebuild is optional for the Meta part */ }
      return rtApi.replay(DEMO_FIXTURE, 60);
    },
    onSuccess: () => { setMsg({ ok: true, text: "Uruchomiono odtworzenie syntetyczne (2 obiegi co ok. 1 s). Wyniki pojawiają się poniżej w trybie Dane syntetyczne." }); setMode("synthetic"); void qc.invalidateQueries({ queryKey: ["meta-ads"] }); },
    onError: (e) => setMsg({ ok: false, text: errorPl(e) }),
  });
  const d = q.data;
  return (
    <div className="flex flex-col gap-4 p-4" data-testid="watch-auto">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-xl font-semibold text-fg">Automatyczne wyszukiwanie podejrzanych reklam</h2>
        <ModeBadge mode={mode} />
        <div role="radiogroup" aria-label="Tryb danych" className="flex flex-wrap gap-1">
          {META_MODES.map((m) => (
            <button key={m} type="button" role="radio" aria-checked={mode === m} data-testid={`auto-mode-${m}`} onClick={() => setMode(m)}
              className={cx("h-7 rounded-full border px-3 text-xs font-medium whitespace-nowrap", mode === m ? "border-accent-strong bg-accent-strong text-canvas" : "border-line-strong bg-raised text-fg hover:bg-hover")}>
              {MODE_TAB[m]}
            </button>
          ))}
        </div>
        {canWrite && (
          <Button size="sm" className="ml-auto" data-testid="auto-demo" disabled={demo.isPending} onClick={() => demo.mutate()}>
            Demonstracja offline (DANE SYNTETYCZNE)
          </Button>
        )}
      </div>
      <ModeBanner mode={mode} />
      {msg && <p role={msg.ok ? "status" : "alert"} className={msg.ok ? "text-xs text-ok" : "text-xs text-err"}>{msg.text}</p>}
      <ol className="grid gap-2 text-xs text-muted sm:grid-cols-2 xl:grid-cols-5" data-testid="auto-loop">
        <li className="surface-card px-3 py-2"><b className="text-fg">1. Źródła publiczne.</b> Biblioteka reklam Meta (Ad Library API) i kanały WATCH-RT: CERT Polska, URLhaus, OpenPhish, crt.sh.</li>
        <li className="surface-card px-3 py-2"><b className="text-fg">2. Reguły.</b> Słowa kluczowe, data wystąpienia (ostatnie N dni, zakres od–do) i inne reguły (kraj, status, platformy, wzorce nazwy strony, ten sam tekst).</li>
        <li className="surface-card px-3 py-2"><b className="text-fg">3. Okresowe aktualizowanie.</b> Obieg co {d ? Math.round(d.collector.interval_s / 60) : "—"} min z kursorem na zapytanie, rewizje treści i własnym budżetem żądań.</li>
        <li className="surface-card px-3 py-2"><b className="text-fg">4. Analiza.</b> Każda nowa reklama i rewizja = incydent: ekstrakcja genów, przypisanie (otwarty zbiór), ocena wstępna.</li>
        <li className="surface-card px-3 py-2"><b className="text-fg">5. Powiązane materiały.</b> Reklama przypisana do kampanii z przebiegu bazowego = alert „nowy materiał powiązany z kampanią”, także na karcie kampanii.</li>
      </ol>
      <QueryState query={q} what="obieg automatycznego wyszukiwania" endpoint="/v1/watch/rt/meta/auto" />
      {d && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8" data-testid="auto-totals">
            <StatTile label="Ostatnia aktualizacja" value={d.last_update ? when(d.last_update) : null} hint={d.cycles[0] ? `obieg: ${d.cycles[0].trigger_pl}` : "brak obiegów"} />
            <StatTile label="Obiegi zapisane" value={d.totals.cycles_recorded} />
            <StatTile label="Unikalne reklamy" value={d.totals.unique_ads} hint={`rekordów: ${d.totals.records_fetched}`} />
            <StatTile label="Zmienione (rewizje)" value={d.totals.new_revisions} />
            <StatTile label="Przypisane do znanych kampanii" value={d.totals.assigned} hint={`kampanii: ${d.totals.linked_campaigns}`} />
            <StatTile label="Nowy wzorzec" value={d.totals.new_patterns} />
            <StatTile label="Abstencja" value={d.totals.abstained} />
            <StatTile label="Alerty otwarte" value={d.totals.alerts_open} hint={`WATCH-RT nowe z kampanii: ${d.watch_matches.counts.new_to_campaigns}`} />
          </div>

          <Panel title={`Nowe materiały powiązane z wcześniej wykrytymi kampaniami (${d.linked_ads.length})`} actions={<span className="text-xs text-subtle">reklamy przypisane do kampanii przebiegu bazowego</span>}>
            <div data-testid="auto-linked">
              <LinkedAdsTable ads={d.linked_ads} />
            </div>
          </Panel>

          <div className="grid gap-4 xl:grid-cols-2">
            <Panel title="Harmonogram i budżet">
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs" data-testid="auto-schedule">
                <dt className="text-subtle">Kolektor Meta</dt><dd className="text-fg">{d.collector.state_pl}{d.collector.message_pl ? ` · ${d.collector.message_pl}` : ""}</dd>
                <dt className="text-subtle">Silnik WATCH-RT</dt><dd className="text-fg">{d.engine.state}{d.offline ? " · tryb offline (brak połączeń)" : ""}</dd>
                <dt className="text-subtle">Token Meta</dt><dd className="text-fg">{d.token_configured ? "skonfigurowany" : "brak (tryb na żywo niedostępny)"}</dd>
                <dt className="text-subtle">Odstęp obiegu</dt><dd className="text-fg">{Math.round(d.collector.interval_s / 60)} min (+0–10 % losowo)</dd>
                <dt className="text-subtle">Następny obieg</dt><dd className="text-fg">{d.collector.enabled ? when(d.collector.next_due) : "kolektor wyłączony"}</dd>
                <dt className="text-subtle">Ostatni sukces</dt><dd className="text-fg">{when(d.collector.last_success)}</dd>
                <dt className="text-subtle">Budżet żądań HYDRY</dt><dd className="text-fg">{d.budget.used_60m} / {d.budget.limit_per_h} w ostatnich 60 min</dd>
                <dt className="text-subtle">Kolejka analiz</dt><dd className="text-fg">w kolejce {d.queue.queued} · w toku {d.queue.running} · zakończone {d.queue.done + d.queue.partial} · odroczone {d.queue.deferred} · błędy {d.queue.failed}</dd>
              </dl>
              <p className="mt-2 text-2xs text-muted">{d.budget.label_pl}</p>
            </Panel>
            <Panel title="Ocena wstępna (triage) i decyzje">
              <div className="flex flex-col gap-2 text-xs" data-testid="auto-triage">
                <div className="flex flex-wrap gap-2">
                  {(["high", "medium", "low", "unknown"] as const).map((k) => (
                    <span key={k} className="rounded-full border border-line px-2 py-0.5 text-fg">{PRIORITY_PL[k]}: <b className="font-mono">{d.triage[k] ?? 0}</b></span>
                  ))}
                </div>
                <p className="text-muted">Analizy zakończone: {d.totals.analyses_done} (pełne) + {d.totals.analyses_partial} (częściowe, bez materiału wizualnego). Analityk: potwierdzone {d.totals.analyst_confirmed}, odrzucone {d.totals.analyst_dismissed}.</p>
                <p className="text-2xs text-muted">Ocena wstępna to wskazania do sprawdzenia, nie stwierdzenie oszustwa. Przypisanie to decyzja HYDRY (bity dowodu), nie potwierdzenie analityka.</p>
              </div>
            </Panel>
          </div>

          <Panel title="Reguły wyszukiwania" actions={<Link to="/watch?view=meta" className="text-xs text-accent hover:underline">Zmień w Reklamy Meta → Konfiguracja</Link>}>
            <Rules r={d.rules} />
          </Panel>

          <Panel title={`Ostatnie obiegi (${d.cycles.length})`} actions={<span className="text-xs text-subtle">okresowe aktualizowanie: nowe / zmienione / bez zmian / zakończone</span>}>
            {d.cycles.length ? (
              <div className="overflow-x-auto"><table className="w-full text-xs" data-testid="auto-cycles">
                <thead className="text-left text-subtle"><tr>
                  <th className="py-1 pr-2 font-medium">Zakończony</th><th className="py-1 pr-2 font-medium">Wyzwalacz</th><th className="py-1 pr-2 font-medium">Status</th>
                  <th className="py-1 pr-2 font-medium">Żądania</th><th className="py-1 pr-2 font-medium">Reklamy w obiegu</th><th className="py-1 pr-2 font-medium">Nowe</th>
                  <th className="py-1 pr-2 font-medium">Zmienione</th><th className="py-1 pr-2 font-medium">Bez zmian</th><th className="py-1 pr-2 font-medium">Z datą zakończenia emisji</th><th className="py-1 font-medium">Do analizy</th>
                </tr></thead>
                <tbody>
                  {d.cycles.map((c, i) => (
                    <tr key={i} className="border-t border-line" data-testid="auto-cycle-row">
                      <td className="py-1 pr-2 whitespace-nowrap">{when(c.finished_at)}</td><td className="py-1 pr-2">{c.trigger_pl}</td>
                      <td className="py-1 pr-2" title={c.message_pl}>{c.status}</td><td className="py-1 pr-2 font-mono">{c.requests}</td>
                      <td className="py-1 pr-2 font-mono">{c.ads_seen}</td><td className="py-1 pr-2 font-mono" data-testid="cycle-new">{c.new_ads}</td>
                      <td className="py-1 pr-2 font-mono" data-testid="cycle-changed">{c.changed_ads}</td><td className="py-1 pr-2 font-mono">{c.unchanged_ads}</td>
                      <td className="py-1 pr-2 font-mono">{c.ended_ads}</td><td className="py-1 font-mono">{c.queued_analyses}</td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            ) : <p className="text-xs text-muted">Brak zapisanych obiegów w tym trybie danych.</p>}
          </Panel>

          <Panel title={`Dopasowania WATCH-RT do wskaźników kampanii (${d.watch_matches.counts.to_campaigns})`} actions={<Link to="/watch" className="text-xs text-accent hover:underline">Monitoring na żywo →</Link>}>
            <WatchLinksTable matches={d.watch_matches.latest} />
            <p className="mt-2 text-2xs text-muted">Wszystkie dopasowania: {d.watch_matches.counts.total} · nowe: {d.watch_matches.counts.new}. Dopasowanie = wspólny wskaźnik (domena, host, telefon) z kampanią, kandydat do sprawdzenia.</p>
          </Panel>

          <Panel title="Źródła">
            <div className="overflow-x-auto"><table className="w-full text-xs" data-testid="auto-sources">
              <thead className="text-left text-subtle"><tr><th className="py-1 pr-2 font-medium">Źródło</th><th className="py-1 pr-2 font-medium">Włączone</th><th className="py-1 pr-2 font-medium">Stan</th><th className="py-1 pr-2 font-medium">Co</th><th className="py-1 pr-2 font-medium">Ostatnie odpytanie</th><th className="py-1 pr-2 font-medium">Następne</th><th className="py-1 font-medium">Odpytań / pozycji</th></tr></thead>
              <tbody>
                {d.sources.map((s) => (
                  <tr key={s.id} className="border-t border-line">
                    <td className="py-1 pr-2 text-fg">{s.label_pl}</td><td className="py-1 pr-2">{s.enabled ? "tak" : "nie"}</td><td className="py-1 pr-2">{s.state}</td>
                    <td className="py-1 pr-2">{Math.round(s.interval_s / 60)} min</td><td className="py-1 pr-2 whitespace-nowrap">{when(s.last_poll)}</td>
                    <td className="py-1 pr-2 whitespace-nowrap">{s.enabled ? when(s.next_due) : "—"}</td><td className="py-1 font-mono">{s.total_polls} / {s.total_items}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          </Panel>
        </>
      )}
    </div>
  );
}
