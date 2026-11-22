/**
 * Monitoring configuration of the Meta collector (admin): country, queries (keywords / page ids), optional language
 * filter, date window with overlap, own budgets and the opt-in media / landing acquisition. Saved with PUT /config.
 * Every limit is a HYDRA configuration proposal, not a Meta limit.
 */
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "../../Button";
import { Panel } from "../../Panel";
import { errorPl } from "../../../api/watchRt";
import { metaApi, metaKeys, PLATFORMS, type MetaConfig, type MetaQuery, type MetaRules } from "../../../api/metaAds";

const DEFAULT_RULES: MetaRules = { keywords: [], celebrities: [], page_name_patterns: [], same_text_min_pages: 3, alert_min_priority: "medium" };
const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);

const MAX_ENABLED = 10;
const input = "h-7 min-w-0 rounded-[4px] border border-line bg-panel px-2 text-sm";
const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

function Num({ id, label, value, onChange, min = 0, disabled }: { id: string; label: string; value: number; onChange: (v: number) => void; min?: number; disabled?: boolean }) {
  return (
    <label htmlFor={id} className="flex flex-col gap-1 text-xs text-subtle">
      {label}
      <input id={id} type="number" min={min} value={Number.isFinite(value) ? value : 0} disabled={disabled} onChange={(e) => onChange(Number(e.target.value))} className={`${input} w-32`} />
    </label>
  );
}

export function MetaConfigForm({ config, isAdmin }: { config: MetaConfig; isAdmin: boolean }) {
  const qc = useQueryClient();
  const [c, setC] = useState<MetaConfig>(config);
  const [countries, setCountries] = useState(config.countries.join(", "));
  const [langs, setLangs] = useState(config.languages.join(", "));
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const rules0 = { ...DEFAULT_RULES, ...(config.rules ?? {}) };
  const [kw, setKw] = useState(rules0.keywords.join("\n"));
  const [celebs, setCelebs] = useState(rules0.celebrities.join("\n"));
  const [pagePat, setPagePat] = useState(rules0.page_name_patterns.join("\n"));
  useEffect(() => {
    setC(config); setCountries(config.countries.join(", ")); setLangs(config.languages.join(", "));
    const r = { ...DEFAULT_RULES, ...(config.rules ?? {}) };
    setKw(r.keywords.join("\n")); setCelebs(r.celebrities.join("\n")); setPagePat(r.page_name_patterns.join("\n"));
  }, [config]);
  const rules = { ...DEFAULT_RULES, ...(c.rules ?? {}) };
  const setRule = <K extends keyof MetaRules>(k: K, v: MetaRules[K]) => setC((p) => ({ ...p, rules: { ...DEFAULT_RULES, ...(p.rules ?? {}), [k]: v } }));
  const platforms = c.platforms ?? [];
  const set = <K extends keyof MetaConfig>(k: K, v: MetaConfig[K]) => setC((p) => ({ ...p, [k]: v }));
  const setQ = (i: number, patch: Partial<MetaQuery>) => setC((p) => ({ ...p, queries: p.queries.map((q, j) => (j === i ? { ...q, ...patch } : q)) }));
  const enabled = c.queries.filter((q) => q.enabled !== false).length;
  const save = useMutation({
    mutationFn: () => metaApi.saveConfig({
      ...c,
      countries: list(countries).map((x) => x.toUpperCase()),
      languages: list(langs).map((x) => x.toLowerCase()),
      queries: c.queries.filter((q) => q.value.trim()).map((q) => ({ kind: q.kind, value: q.value.trim(), label: q.label || null, enabled: q.enabled !== false })),
      date_from: c.date_from || "",
      date_to: c.date_to || "",
      platforms,
      rules: { ...rules, keywords: lines(kw), celebrities: lines(celebs), page_name_patterns: lines(pagePat) },
    }),
    onSuccess: (r) => { qc.setQueryData(metaKeys.config, r); setMsg({ ok: true, text: `Zapisano konfigurację (wersja ${r.config_version}).` }); void qc.invalidateQueries({ queryKey: metaKeys.all }); },
    onError: (e) => setMsg({ ok: false, text: errorPl(e) }),
  });
  const ro = !isAdmin;
  return (
    <Panel title="Konfiguracja monitoringu" actions={<span className="text-xs text-muted">wersja {config.config_version}</span>}>
      <form
        data-testid="meta-config"
        className="flex flex-col gap-4"
        onSubmit={(e) => { e.preventDefault(); if (!ro) save.mutate(); }}
      >
        <fieldset disabled={ro} className="flex flex-col gap-4">
          <div className="flex flex-wrap gap-3">
            <label htmlFor="meta-countries" className="flex flex-col gap-1 text-xs text-subtle">
              Kraje dotarcia (kody, przecinek)
              <input id="meta-countries" value={countries} onChange={(e) => setCountries(e.target.value)} className={`${input} w-40`} />
            </label>
            <label htmlFor="meta-status" className="flex flex-col gap-1 text-xs text-subtle">
              Status reklam
              <select id="meta-status" value={c.ad_active_status} onChange={(e) => set("ad_active_status", e.target.value as MetaConfig["ad_active_status"])} className={input}>
                <option value="ALL">wszystkie</option>
                <option value="ACTIVE">aktywne</option>
                <option value="INACTIVE">nieaktywne</option>
              </select>
            </label>
            <label htmlFor="meta-langs" className="flex flex-col gap-1 text-xs text-subtle">
              Filtr języka (opcjonalny)
              <input id="meta-langs" value={langs} placeholder="np. pl" onChange={(e) => setLangs(e.target.value)} className={`${input} w-32`} />
              <span className="max-w-56 text-2xs text-muted">Puste = bez filtra. Filtr języka zawęża wyniki i może pominąć polskojęzyczne reklamy oznaczone inaczej.</span>
            </label>
          </div>

          <div>
            <div className="mb-1 flex items-center gap-2 text-xs text-subtle">
              Hasła i strony ({enabled} / {MAX_ENABLED} aktywnych)
              {enabled > MAX_ENABLED && <span role="alert" className="text-err">Za dużo aktywnych zapytań (maks. {MAX_ENABLED}).</span>}
            </div>
            <ul className="flex flex-col gap-2" data-testid="meta-queries">
              {c.queries.map((q, i) => (
                <li key={q.id ?? `new-${i}`} className="flex flex-wrap items-center gap-2">
                  <label className="sr-only" htmlFor={`mq-kind-${i}`}>Rodzaj zapytania</label>
                  <select id={`mq-kind-${i}`} value={q.kind} onChange={(e) => setQ(i, { kind: e.target.value as MetaQuery["kind"] })} className={input}>
                    <option value="keyword">hasło</option>
                    <option value="page_ids">identyfikatory stron</option>
                  </select>
                  <label className="sr-only" htmlFor={`mq-value-${i}`}>Wartość</label>
                  <input id={`mq-value-${i}`} value={q.value} maxLength={100} placeholder={q.kind === "keyword" ? "hasło (do 100 znaków)" : "do 10 ID, przecinek"} onChange={(e) => setQ(i, { value: e.target.value })} className={`${input} w-64`} />
                  <label className="sr-only" htmlFor={`mq-label-${i}`}>Etykieta</label>
                  <input id={`mq-label-${i}`} value={q.label ?? ""} placeholder="etykieta" onChange={(e) => setQ(i, { label: e.target.value })} className={`${input} w-40`} />
                  <label className="inline-flex items-center gap-1 text-xs">
                    <input type="checkbox" checked={q.enabled !== false} onChange={(e) => setQ(i, { enabled: e.target.checked })} /> aktywne
                  </label>
                  {q.origin && q.origin !== "manual" && <span className="text-2xs text-muted">pochodzenie: {q.origin}</span>}
                  {q.last_status && <span className="text-2xs text-muted">ostatnio: {q.last_status}</span>}
                  <Button size="sm" variant="ghost" onClick={() => setC((p) => ({ ...p, queries: p.queries.filter((_, j) => j !== i) }))}>Usuń</Button>
                </li>
              ))}
            </ul>
            <Button size="sm" className="mt-2" onClick={() => setC((p) => ({ ...p, queries: [...p.queries, { kind: "keyword", value: "", label: "", enabled: true }] }))}>Dodaj zapytanie</Button>
            <p className="mt-1 text-2xs text-muted">Do Meta trafiają tylko wpisane tu hasła i identyfikatory stron. Wartości z prywatnego korpusu nie są wysyłane.</p>
          </div>

          <div className="flex flex-wrap gap-3">
            <Num id="meta-window" label="Okno dat (dni)" value={c.date_window_days} min={1} onChange={(v) => set("date_window_days", v)} />
            <Num id="meta-overlap" label="Nakładka okna (dni)" value={c.overlap_days} onChange={(v) => set("overlap_days", v)} />
            <Num id="meta-budget" label="Budżet HYDRY: żądań / h" value={c.budget_per_h} min={1} onChange={(v) => set("budget_per_h", v)} />
            <Num id="meta-pages" label="Stron na zapytanie w obiegu" value={c.max_pages_per_query} min={1} onChange={(v) => set("max_pages_per_query", v)} />
            <Num id="meta-interval" label="Odstęp obiegu (s)" value={c.interval_s} min={60} onChange={(v) => set("interval_s", v)} />
            <Num id="meta-analyses" label="Pełnych analiz / h" value={c.max_analyses_per_h} onChange={(v) => set("max_analyses_per_h", v)} />
            <Num id="meta-bytes" label="Limit bajtów materiałów" value={c.max_media_bytes} onChange={(v) => set("max_media_bytes", v)} />
          </div>
          <fieldset className="flex flex-col gap-3 rounded-[4px] border border-line p-3" data-testid="meta-rules">
            <legend className="px-1 text-xs font-semibold text-fg">Reguły wyszukiwania (automatyczne wyszukiwanie)</legend>
            <p className="text-2xs text-muted">
              Słowa kluczowe = hasła i strony powyżej (wysyłane do Meta). Data wystąpienia = okno „ostatnie N dni” powyżej oraz opcjonalny zakres od–do
              (ad_delivery_date_min / _max). Inne reguły poniżej działają lokalnie w ocenie wstępnej (nic nie jest wysyłane).
            </p>
            <div className="flex flex-wrap gap-3">
              <label htmlFor="meta-date-from" className="flex flex-col gap-1 text-xs text-subtle">
                Data wystąpienia od (opcjonalnie)
                <input id="meta-date-from" type="date" value={c.date_from ?? ""} onChange={(e) => set("date_from", e.target.value || null)} className={`${input} w-40`} />
              </label>
              <label htmlFor="meta-date-to" className="flex flex-col gap-1 text-xs text-subtle">
                Data wystąpienia do (opcjonalnie)
                <input id="meta-date-to" type="date" value={c.date_to ?? ""} onChange={(e) => set("date_to", e.target.value || null)} className={`${input} w-40`} />
              </label>
              <div className="flex flex-col gap-1 text-xs text-subtle">
                Platformy (puste = wszystkie)
                <div className="flex flex-wrap gap-2" data-testid="meta-platforms">
                  {PLATFORMS.map(([id, label]) => (
                    <label key={id} className="inline-flex items-center gap-1 text-xs text-fg">
                      <input type="checkbox" checked={platforms.includes(id)} onChange={(e) => set("platforms", e.target.checked ? [...platforms, id] : platforms.filter((x) => x !== id))} /> {label}
                    </label>
                  ))}
                </div>
              </div>
            </div>
            <div className="flex flex-wrap gap-3">
              <label htmlFor="meta-rule-kw" className="flex flex-col gap-1 text-xs text-subtle">
                Własne słowa kluczowe (jedno w wierszu)
                <textarea id="meta-rule-kw" rows={3} value={kw} onChange={(e) => setKw(e.target.value)} className="w-56 rounded-[4px] border border-line bg-panel px-2 py-1 text-sm" />
                <span className="text-2xs text-muted">Dodawane do wbudowanej listy słów-przynęt.</span>
              </label>
              <label htmlFor="meta-rule-celebs" className="flex flex-col gap-1 text-xs text-subtle">
                Nazwiska / marki do podszycia (jedno w wierszu)
                <textarea id="meta-rule-celebs" rows={3} value={celebs} onChange={(e) => setCelebs(e.target.value)} className="w-56 rounded-[4px] border border-line bg-panel px-2 py-1 text-sm" />
              </label>
              <label htmlFor="meta-rule-pages" className="flex flex-col gap-1 text-xs text-subtle">
                Wzorce nazwy strony (wyrażenia regularne, jedno w wierszu)
                <textarea id="meta-rule-pages" rows={3} value={pagePat} onChange={(e) => setPagePat(e.target.value)} className="w-56 rounded-[4px] border border-line bg-panel px-2 py-1 font-mono text-xs" />
              </label>
            </div>
            <div className="flex flex-wrap gap-3">
              <Num id="meta-rule-same" label="Ten sam tekst na ≥ N stronach" value={rules.same_text_min_pages} min={2} onChange={(v) => setRule("same_text_min_pages", v)} />
              <label htmlFor="meta-rule-alert" className="flex flex-col gap-1 text-xs text-subtle">
                Próg alertu (priorytet oceny wstępnej)
                <select id="meta-rule-alert" value={rules.alert_min_priority} onChange={(e) => setRule("alert_min_priority", e.target.value as MetaRules["alert_min_priority"])} className={input}>
                  <option value="low">niski i wyżej</option>
                  <option value="medium">średni i wyżej</option>
                  <option value="high">tylko wysoki</option>
                </select>
                <span className="max-w-56 text-2xs text-muted">Przypisanie do znanej kampanii zawsze daje alert.</span>
              </label>
            </div>
          </fieldset>
          <p className="text-2xs text-muted">Wszystkie limity są własną konfiguracją HYDRY (propozycja pilotażowa), a nie limitami Meta; ograniczenia Meta mają pierwszeństwo.</p>

          <div className="flex flex-col gap-2 text-sm">
            <label className="inline-flex items-center gap-2">
              <input type="checkbox" data-testid="media-fetch" checked={c.media_fetch} onChange={(e) => set("media_fetch", e.target.checked)} />
              Pobieraj materiały z podglądu reklamy (opcja świadoma, domyślnie wyłączona)
            </label>
            <label className="inline-flex items-center gap-2">
              <input type="checkbox" checked={c.landing_fetch} onChange={(e) => set("landing_fetch", e.target.checked)} />
              Pobieraj strony docelowe reklam (izolowane pobranie bez JS, domyślnie wyłączone)
            </label>
            <label className="inline-flex items-center gap-2">
              <input type="checkbox" checked={c.expansion.enabled} onChange={(e) => set("expansion", { ...c.expansion, enabled: e.target.checked })} />
              Rozszerzaj obserwację o inne reklamy tej samej strony (hipoteza do weryfikacji)
            </label>
            <div className="flex flex-wrap gap-3 pl-6">
              <Num id="meta-exp-depth" label="Maks. głębokość" value={c.expansion.max_depth} disabled={!c.expansion.enabled || ro} onChange={(v) => set("expansion", { ...c.expansion, max_depth: v })} />
              <Num id="meta-exp-pages" label="Maks. stron" value={c.expansion.max_pages} disabled={!c.expansion.enabled || ro} onChange={(v) => set("expansion", { ...c.expansion, max_pages: v })} />
            </div>
          </div>
        </fieldset>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" variant="primary" disabled={ro || save.isPending || enabled > MAX_ENABLED} data-testid="meta-config-save">Zapisz konfigurację</Button>
          {ro && <span className="text-xs text-muted">Zmiana konfiguracji wymaga roli administratora.</span>}
          {msg && <span role={msg.ok ? "status" : "alert"} className={msg.ok ? "text-xs text-ok" : "text-xs text-err"}>{msg.text}</span>}
        </div>
      </form>
    </Panel>
  );
}
