/** Contract-shaped fixtures for the integrations unit tests (no real API data). */
import type { Connector, IntegrationsIndex, WebhooksIndex } from "../../../api/integrations";

export function connector(over: Partial<Connector> & { id: string }): Connector {
  return {
    name: over.id, category: "enrichment", direction: "both", auth: "api_key", level: "live", formats: [],
    description_pl: "Opis.", setup_pl: ["Krok 1"], notes_pl: "", docs_url: "https://example.org/docs", licence_pl: "ToS",
    rate_limit_pl: "4/min", rate_limit_per_min: 4, data_out: ["domain"], data_out_pl: "Domena.", never_sent_pl: "Artefakty.",
    secrets: [], status: "not_configured", enabled: false, can_connect: true, can_test: true, can_run: true, can_export: false,
    last_test: null, last_run: null, usage: { requests_60m: 3, limit_per_min: 4 }, ...over,
  };
}

export function index(over: Partial<IntegrationsIndex> = {}): IntegrationsIndex {
  return {
    offline: true, enrich: false, generated_at: "2026-11-22T02:00:00Z",
    categories: [
      { id: "enrichment", label_pl: "Wzbogacanie", description_pl: "Wzbogacanie wskaźników." },
      { id: "outbound_sharing", label_pl: "Udostępnianie", description_pl: "Eksport." },
    ],
    levels: [],
    presets: [{ id: "cert-misp", name_pl: "Pakiet CERT/MISP", description_pl: "MISP i STIX.", connectors: ["misp"] }],
    connectors: [
      connector({ id: "virustotal", name: "VirusTotal", secrets: [{ name: "VT_API_KEY", configured: false, source: null, last4: null }] }),
      connector({ id: "misp", name: "MISP", category: "outbound_sharing", level: "export-format", direction: "outbound", formats: ["misp"], status: "configured", can_export: true, can_connect: false, can_test: false, secrets: [], usage: null }),
      connector({ id: "threatfox", name: "ThreatFox", level: "planned", status: "planned", can_connect: false, can_test: false, can_run: false, usage: null }),
    ],
    ...over,
  };
}

export function webhooks(over: Partial<WebhooksIndex> = {}): WebhooksIndex {
  return {
    delivery_possible: false, offline: true,
    event_types: [
      { id: "ping", label_pl: "Test", description_pl: "Zdarzenie testowe." },
      { id: "delta", label_pl: "Zmiana", description_pl: "Nowa kampania lub przypisanie." },
    ],
    endpoints: [], signature: { header: "X-Hydra-Signature", algo: "HMAC-SHA256", scheme_pl: "Podpis HMAC." },
    ...over,
  };
}
