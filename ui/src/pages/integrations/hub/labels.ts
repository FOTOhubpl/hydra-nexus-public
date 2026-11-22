/** Polish labels and definitions for the integrations UI (contract: CONTRACT.md "Status pill labels"). */
import type { BadgeVariant } from "../../../components/Badge";
import type { ConnectorLevel, ConnectorStatus, Delivery } from "../../../api/integrations";

/** Required honesty note for every "live" connector (jury): shown as a visible note and in the level tooltip. */
export const LIVE_NOTE = "zaimplementowany; testowany na fiksturach z dokumentacji, bez wywołań produkcyjnych";

export const LEVEL_LABEL: Record<ConnectorLevel, string> = {
  live: "Na żywo (live)",
  "export-format": "Format eksportu",
  planned: "Planowane",
};

export const LEVEL_VARIANT: Record<ConnectorLevel, BadgeVariant> = { live: "info", "export-format": "success", planned: "neutral" };

export const LEVEL_TIP: Record<ConnectorLevel, string> = {
  live: `Klient ${LIVE_NOTE}: odpowiedzi testowe są przygotowane ręcznie z publicznej dokumentacji dostawcy, nie nagrane z prawdziwych wywołań. Łączy się z siecią tylko po włączeniu, z kluczem i przy HYDRA_OFFLINE=0.`,
  "export-format": "HYDRA tworzy plik w formacie importu dostawcy (pobranie lub podgląd na sucho). Działa offline, nic nie jest wysyłane.",
  planned: "Tylko opis integracji. Nie ma klienta, więc nie da się jej połączyć, przetestować ani uruchomić.",
};

export const STATUS_LABEL: Record<ConnectorStatus, string> = {
  configured: "Skonfigurowane",
  not_configured: "Nieskonfigurowane",
  offline: "Offline",
  disabled: "Wyłączone",
  planned: "Planowane",
};

export const STATUS_VARIANT: Record<ConnectorStatus, BadgeVariant> = {
  configured: "success",
  not_configured: "neutral",
  offline: "warning",
  disabled: "neutral",
  planned: "neutral",
};

export const STATUS_TIP: Record<ConnectorStatus, string> = {
  configured: "Wymagane klucze są zapisane (albo nie są potrzebne) i konektor jest gotowy do użycia.",
  not_configured: "Brakuje wymaganego klucza. Użyj „Połącz”, aby go zapisać.",
  offline: "Konektor jest skonfigurowany, ale HYDRA działa w trybie offline (HYDRA_OFFLINE=1), więc połączenia sieciowe są zablokowane.",
  disabled: "Konektor jest wyłączony; nie wykona żadnego zapytania.",
  planned: "Integracja planowana: brak implementacji.",
};

export const DIRECTION_LABEL: Record<string, { arrow: string; label: string; tip: string }> = {
  inbound: { arrow: "←", label: "przychodzące", tip: "Dane przychodzą do HYDRA (wzbogacanie, wyszukiwanie)." },
  outbound: { arrow: "→", label: "wychodzące", tip: "HYDRA wysyła lub eksportuje wskaźniki do odbiorcy." },
  both: { arrow: "⇄", label: "w obie strony", tip: "Zapytania wychodzą, odpowiedzi wracają do HYDRA." },
};

export const AUTH_LABEL: Record<string, string> = {
  none: "brak (publiczne API)",
  api_key: "klucz API",
  oauth_token: "token OAuth",
  basic: "login i hasło (Basic)",
  hmac: "podpis HMAC",
};

export const CATEGORY_FALLBACK: Record<string, string> = {
  enrichment: "Wzbogacanie",
  threat_intel: "Threat intelligence",
  ad_transparency: "Przejrzystość reklam",
  outbound_sharing: "Udostępnianie wskaźników",
  database_sink: "Bazy danych i SIEM",
  notification: "Powiadomienia",
};

export const DELIVERY_LABEL: Record<Delivery["status"], string> = {
  delivered: "Dostarczono",
  retrying: "Ponawianie",
  dead: "Martwa kolejka",
  blocked_offline: "Zablokowano offline",
  pending: "Oczekuje",
};

export const DELIVERY_VARIANT: Record<Delivery["status"], BadgeVariant> = {
  delivered: "success",
  retrying: "warning",
  dead: "danger",
  blocked_offline: "warning",
  pending: "info",
};

export const DELIVERY_TIP: Record<Delivery["status"], string> = {
  delivered: "Odbiorca odpowiedział kodem 2xx.",
  retrying: "Błąd sieci, 429 lub 5xx: kolejna próba z wykładniczym opóźnieniem.",
  dead: "Wyczerpano próby albo odbiorca odrzucił żądanie (4xx). „Ponów” wstawia je ponownie do kolejki.",
  blocked_offline: "Adres spoza tego komputera, a HYDRA działa offline lub endpoint jest wyłączony. Nic nie zostało wysłane.",
  pending: "Czeka na pierwszą próbę doręczenia.",
};

export const STEP_LABEL: Record<string, string> = {
  secret: "Zapis klucza",
  test: "Test połączenia",
  enable: "Włączenie",
  export: "Eksport",
};

/** Per-connector key guidance for the inline key form: what the key unlocks, where to get it, plan/cost note.
 * Facts follow docs/INTEGRATIONS.md (provider pages checked 2026-11-22); limits come from the API (rate_limit_pl). */
export interface KeyInfo { unlocks: string; where: string; plan: string; docAnchor?: string }

export const KEY_INFO: Record<string, KeyInfo> = {
  "meta-adlibrary": { unlocks: "wyszukiwanie reklam w Meta Ad Library (słowa kluczowe, strony, kraj PL)", where: "token użytkownika z Graph API Explorer po weryfikacji tożsamości w Meta", plan: "bezpłatnie; obowiązują warunki Meta Platform", docAnchor: "doc-meta-token" },
  urlscan: { unlocks: "wyszukiwanie publicznych skanów domen w urlscan.io", where: "konto urlscan.io → ustawienia → klucz API", plan: "plan darmowy z limitami, plany płatne" },
  virustotal: { unlocks: "reputację domen, URL-i, adresów IP i hashy w VirusTotal", where: "konto VirusTotal → profil → klucz API", plan: "publiczne API bezpłatne: 4 zapytania/min, 500/dzień, tylko niekomercyjnie" },
  urlhaus: { unlocks: "sprawdzanie hostów i URL-i w URLhaus (abuse.ch)", where: "konto abuse.ch → Auth-Key (wymagany od 2025-06-30)", plan: "bezpłatnie" },
  "google-safe-browsing": { unlocks: "sprawdzanie URL-i na listach Google Safe Browsing", where: "Google Cloud Console → włącz Safe Browsing API → klucz API", plan: "bezpłatnie, tylko niekomercyjnie (komercyjnie: Web Risk)" },
  abuseipdb: { unlocks: "reputację adresów IP w AbuseIPDB", where: "konto AbuseIPDB → API → klucz", plan: "plan darmowy: 1000 sprawdzeń/dzień" },
  misp: { unlocks: "wysyłkę zdarzeń MISP do Twojej instancji (plik działa bez klucza)", where: "Twoja instancja MISP → klucz uwierzytelniania użytkownika (Auth key)", plan: "własna instancja" },
  elasticsearch: { unlocks: "wysyłkę wskaźników przez _bulk do Twojego klastra (plik działa bez klucza)", where: "Kibana → Stack Management → API keys", plan: "własny klaster" },
  "splunk-hec": { unlocks: "wysyłkę zdarzeń do Splunk HTTP Event Collector (plik działa bez klucza)", where: "Splunk → Settings → Data inputs → HTTP Event Collector → nowy token", plan: "własna instancja" },
  slack: { unlocks: "krótkie powiadomienia na kanał Slack (plik działa bez klucza)", where: "Slack API → Twoja aplikacja → Incoming Webhooks → adres webhooka", plan: "bezpłatnie" },
};

export const DEFAULT_KEY_INFO: KeyInfo = { unlocks: "zapytania do tego dostawcy", where: "konto u dostawcy (link poniżej)", plan: "zobacz warunki dostawcy" };

/** Secret names that are plain addresses of the user's own instance (shown as text, still never echoed back). */
export const isAddressSecret = (name: string) => /_URL$/.test(name) && name !== "SLACK_WEBHOOK_URL";

/** In-app docs deep link for a connector's key guide (DocsTab reads ?doc=<element id>). */
export const keyDocHref = (connectorId: string) => `/integrations?tab=docs&doc=${encodeURIComponent(KEY_INFO[connectorId]?.docAnchor ?? `conn-${connectorId}`)}`;

/** Connector readiness used by presets and cards; every state has a text label and a glyph (not colour only). */
export type Readiness = "ready" | "needs_key" | "offline" | "error" | "planned";
export const READINESS: Record<Readiness, { label: string; glyph: string; tone: string }> = {
  ready: { label: "Gotowe", glyph: "✓", tone: "border-ok/40 bg-ok/10 text-ok" },
  needs_key: { label: "Wymaga klucza", glyph: "!", tone: "border-warn/40 bg-warn/10 text-warn" },
  error: { label: "Błąd testu", glyph: "✕", tone: "border-err/40 bg-err/10 text-err" },
  offline: { label: "Offline", glyph: "⏸", tone: "border-line-strong bg-hover text-muted" },
  planned: { label: "Planowany (pominięty)", glyph: "–", tone: "border-line bg-raised text-subtle" },
};

export function readinessOf(c: { level: string; secrets: { configured: boolean }[]; status: string } | undefined): Readiness {
  if (!c || c.level === "planned") return "planned";
  // export-format connectors work without keys (file export); their keys only unlock the optional push
  if (c.level !== "export-format" && c.secrets.some((s) => !s.configured)) return "needs_key";
  if (c.status === "offline") return "offline";
  return "ready";
}

/** Optional push keys of an export-format connector that are not stored yet. */
export const optionalKeysMissing = (c: { level: string; secrets: { configured: boolean }[] } | undefined) =>
  Boolean(c && c.level === "export-format" && c.secrets.some((s) => !s.configured));

/** Polish plural for "wymaga/wymagają klucza". */
export const needsKeyPl = (n: number) => {
  const few = n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14);
  return `${n} ${few ? "wymagają" : "wymaga"} klucza`;
};

/** Polish date + time, or an em dash for unknown. */
export function formatWhen(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString("pl-PL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
