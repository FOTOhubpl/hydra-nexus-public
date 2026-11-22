/**
 * "Pakiety jednym kliknięciem": one card per preset. Cards share a CSS subgrid, so the header, the action row
 * (progress bar, summary, button) and the connector list line up across a row and the button sits at the same
 * height in every card; apply results appear below it. A connector that needs a key gets an inline "Połącz" that
 * expands the shared KeyForm right in the row; the card button reads "Uzupełnij klucze (N)" until every key is
 * present (it opens and focuses the first missing form), then "Zastosuj pakiet". After the last missing key is
 * saved the preset is applied again automatically. States carry a glyph and a text label, never colour alone.
 */
import { useState } from "react";
import { Link } from "react-router";
import { applyPreset, useIntegrationMutation, type ConnectResult, type Connector, type Preset, type PresetApplyResult } from "../../../api/integrations";
import { Button } from "../../../components/Button";
import { InfoTip } from "../../../components/Tooltip";
import { cx } from "../../../lib/cx";
import { KeyForm } from "./KeyForm";
import { keyDocHref, needsKeyPl, optionalKeysMissing, READINESS, readinessOf, type Readiness } from "./labels";

type Row = PresetApplyResult["results"][number];

/** Step messages as one clean sentence list joined with " · " (never ".;"). */
export function joinSteps(r: Row): string {
  const parts = (r.steps_pl && r.steps_pl.length ? r.steps_pl : r.message_pl.split(/;\s*/))
    .map((s) => s.trim().replace(/[;,\s]+$/, "").replace(/\.{2,}$/, "."))
    .filter(Boolean);
  return parts.join(" · ");
}

function rowReadiness(c: Connector | undefined, r: Row | undefined): Readiness {
  const base = readinessOf(c);
  if (base !== "ready" || !r) return base;
  if (!r.ok && r.status !== "offline" && r.status !== "not_configured" && r.status !== "planned") return "error";
  return base;
}

export function ReadinessPill({ r, testId }: { r: Readiness; testId?: string }) {
  const s = READINESS[r];
  return (
    <span className={cx("inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-2xs font-medium whitespace-nowrap", s.tone)} data-testid={testId} data-readiness={r}>
      <span aria-hidden="true" className="font-bold">{s.glyph}</span>{s.label}
    </span>
  );
}

function hint(c: Connector | undefined, r: Readiness): string {
  if (!c) return "Nieznany konektor.";
  if (r === "planned") return "Brak implementacji; pakiet go pomija.";
  if (r === "needs_key") return `Brakuje: ${c.secrets.filter((s) => !s.configured).map((s) => s.name).join(", ")}.`;
  if (r === "offline") return c.secrets.length ? "Klucz zapisany; połączenia zablokowane (HYDRA_OFFLINE=1)." : "Nie wymaga klucza; połączenia zablokowane (HYDRA_OFFLINE=1).";
  if (optionalKeysMissing(c)) return `Plik eksportu działa bez klucza. Wysyłka (opcjonalna) wymaga: ${c.secrets.filter((s) => !s.configured).map((s) => s.name).join(", ")}.`;
  return c.enabled ? "Włączony." : "Gotowy do włączenia.";
}

export function summarize(states: Readiness[]): { text: string; counts: Record<Readiness, number> } {
  const counts = { ready: 0, needs_key: 0, offline: 0, error: 0, planned: 0 } as Record<Readiness, number>;
  for (const s of states) counts[s] += 1;
  const parts = [`${counts.ready} z ${states.length} gotowe`];
  if (counts.needs_key) parts.push(needsKeyPl(counts.needs_key));
  if (counts.offline) parts.push(`${counts.offline} offline`);
  if (counts.error) parts.push(`${counts.error} z błędem testu`);
  if (counts.planned) parts.push(`${counts.planned} planowane`);
  return { text: parts.join(", "), counts };
}

const BAR: { key: Readiness; cls: string }[] = [
  { key: "ready", cls: "bg-ok" },
  { key: "needs_key", cls: "bg-warn" },
  { key: "offline", cls: "bg-line-strong" },
  { key: "error", cls: "bg-err" },
  { key: "planned", cls: "bg-line" },
];

function PresetCard({ p, byId, offline }: { p: Preset; byId: Map<string, Connector>; offline: boolean }) {
  const [result, setResult] = useState<PresetApplyResult | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const apply = useIntegrationMutation((id: string) => applyPreset(id));
  const rowsById = new Map((result?.results ?? []).map((r) => [r.connector, r]));
  const states = p.connectors.map((id) => rowReadiness(byId.get(id), rowsById.get(id)));
  const sum = summarize(states);
  const missing = p.connectors.filter((id) => readinessOf(byId.get(id)) === "needs_key");
  const run = () => apply.mutate(p.id, { onSuccess: (data) => setResult(data) });

  const focusFirstMissing = () => {
    const id = missing[0];
    if (!id) return;
    setOpen(id);
    window.setTimeout(() => {
      const el = document.querySelector<HTMLElement>(`[data-testid="preset-${p.id}-${id}-keyform"] input`);
      el?.scrollIntoView?.({ block: "center", behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      el?.focus();
    }, 0);
  };

  const onSaved = (cid: string) => (r: ConnectResult) => {
    const stillMissing = missing.filter((id) => id !== cid);
    if (r.connector.secrets.every((s) => s.configured) && stillMissing.length === 0) run();
  };

  const pending = apply.isPending;
  return (
    <div className="surface-card row-span-3 mb-3 grid min-w-0 grid-rows-subgrid gap-y-3 p-4" data-testid={`preset-${p.id}`}>
      <div className="min-w-0 space-y-1">
        <div className="text-sm font-semibold text-fg">{p.name_pl}</div>
        <p className="text-xs text-muted">{p.description_pl}</p>
      </div>

      <div className="min-w-0 space-y-2 self-start">
        <div className="flex h-1.5 overflow-hidden rounded-full bg-hover" role="img" aria-label={`Stan pakietu: ${sum.text}`} data-testid={`preset-bar-${p.id}`}>
          {BAR.map((b) => sum.counts[b.key] > 0 && <div key={b.key} className={b.cls} style={{ width: `${(sum.counts[b.key] / states.length) * 100}%` }} />)}
        </div>
        <p className="text-xs text-muted" data-testid={`preset-summary-${p.id}`}>{sum.text}</p>
        {missing.length > 0 ? (
          <Button variant="primary" onClick={focusFirstMissing} data-testid={`apply-${p.id}`}>Uzupełnij klucze ({missing.length})</Button>
        ) : (
          <Button variant="primary" onClick={run} disabled={pending} aria-busy={pending} data-testid={`apply-${p.id}`}>{pending ? "Włączanie…" : "Zastosuj pakiet"}</Button>
        )}
        {apply.isError && <div role="alert" className="text-xs text-err">{(apply.error as Error).message}</div>}
      </div>

      <ul className="min-w-0 space-y-2" aria-label={`Konektory pakietu ${p.name_pl}`} data-testid={`preset-result-${p.id}`}>
        {p.connectors.map((id, i) => {
          const c = byId.get(id);
          const r = rowsById.get(id);
          const st = states[i]!;
          const expanded = open === id;
          const formId = `preset-${p.id}-${id}-form`;
          return (
            <li key={id} className="min-w-0 space-y-1.5 border-t border-line pt-2 first:border-t-0 first:pt-0" data-testid={`preset-row-${p.id}-${id}`}>
              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                <ReadinessPill r={st} testId={`preset-state-${p.id}-${id}`} />
                <span className="min-w-0 font-medium text-fg">{c?.name ?? id}</span>
                {st !== "needs_key" && optionalKeysMissing(c) && c && (
                  <span className="ml-auto">
                    <Button size="sm" onClick={() => setOpen(expanded ? null : id)} aria-expanded={expanded} aria-controls={formId} data-testid={`preset-connect-${p.id}-${id}`}>
                      {expanded ? "Zwiń" : "Klucze wysyłki (opcjonalnie)"}
                    </Button>
                  </span>
                )}
                {st === "needs_key" && c && (
                  <span className="ml-auto flex items-center gap-2">
                    <Button size="sm" onClick={() => setOpen(expanded ? null : id)} aria-expanded={expanded} aria-controls={formId} data-testid={`preset-connect-${p.id}-${id}`}>
                      {expanded ? "Zwiń" : "Połącz"}
                    </Button>
                    {!expanded && <Link to={keyDocHref(id)} className="font-medium text-accent hover:underline">Jak zdobyć klucz</Link>}
                  </span>
                )}
              </div>
              <p className="text-xs break-words text-muted" data-testid={`preset-msg-${p.id}-${id}`}>{r ? joinSteps(r) : hint(c, st)}</p>
              {c && expanded && (
                <div id={formId} className="min-w-0 rounded-lg border border-line bg-raised p-2.5">
                  <KeyForm connector={c} offline={offline} idPrefix={`preset-${p.id}-${id}`} onSaved={onSaved(id)} autoFocus />
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function Presets({ presets, connectors, offline }: { presets: Preset[]; connectors: Connector[]; offline: boolean }) {
  if (!presets.length) return null;
  const byId = new Map(connectors.map((c) => [c.id, c]));
  return (
    <section aria-labelledby="presets-h" className="space-y-2">
      <h3 id="presets-h" className="flex items-center gap-1.5 text-sm font-semibold text-fg">
        Pakiety jednym kliknięciem
        <InfoTip content="Włącza naraz zestaw konektorów do typowego zastosowania. Brakujące klucze uzupełnisz bezpośrednio w karcie pakietu („Połącz” przy konektorze); po zapisaniu ostatniego klucza pakiet zastosuje się sam. Klucze trafiają tylko do lokalnego pliku 0600." />
      </h3>
      <div className="grid min-w-0 gap-x-3 gap-y-0 md:grid-cols-2 2xl:grid-cols-4">
        {presets.map((p) => <PresetCard key={p.id} p={p} byId={byId} offline={offline} />)}
      </div>
    </section>
  );
}
