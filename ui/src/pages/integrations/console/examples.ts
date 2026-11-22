/**
 * Curated example requests ("Przykłady") for the API console. Each one names an endpoint by method + path; the
 * console resolves it against the live /openapi.json and greys it out when this build does not expose it
 * (e.g. a router that is not merged yet). Values are placeholders the analyst edits; nothing here is data.
 */
export interface ConsoleExample {
  id: string;
  title: string;
  description: string;
  method: "get" | "post" | "put" | "patch" | "delete";
  path: string;
  /** Form values keyed `path:<name>` / `query:<name>`. */
  values?: Record<string, string>;
  body?: unknown;
}

export const CONSOLE_EXAMPLES: ConsoleExample[] = [
  {
    id: "health",
    title: "Stan serwera",
    description: "Czy lokalne API żyje: wersja, liczba artefaktów, urządzenie.",
    method: "get",
    path: "/health",
  },
  {
    id: "connectors",
    title: "Lista konektorów",
    description: "Wszystkie integracje z poziomem (live / format eksportu / planowane), statusem i danymi wychodzącymi.",
    method: "get",
    path: "/v1/integrations",
  },
  {
    id: "misp-preview",
    title: "Podgląd paczki MISP (na sucho)",
    description: "Buduje zdarzenie MISP z bieżącego przebiegu i pokazuje, co zostałoby wysłane. Nic nie wychodzi z komputera.",
    method: "post",
    path: "/v1/integrations/{connector_id}/preview",
    values: { "path:connector_id": "misp" },
    body: { format: "misp", limit: 50 },
  },
  {
    id: "webhook-test",
    title: "Testowy webhook (ping)",
    description: "Wysyła zdarzenie „ping” do skonfigurowanego odbiorcy. Wpisz identyfikator odbiorcy z zakładki Webhooki.",
    method: "post",
    path: "/v1/integrations/webhooks/{endpoint_id}/test",
  },
  {
    id: "webhooks-list",
    title: "Odbiorcy webhooków i katalog zdarzeń",
    description: "Skonfigurowani odbiorcy, typy zdarzeń z przykładami i schemat podpisu HMAC.",
    method: "get",
    path: "/v1/integrations/webhooks",
  },
  {
    id: "system-flow",
    title: "Przepływ systemu",
    description: "Etapy potoku z licznikami (import → geny → kandydaci → hipotezy → kampanie).",
    method: "get",
    path: "/v1/system/flow",
  },
  {
    id: "campaigns",
    title: "Lista kampanii",
    description: "Kampanie bieżącego przebiegu z członkami i decyzjami.",
    method: "get",
    path: "/v1/campaigns",
  },
  {
    id: "status",
    title: "Status instancji",
    description: "Tryb offline, wzbogacanie, przebiegi, łańcuch audytu, aktywne zadania.",
    method: "get",
    path: "/v1/status",
  },
  {
    id: "audit-verify",
    title: "Weryfikacja łańcucha audytu",
    description: "Przelicza skróty łańcucha zdarzeń audytu i zgłasza pierwsze zerwanie.",
    method: "get",
    path: "/v1/audit/verify",
  },
  {
    id: "signing-key",
    title: "Klucz podpisu eksportów",
    description: "Odcisk klucza Ed25519 tej instalacji. Weryfikację pakietu ZIP wykonasz poleceniem hydra verify-export lub POST /v1/exports/verify (surowy ZIP w treści).",
    method: "get",
    path: "/v1/exports/signing-key",
  },
  {
    id: "watchlist",
    title: "Lista obserwowanych",
    description: "Słowa kluczowe i domeny obserwowane przez harmonogram.",
    method: "get",
    path: "/v1/watchlist",
  },
];
