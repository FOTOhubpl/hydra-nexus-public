import { useQueryClient } from "@tanstack/react-query";
import { API_BASE, IMPORT_FIELD, queryKeys, useHealth } from "../api/client";
import { ApiErrorState } from "../components/ApiErrorState";
import { Button } from "../components/Button";
import { Panel } from "../components/Panel";
import { ReferenceGalleryPanel } from "../components/ReferenceGalleryPanel";
import { clearSessionJobs, useSessionJobs } from "../lib/sessionJobs";
import { DEFAULT_SETTINGS, SETTINGS_LIMITS, settingsStore, updateSettings, useSettings, type UiSettings } from "../lib/settings";

type NumericKey = keyof typeof SETTINGS_LIMITS;

// B17 (audit f450a70): help texts written for an analyst; the API detail lives under "Szczegóły techniczne".
const NUMERIC_FIELDS: Array<{ key: NumericKey; label: string; unit: string; help: string }> = [
  { key: "healthIntervalMs", label: "Odświeżanie stanu", unit: "ms", help: "Jak często pasek stanu sprawdza, czy serwer HYDRA działa. Mniejsza wartość to szybsza informacja o awarii." },
  { key: "jobPollMs", label: "Odświeżanie zadań", unit: "ms", help: "Jak często odświeżany jest postęp importu i analizy, które właśnie trwają." },
  { key: "pageSize", label: "Rozmiar strony", unit: "wierszy", help: "Ile plików pokazuje jedna strona listy materiałów w „Import danych”." },
  { key: "layoutIterations", label: "Iteracje układu", unit: "iter.", help: "Dokładność rozmieszczenia grafu powiązań. Więcej iteracji daje czytelniejszy układ, ale graf wczytuje się dłużej." },
];

const ENDPOINTS: Array<[string, string]> = [
  ["GET", "/health"],
  ["POST", `/v1/import  (multipart, pole "${IMPORT_FIELD}", odpowiedź 202 + {job_id})`],
  ["GET", "/v1/jobs/{id}  (queued | running | done | failed)"],
  ["GET", "/v1/artifacts?limit&offset  ({items, total})"],
  ["GET", "/v1/artifacts/{id}  (konwencja: szczegóły artefaktu)"],
  ["GET", "/v1/artifacts/{id}/content  (konwencja: podgląd obrazu)"],
  ["GET", "/v1/graph  ({nodes, edges})"],
];

function NumberField({ field, settings }: { field: (typeof NUMERIC_FIELDS)[number]; settings: UiSettings }) {
  const limits = SETTINGS_LIMITS[field.key];
  return (
    <label className="grid grid-cols-[160px_140px_minmax(0,1fr)] items-center gap-3 border-b border-line/70 py-2 last:border-b-0">
      <span className="text-sm text-fg">{field.label}</span>
      <span className="flex items-center gap-1.5">
        <input
          type="number"
          min={limits.min}
          max={limits.max}
          value={settings[field.key]}
          onChange={(event) => updateSettings({ [field.key]: Number(event.target.value) } as Partial<UiSettings>)}
          className="h-7 w-24 rounded-[4px] border border-line bg-canvas px-2 font-mono text-xs text-fg focus:border-accent focus:outline-none"
        />
        <span className="text-xs text-subtle">{field.unit}</span>
      </span>
      <span className="text-xs text-muted">
        {field.help} <span className="text-subtle">Domyślnie: {DEFAULT_SETTINGS[field.key]}.</span>
      </span>
    </label>
  );
}

export function SettingsPage() {
  const settings = useSettings();
  const jobs = useSessionJobs();
  const { healthIntervalMs } = settings;
  const health = useHealth(healthIntervalMs);
  const client = useQueryClient();

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-3 p-4">
      <Panel title="Serwer">
        <div className="flex flex-col gap-3 text-sm">
          <div className="flex flex-wrap items-center gap-3" data-testid="server-summary">
            <span className={health.isError ? "text-err" : health.data ? "text-ok" : "text-muted"}>
              {health.isError ? "Serwer HYDRA nie odpowiada" : health.data ? `Serwer HYDRA działa${health.data.version ? ` (wersja ${String(health.data.version)})` : ""}` : "Sprawdzanie stanu serwera…"}
            </span>
            <Button size="sm" onClick={() => void client.invalidateQueries({ queryKey: queryKeys.health })}>
              Sprawdź teraz
            </Button>
          </div>
          <p className="text-xs text-muted">
            HYDRA działa w całości na tym komputerze: przeglądarka łączy się tylko z lokalnym serwerem, a materiały dowodowe nie są
            wysyłane do internetu. Zapytania do zewnętrznych usług (np. rejestrów domen) wykonują wyłącznie konektory włączone jawnie w „Integracje & API”.
          </p>
          {health.isError && <ApiErrorState error={health.error} resource="stan serwera" compact />}
        </div>
        <details className="mt-3 rounded-md border border-line p-3" data-testid="tech-details">
          <summary className="cursor-pointer text-xs font-medium text-muted">Szczegóły techniczne</summary>
        <div className="mt-3 grid gap-4 lg:grid-cols-2">
          <div className="flex flex-col gap-2 text-xs">
            <div className="text-muted">
              Przeglądarka wysyła żądania do <code className="font-mono text-fg">{API_BASE}/*</code> w tej samej domenie. Serwer Vite w trybie developerskim lub podglądu
              przekazuje je do <code className="font-mono text-fg">http://127.0.0.1:8000</code>, usuwając prefiks <code className="font-mono">/api</code>{" "}
              (adres można zmienić przez <code className="font-mono">HYDRA_UI_BACKEND</code>).
            </div>
            <ul className="space-y-1 font-mono text-xs">
              {ENDPOINTS.map(([method, path]) => (
                <li key={path} className="flex gap-2">
                  <span className="w-10 shrink-0 text-subtle">{method}</span>
                  <span className="text-fg">{path}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="flex min-w-0 flex-col gap-2">
            <span className="text-xs text-muted">Ostatnia odpowiedź /health</span>
            <pre className="m-0 max-h-56 overflow-auto rounded-md border border-line bg-canvas p-3 font-mono text-xs text-fg">
              {health.isPending ? "…" : health.isError ? "brak odpowiedzi" : JSON.stringify(health.data, null, 2)}
            </pre>
          </div>
        </div>
        </details>
      </Panel>

      <Panel title="Baza referencyjna">
        <ReferenceGalleryPanel />
      </Panel>

      <Panel title="Interfejs">
        <div className="flex flex-col">
          {NUMERIC_FIELDS.map((field) => (
            <NumberField key={field.key} field={field} settings={settings} />
          ))}
          <label className="grid grid-cols-[160px_140px_minmax(0,1fr)] items-center gap-3 py-2">
            <span className="text-sm text-fg">Etykiety grafu</span>
            <input
              type="checkbox"
              className="accent-accent justify-self-start"
              checked={settings.graphLabels}
              onChange={(event) => updateSettings({ graphLabels: event.target.checked })}
            />
            <span className="text-xs text-muted">Pokazuj nazwy wszystkich incydentów i kampanii na grafie, także przy dużym zagęszczeniu.</span>
          </label>
        </div>
      </Panel>

      <Panel title="Dane lokalne przeglądarki">
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
          <span>
            Ustawienia widoku i lista ostatnich zadań importu są zapamiętane tylko w tej przeglądarce. Wyniki analizy, decyzje i dziennik audytu przechowuje serwer HYDRA, nie przeglądarka.
          </span>
          <div className="ml-auto flex gap-2">
            <Button size="sm" variant="secondary" disabled={jobs.length === 0} onClick={clearSessionJobs}>
              Usuń z listy: {jobs.length}
            </Button>
            <Button size="sm" variant="danger" onClick={() => settingsStore.reset()}>
              Przywróć ustawienia
            </Button>
          </div>
        </div>
      </Panel>
    </div>
  );
}
