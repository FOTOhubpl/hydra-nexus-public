import { useState } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { taskApi, taskKeys, type Campaign } from "../../../api/task";
import { exportUrl, setEnabled, useIntegrationMutation, type Connector } from "../../../api/integrations";
import { extractErrorMessage } from "../../../api/client";
import { authHeaders, notifyAuthStatus } from "../../../api/auth";
import { Button } from "../../../components/Button";
import { Icon } from "../../../components/Icon";
import { Tooltip } from "../../../components/Tooltip";
import { cx } from "../../../lib/cx";
import { useSelectedRun } from "../../../lib/runSelection";
import { DIRECTION_LABEL, formatWhen, LEVEL_LABEL, LEVEL_TIP, LIVE_NOTE, LEVEL_VARIANT, STATUS_LABEL, STATUS_TIP, STATUS_VARIANT } from "./labels";
import { KeyForm } from "./KeyForm";
import { Switch, Term, TipPill } from "./ui";

/** Export formats the server builds per campaign only (hydra/integrations/exports.py: XARF needs campaign_id). */
export const CAMPAIGN_FORMATS = new Set(["xarf"]);

export function needsCampaign(format: string | undefined): boolean {
  return format !== undefined && CAMPAIGN_FORMATS.has(format);
}

/** Campaigns largest first (the picker's default is the largest campaign). */
export function campaignsBySize(items: Campaign[]): Campaign[] {
  return [...items].sort((a, b) => (b.size ?? b.members.length) - (a.size ?? a.members.length) || a.id.localeCompare(b.id));
}

/** Download the provider-format file in one click (fetch → blob, so an API error is shown instead of saved). */
export function useExportDownload(connector: Connector) {
  const run = useSelectedRun();
  const [state, setState] = useState<{ busy: boolean; error: string | null; done: string | null }>({ busy: false, error: null, done: null });
  const format = connector.formats[0];
  const perCampaign = needsCampaign(format);
  const cq = useQuery({ queryKey: taskKeys.campaigns, queryFn: ({ signal }) => taskApi.campaigns(signal), enabled: perCampaign, retry: false });
  const campaigns = perCampaign ? campaignsBySize(cq.data?.items ?? []) : [];
  const [picked, setPicked] = useState<string | null>(null);
  const campaignId = perCampaign ? (picked && campaigns.some((c) => c.id === picked) ? picked : (campaigns[0]?.id ?? null)) : null;
  const download = async () => {
    if (perCampaign && !campaignId) {
      setState({ busy: false, error: "Ten format wymaga wyboru kampanii, a bieżący przebieg nie ma kampanii.", done: null });
      return;
    }
    setState({ busy: true, error: null, done: null });
    try {
      const r = await fetch(exportUrl(connector.id, format, run, campaignId), { credentials: "same-origin", headers: authHeaders("GET") });
      if (!r.ok) {
        notifyAuthStatus(`/v1/integrations/${connector.id}/export`, r.status);
        const text = await r.text();
        let body: unknown = text;
        try { body = JSON.parse(text); } catch { /* text body */ }
        throw new Error(extractErrorMessage(body, `HTTP ${r.status}`));
      }
      const blob = await r.blob();
      const cd = r.headers.get("content-disposition") ?? "";
      const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
      const name = m ? decodeURIComponent(m[1]!) : `hydra-${connector.id}.${format ?? "dat"}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setState({ busy: false, error: null, done: name });
    } catch (err) {
      setState({ busy: false, error: err instanceof Error ? err.message : String(err), done: null });
    }
  };
  return { ...state, download, format, perCampaign, campaigns, campaignId, setCampaignId: setPicked, campaignsLoading: perCampaign && cq.isPending };
}

/** Campaign picker for per-campaign formats (B5, audit f450a70). */
export function CampaignPicker({ exp, idPrefix }: { exp: ReturnType<typeof useExportDownload>; idPrefix: string }) {
  if (!exp.perCampaign) return null;
  const id = `${idPrefix}-campaign`;
  return (
    <div className="flex min-w-0 flex-col gap-1 text-xs">
      <label htmlFor={id} className="text-muted">Kampania (format wymaga wyboru kampanii)</label>
      <select
        id={id}
        data-testid={id}
        value={exp.campaignId ?? ""}
        disabled={exp.campaignsLoading || exp.campaigns.length === 0}
        onChange={(e) => exp.setCampaignId(e.target.value)}
        className="h-7 min-w-0 rounded-[4px] border border-line bg-raised px-2 text-sm focus:border-accent focus:outline-none"
      >
        {exp.campaignsLoading && <option value="">wczytywanie kampanii…</option>}
        {!exp.campaignsLoading && exp.campaigns.length === 0 && <option value="">brak kampanii w bieżącym przebiegu</option>}
        {exp.campaigns.map((c) => (
          <option key={c.id} value={c.id}>{c.id} · {c.label} ({c.size ?? c.members.length})</option>
        ))}
      </select>
    </div>
  );
}

function UsageBar({ c }: { c: Connector }) {
  const u = c.usage;
  const perMin = u?.limit_per_min ?? c.rate_limit_per_min;
  if (!u && !perMin) return null;
  const used = u?.requests_60m ?? 0;
  const cap = perMin ? perMin * 60 : null;
  const pct = cap ? Math.min(100, (used / cap) * 100) : 0;
  return (
    <div className="min-w-0 space-y-1">
      <div className="flex items-center justify-between gap-2">
        <Term tip="Zapytania tego konektora w ostatnich 60 minutach względem limitu dostawcy (limit na minutę × 60). HYDRA sama wstrzymuje zapytania ponad limit.">Wykorzystanie limitu</Term>
        <span className="truncate font-mono text-2xs text-muted">{used}{cap ? ` / ${Math.round(cap)}` : ""} w 60 min</span>
      </div>
      <div
        role="meter"
        aria-label={`Wykorzystanie limitu ${c.name}`}
        aria-valuemin={0}
        aria-valuemax={cap ?? 0}
        aria-valuenow={used}
        className="h-1.5 overflow-hidden rounded-full bg-hover"
      >
        <div className={cx("h-full rounded-full", pct > 80 ? "bg-warn" : "bg-accent")} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export function ConnectorCard({ c, categoryLabel, onOpen, offline }: { c: Connector; categoryLabel: string; onOpen: () => void; offline: boolean }) {
  const [keysOpen, setKeysOpen] = useState(false);
  const needsKey = c.secrets.some((s) => !s.configured);
  const toggle = useIntegrationMutation((enabled: boolean) => setEnabled(c.id, enabled));
  const exp = useExportDownload(c);
  const dir = DIRECTION_LABEL[c.direction] ?? DIRECTION_LABEL.both!;
  const planned = c.level === "planned";
  const test = c.last_test;
  const enabled = toggle.isPending && toggle.variables !== undefined ? toggle.variables : c.enabled;

  return (
    <article className="surface-card surface-hover flex min-w-0 flex-col gap-3 p-4" data-testid={`connector-${c.id}`} data-level={c.level} data-status={c.status} aria-labelledby={`conn-${c.id}-name`}>
      <div className="flex min-w-0 items-start gap-2">
        <div className="min-w-0 flex-1">
          <h4 id={`conn-${c.id}-name`} className="truncate text-sm font-semibold text-fg">
            <button type="button" onClick={onOpen} className="max-w-full truncate text-left hover:underline" data-testid={`open-${c.id}`}>{c.name}</button>
          </h4>
          <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-2 text-xs text-muted">
            <span className="truncate">{categoryLabel}</span>
            <Tooltip content={dir.tip} focusable>
              <span className="inline-flex items-center gap-1 whitespace-nowrap"><span aria-hidden="true" className="font-mono">{dir.arrow}</span>{dir.label}</span>
            </Tooltip>
          </div>
        </div>
        <Tooltip content={planned ? "Konektora planowanego nie da się włączyć." : c.enabled ? "Wyłącz konektor (żadnych zapytań)." : "Włącz konektor. Połączenia sieciowe nadal wymagają klucza i trybu online."}>
          <Switch
            checked={enabled}
            disabled={planned || toggle.isPending}
            busy={toggle.isPending}
            onChange={(v) => toggle.mutate(v)}
            label={`${c.enabled ? "Wyłącz" : "Włącz"} ${c.name}`}
            testId={`toggle-${c.id}`}
          />
        </Tooltip>
      </div>

      <div className="flex min-w-0 flex-wrap gap-1.5">
        <TipPill variant={LEVEL_VARIANT[c.level]} tip={LEVEL_TIP[c.level]} testId={`level-${c.id}`}>{LEVEL_LABEL[c.level]}</TipPill>
        {!planned && <TipPill variant={STATUS_VARIANT[c.status]} tip={STATUS_TIP[c.status]} testId={`status-${c.id}`}>{STATUS_LABEL[c.status]}</TipPill>}
        {!planned && exp.perCampaign && <TipPill variant="warning" tip="Ten format powstaje dla jednej kampanii: wybierz ją poniżej przed pobraniem." testId={`needs-campaign-${c.id}`}>Wymaga wyboru kampanii</TipPill>}
      </div>

      {c.level === "live" && <p className="text-[11px] leading-snug text-subtle" data-testid={`live-note-${c.id}`}>Na żywo: {LIVE_NOTE}.</p>}
      <p className="line-clamp-2 text-xs text-muted">{c.description_pl}</p>

      {!planned && (
        <dl className="grid min-w-0 grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
          <div className="min-w-0">
            <dt><Term tip="Wynik ostatniego jawnego testu połączenia („Testuj połączenie”). Testy nigdy nie uruchamiają się same.">Ostatni test</Term></dt>
            <dd className={cx("truncate", test ? (test.ok ? "text-ok" : "text-err") : "text-subtle")}>
              {test ? `${test.ok ? "✓ ok" : `✕ ${test.status ?? "błąd"}`} · ${formatWhen(test.at)}` : "nie testowano"}
            </dd>
          </div>
          <div className="min-w-0">
            <dt><Term tip="Ostatnie uruchomienie: zapytanie, wysyłka lub eksport pliku.">Ostatnia synchronizacja</Term></dt>
            <dd className="truncate text-muted">{c.last_run ? `${c.last_run.ok === false ? "✕ " : ""}${formatWhen(c.last_run.at)}` : "—"}</dd>
          </div>
        </dl>
      )}
      {c.level === "live" && <UsageBar c={c} />}

      {c.level === "export-format" && <CampaignPicker exp={exp} idPrefix={`card-${c.id}`} />}
      <div className="mt-auto flex min-w-0 flex-wrap items-center gap-2 pt-1">
        {c.level === "live" && c.can_connect && (
          <Button variant="primary" onClick={() => setKeysOpen((v) => !v)} aria-expanded={keysOpen} aria-controls={`card-${c.id}-keys`} data-testid={`connect-${c.id}`}>
            <Icon name="chain" size={14} /> {keysOpen ? "Zwiń" : "Połącz"}
          </Button>
        )}
        {c.level === "live" && !c.can_connect && c.auth === "hmac" && (
          <Link to="/integrations?tab=webhooks" className="inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-raised px-3 text-sm font-medium text-fg hover:bg-hover">Skonfiguruj w zakładce Webhooki</Link>
        )}
        {c.level === "export-format" && (
          <Tooltip content={exp.perCampaign ? `Pobiera plik ${exp.format ?? ""} dla wybranej kampanii. Nic nie jest wysyłane.` : `Pobiera plik ${exp.format ?? ""} z bieżącego przebiegu. Nic nie jest wysyłane.`}>
            <Button variant="primary" onClick={exp.download} disabled={exp.busy || !c.can_export || (exp.perCampaign && !exp.campaignId)} data-testid={`export-${c.id}`}>
              <Icon name="download" size={14} /> {exp.busy ? "Pobieranie…" : "Pobierz pakiet"}
            </Button>
          </Tooltip>
        )}
        {planned ? (
          <button type="button" onClick={onOpen} className="text-xs font-medium text-accent hover:underline" data-testid={`planned-desc-${c.id}`}>Opis planowanej integracji</button>
        ) : (
          <button type="button" onClick={onOpen} className="text-xs font-medium text-accent hover:underline">Szczegóły</button>
        )}
      </div>
      {c.level === "export-format" && c.can_connect && c.secrets.length > 0 && !keysOpen && (
        <button type="button" onClick={() => setKeysOpen(true)} aria-expanded={false} aria-controls={`card-${c.id}-keys`} className="self-start text-xs font-medium text-accent hover:underline" data-testid={`keys-${c.id}`}>
          {needsKey ? "Wysyłka wymaga klucza API: uzupełnij" : "Klucze wysyłki"}
        </button>
      )}
      {keysOpen && (
        <div id={`card-${c.id}-keys`} className="min-w-0 rounded-lg border border-line bg-raised p-2.5">
          <KeyForm connector={c} offline={offline} idPrefix={`card-${c.id}`} autoFocus />
        </div>
      )}
      {exp.error && <div role="alert" className="text-xs break-words text-err">{exp.error}</div>}
      {exp.done && <div role="status" className="truncate text-xs text-ok">Pobrano {exp.done}</div>}
      {toggle.isError && <div role="alert" className="text-xs text-err">{(toggle.error as Error).message}</div>}
    </article>
  );
}
