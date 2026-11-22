/**
 * "Integracje & API" (route /integrations, `?tab=hub|console|webhooks|docs`): one page, four tabs.
 * The tablist follows the WAI-ARIA tabs pattern: roving tabindex, Left/Right/Home/End move and activate,
 * aria-selected + aria-controls, the tab state lives in the URL so links and reloads keep it.
 */
import { useEffect, useRef, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router";
import { useIntegrations } from "../../api/integrations";
import { cx } from "../../lib/cx";
import { ConsoleTab } from "./ConsoleTab";
import { DocsTab } from "./DocsTab";
import { HubTab } from "./HubTab";
import { WebhooksTab } from "./WebhooksTab";

export const TABS = [
  { id: "hub", label: "Centrum integracji" },
  { id: "console", label: "Konsola API" },
  { id: "webhooks", label: "Webhooki" },
  { id: "docs", label: "Dokumentacja" },
] as const;

export type TabId = (typeof TABS)[number]["id"];

export const OFFLINE_BANNER = "Tryb offline: połączenia sieciowe zablokowane (HYDRA_OFFLINE=1). Eksporty plików działają.";

function isTab(v: string | null): v is TabId {
  return TABS.some((t) => t.id === v);
}

export function IntegrationsPage() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: TabId = isTab(raw) ? raw : "hub";
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const index = useIntegrations();
  const pendingFocus = useRef<TabId | null>(null);
  useEffect(() => {
    if (pendingFocus.current === tab) refs.current[tab]?.focus();
    pendingFocus.current = null;
  }, [tab]);

  const select = (id: TabId, focus = false) => {
    const next = new URLSearchParams(params);
    if (id === "hub") next.delete("tab");
    else next.set("tab", id);
    if (focus) {
      pendingFocus.current = id;
      refs.current[id]?.focus();
    }
    setParams(next, { replace: true });
  };

  const onKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = TABS.findIndex((t) => `integ-tab-${t.id}` === e.currentTarget.id);
    let j: number | null = null;
    if (e.key === "ArrowRight") j = (i + 1) % TABS.length;
    else if (e.key === "ArrowLeft") j = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = TABS.length - 1;
    if (j === null) return;
    e.preventDefault();
    select(TABS[j]!.id, true);
  };

  return (
    <div className="flex min-w-0 flex-col gap-4 p-4" data-testid="integrations-page">
      <div className="min-w-0 space-y-1">
        <h2 className="text-xl font-semibold text-fg">Integracje &amp; API</h2>
        <p className="max-w-3xl text-sm text-muted">
          Połączenia z zewnętrznymi źródłami i odbiorcami, konsola lokalnego API, webhooki i dokumentacja. Wszystko jest
          wyłączone domyślnie i działa offline; żadne dane nie wychodzą z komputera bez jawnej zgody (włączenie konektora,
          klucz i tryb sieciowy).
        </p>
      </div>

      {index.data?.offline && (
        <div role="status" data-testid="offline-banner" className="flex min-w-0 items-start gap-2 rounded-lg border border-warn/35 bg-warn/10 px-3 py-2 text-sm text-warn">
          <span aria-hidden="true" className="font-mono">⏻</span>
          <span className="min-w-0">{OFFLINE_BANNER}</span>
        </div>
      )}

      <div role="tablist" aria-label="Sekcje integracji" className="flex min-w-0 flex-wrap gap-1 border-b border-line">
        {TABS.map((t) => {
          const active = t.id === tab;
          return (
            <button
              key={t.id}
              ref={(el) => { refs.current[t.id] = el; }}
              type="button"
              role="tab"
              id={`integ-tab-${t.id}`}
              aria-selected={active}
              aria-controls={`integ-panel-${t.id}`}
              tabIndex={active ? 0 : -1}
              onClick={() => select(t.id)}
              onKeyDown={onKey}
              className={cx(
                "-mb-px rounded-t-md border-b-2 px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
                active ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg",
              )}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      <div role="tabpanel" id={`integ-panel-${tab}`} aria-labelledby={`integ-tab-${tab}`} tabIndex={0} className="min-w-0 focus-visible:outline-none">
        {tab === "hub" && <HubTab />}
        {tab === "console" && <ConsoleTab />}
        {tab === "webhooks" && <WebhooksTab />}
        {tab === "docs" && <DocsTab />}
      </div>
    </div>
  );
}
