import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { defang, type AdDetail, type AdRow, type AdScheme, type Analysis, type MetaConfig, type MetaMode, type MetaStatus } from "../../api/metaAds";
import { WatchPage } from "./WatchPage";

const config = (over: Partial<MetaConfig> = {}): MetaConfig => ({
  countries: ["PL"], queries: [], ad_active_status: "ALL", languages: [], date_window_days: 7, overlap_days: 2, budget_per_h: 60,
  max_active_queries: 10, max_pages_per_query: 2, interval_s: 1800, max_analyses_per_h: 30, max_media_bytes: 25_000_000,
  media_fetch: false, landing_fetch: false, expansion: { enabled: false, max_depth: 1, max_pages: 5 }, config_version: 1, ...over,
});

const counters = {
  records_fetched: 0, unique_ads: 0, new_revisions: 0, assets_acquired: 0, analyses_done: 0, analyses_partial: 0, assigned: 0,
  new_patterns: 0, abstained: 0, alerts_open: 0, analyst_confirmed: 0, analyst_dismissed: 0,
};

const status = (mode: MetaMode = "live", over: Partial<MetaStatus["collector"]> = {}, extra: Partial<MetaStatus> = {}): MetaStatus => ({
  mode, mode_label_pl: mode === "live" ? "NA ŻYWO" : mode === "recorded" ? "ODTWORZENIE" : "DANE SYNTETYCZNE", offline: true, media_fetch_enabled: false,
  collector: { state: "disabled", state_pl: "Wyłączony", message_pl: null, enabled: false, engine_state: "stopped", next_due: null, last_poll: null, last_success: null, last_error_pl: null, interval_s: 1800, ...over },
  token: { configured: true, source: "file", last4: "WXYZ" },
  budget: { limit_per_h: 60, used_60m: 0, remaining: 60, label_pl: "Własny limit HYDRY (propozycja pilotażowa), nie limit Meta", deferred_until: null, last_usage: null },
  queue: { queued: 0, running: 0, done: 0, partial: 0, failed: 0, deferred: 0 },
  counters, contract: { graph_api_version: "v24.0", verified_on: "2026-11-22", last_diagnostics: null }, config: config(), ...extra,
});

const adRow = (over: Partial<AdRow> = {}): AdRow => ({
  ad_id: "900000000000001", mode: "synthetic", mode_label_pl: "DANE SYNTETYCZNE", page_id: "100000000000001", page_name: "Inwestycje Demo",
  first_observed_at: "2026-11-22T03:00:00Z", last_observed_at: "2026-11-22T03:30:00Z", seen_count: 2, revisions: 1,
  current_revision: { id: 11, rev: 1, content_sha256: "abcdef0123456789abcdef", observed_at: "2026-11-22T03:00:00Z", collection_status: "metadata_available", collection_status_pl: "dostępne metadane" },
  text_excerpt: "Zarabiaj z nami 5000 zł dziennie", link_hosts: ["promo-demo[.]example"], queries: ["zysk"],
  analysis: { id: 7, status: "partial", status_pl: "częściowa", decision: "new_campaign", decision_pl: "Nowy wzorzec", campaign_id: null, triage_priority: "medium", triage_priority_pl: "średni", review_status: "unreviewed", coverage_pl: "tekst i metadane; brak obrazu i wideo" },
  alert: true, ...over,
});

const analysis = (over: Partial<Analysis> = {}): Analysis => ({
  id: 7, ad_id: "900000000000001", mode: "synthetic", mode_label_pl: "DANE SYNTETYCZNE", revision_id: 11, incident_id: "metaad-syn-1", base_run_id: "run-1",
  context_version: "ctx-1", status: "partial", status_pl: "częściowa", reason_pl: "Brak materiału wideo.", coverage_pl: "tekst i metadane; brak obrazu i wideo", report_id: "r-7",
  correlation: { decision: "new_campaign", decision_pl: "Nowy wzorzec", campaign_id: null, group_id: null, reason_pl: "Brak kampanii z wystarczającym poparciem.", candidates: [{ campaign_id: "C-3", probability: null, support: 0.2, total_bits: 3.5 }], evidence: [{ kind: "landing_host", value: "promo-demo.example", bits: 3.5, level: "campaign", family: "INF" }], base_run_id: "run-1", assign_run_id: "assign-9", note_pl: "Bity nie są prawdopodobieństwem." },
  triage: { review_status: "unreviewed", triage_priority: "medium", triage_priority_pl: "średni", reasons: [{ code: "profit_promise", description_pl: "Obietnica zysku", artifact: null, source: "tekst reklamy", rule_version: "1" }], note_pl: "Przesłanki nie są potwierdzeniem oszustwa." },
  manipulation: [
    { modality: "image", status: "not_applicable", status_pl: "brak obrazu", method: "none", version: "-", scope: "-", quality_and_coverage: "-", evidence_refs: [], limitations_pl: "Brak materiału obrazowego." },
    { modality: "video", status: "unavailable", status_pl: "niedostępne", method: "none", version: "-", scope: "-", quality_and_coverage: "-", evidence_refs: [], limitations_pl: "Brak wiarygodnego detektora; brak sygnału nie oznacza autentyczności." },
    { modality: "audio", status: "not_applicable", status_pl: "brak dźwięku", method: "none", version: "-", scope: "-", quality_and_coverage: "-", evidence_refs: [], limitations_pl: "Brak ścieżki dźwiękowej." },
  ],
  review: { status: "unreviewed", history: [] }, ...over,
});

const detail = (): AdDetail => ({
  ad: adRow(), source_link: "https://www.facebook.com/ads/library/?id=900000000000001",
  revisions: [{ id: 11, rev: 1, observed_at: "2026-11-22T03:00:00Z", content_sha256: "abcdef0123456789abcdef", content: {}, collection_status: "metadata_available", collection_status_pl: "dostępne metadane", collection_reason_pl: "Pobieranie materiałów wyłączone.", artifacts: [{ role: "ad_creative", status: "asset_unavailable", status_pl: "materiał niedostępny", sha256: null, mime: null, size: null, source: "ad_snapshot", derived: false, fetched_at: null, retry_possible: true }] }],
  queries: [{ query_id: "q1", label: "zysk", first_at: "2026-11-22T03:00:00Z", last_at: "2026-11-22T03:30:00Z", hits: 2 }],
  analyses: [adRow().analysis!],
});

const scheme = (): AdScheme => ({
  mode: "synthetic", mode_label_pl: "DANE SYNTETYCZNE", ad_id: "900000000000001", decision: "assign", campaign_id: "C-1",
  versions: [{ from_rev: 1, to_rev: 2, observed_at: "2026-11-22T03:30:00Z", text_changed: true, domain_change: { removed: ["zysk-a.example"], added: ["zysk-b.example"] }, media_changed: false, kept: { phones: ["+48512000111"] } }],
  same_scheme: [{ ad_id: "900000000000002", decision: "assign", campaign_id: "C-1", shared: { hosts: ["zysk-b.example"], phones: ["+48512000111"], media_sha256: ["0123456789abcdef0123"] }, analysis_id: 8, evidence: [{ kind: "landing_host", value: "zysk-b.example", bits: 6.5, level: "campaign", family: "INF" }] }],
  lookalike_not_merged: [{ ad_id: "900000000000003", decision: "new_campaign", campaign_id: null, shared: { media_sha256: ["fedcba9876543210fedc"] }, why_not_merged_pl: "Wspólne jest tylko zdjęcie stockowe; samo zdjęcie nie dowodzi wspólnego operatora.", analysis_id: 9 }],
  note_pl: "Powiązania są hipotezami do weryfikacji, nie potwierdzeniem operatora.",
});

interface Opts { status?: MetaStatus; ads?: AdRow[]; analysis?: Partial<Analysis> }

function setup(opts: Opts = {}) {
  const calls: { url: string; method: string; body?: unknown }[] = [];
  let an = analysis(opts.analysis);
  mockFetch(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ url, method, body });
    const path = url.replace(/^\/api/, "").split("?")[0]!;
    const mode = (new URL(url, "http://x").searchParams.get("mode") ?? "live") as MetaMode;
    if (path === "/v1/watch/rt/meta/status") return jsonResponse(opts.status ?? status(mode));
    if (path === "/v1/watch/rt/meta/config" && method === "PUT") return jsonResponse(config({ ...(body as Partial<MetaConfig>), config_version: 2 }));
    if (path === "/v1/watch/rt/meta/config") return jsonResponse(config());
    if (path === "/v1/watch/rt/meta/ads") return jsonResponse({ mode, ads: opts.ads ?? [], next_before: null });
    if (path === "/v1/watch/rt/meta/ads/900000000000001/scheme") return jsonResponse(scheme());
    if (path === "/v1/watch/rt/meta/ads/900000000000001") return jsonResponse(detail());
    if (path === "/v1/watch/rt/meta/analyses/7/review") { an = analysis({ triage: { ...an.triage, review_status: (body as { decision: string }).decision }, review: { status: "x", history: [{ decision: (body as { decision: string }).decision, analyst: "admin", note: null, created_at: "2026-11-22T04:00:00Z" }] } }); return jsonResponse(an); }
    if (path === "/v1/watch/rt/meta/analyses/7") return jsonResponse(an);
    if (path === "/v1/watch/rt/meta/collector") return jsonResponse(status("live", { state: "running", state_pl: "Działa", enabled: true }));
    if (path === "/v1/watch/rt/meta/diagnose") {
      return jsonResponse({ ok: false, at: "2026-11-22T04:00:00Z", graph_api_version: "v24.0", requests: 2, optional_fields: { eu_total_reach: "unknown" }, layers: [
        { id: "token", label_pl: "Token przyjęty", status: "pass", message_pl: "Token przyjęty." },
        { id: "required_fields", label_pl: "Pełny zestaw pól importera", status: "fail", message_pl: "Pole odrzucone przez API." },
      ] }, {});
    }
    return jsonResponse({ detail: "not found" }, { status: 404 });
  });
  return calls;
}

describe("WatchMeta (Reklamy Meta tab)", () => {
  it("empty state: tab selected, live badge and banner, separate counter tiles, empty list hint", async () => {
    setup();
    renderWithProviders(<WatchPage />, { route: "/watch?view=meta" });
    expect(await screen.findByRole("tab", { name: "Reklamy Meta" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("meta-mode-banner")).toHaveTextContent("NA ŻYWO");
    expect(await screen.findByTestId("meta-empty")).toHaveTextContent("Brak reklam w tym trybie");
    const tiles = await screen.findByTestId("meta-counters");
    expect(within(tiles).getAllByTestId(/^counter-/)).toHaveLength(11);
    expect(within(tiles).getByText("Rekordy pobrane")).toBeInTheDocument();
    expect(within(tiles).getByText("Potwierdzenia analityka")).toBeInTheDocument();
    expect(screen.getByTestId("budget-label")).toHaveTextContent("nie limit Meta");
    expect(screen.getByTestId("token-state")).toHaveTextContent("…WXYZ");
  });

  it("missing configuration: no token and needs_config state with link to Integrations", async () => {
    setup({ status: status("live", { state: "needs_config", state_pl: "Wymaga konfiguracji", message_pl: "Brak tokenu META_AD_LIBRARY_TOKEN." }, { token: { configured: false, source: null, last4: null } }) });
    renderWithProviders(<WatchPage />, { route: "/watch?view=meta" });
    expect(await screen.findByTestId("collector-state")).toHaveAttribute("data-state", "needs_config");
    expect(screen.getByTestId("collector-message")).toHaveTextContent("META_AD_LIBRARY_TOKEN");
    expect(screen.getByTestId("token-state")).toHaveTextContent("brak tokenu");
    expect(screen.getByRole("link", { name: /Integracje/ })).toHaveAttribute("href", "/integrations");
  });

  it("running collector deferred by the own budget, diagnostics shown per layer, start goes to the API", async () => {
    const calls = setup({ status: status("live", { state: "deferred_budget", state_pl: "Wstrzymano do wznowienia (budżet)", enabled: true, engine_state: "live" }, { budget: { limit_per_h: 60, used_60m: 60, remaining: 0, label_pl: "Własny limit HYDRY", deferred_until: "2026-11-22T05:00:00Z", last_usage: { "x-app-usage": { call_count: 12 } } } }) });
    renderWithProviders(<WatchPage />, { route: "/watch?view=meta" });
    expect(await screen.findByTestId("collector-state")).toHaveAttribute("data-state", "deferred_budget");
    expect(screen.getByTestId("budget")).toHaveTextContent("60 / 60");
    expect(screen.getByText("Wstrzymano do")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("diagnose"));
    const diag = await screen.findByTestId("meta-diagnostics");
    expect(within(diag).getByText("import niesprawny")).toBeInTheDocument();
    expect(screen.getByTestId("diag-required_fields")).toHaveAttribute("data-status", "fail");
    fireEvent.click(screen.getByTestId("poll-now"));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/meta/collector") && (c.body as { action: string }).action === "poll_now")).toBe(true));
  });

  it("synthetic mode: banner always visible, ad card badge, partial analysis with three separate sections and report links, review", async () => {
    const calls = setup({ ads: [adRow()] });
    renderWithProviders(<WatchPage />, { route: "/watch?view=meta&mode=synthetic" });
    expect(await screen.findByTestId("meta-mode-banner")).toHaveTextContent("DANE SYNTETYCZNE — wyniki z zestawu testowego, nie z Meta Ad Library");
    const row = await screen.findByTestId("meta-ad-row");
    expect(within(row).getByTestId("mode-badge")).toHaveTextContent("DANE SYNTETYCZNE");
    expect(within(row).getByText("alert do sprawdzenia")).toBeInTheDocument();
    expect(within(row).getByTestId("ad-coverage")).toHaveTextContent("pokrycie: tekst i metadane");
    fireEvent.click(row);
    const det = await screen.findByTestId("meta-ad-detail");
    expect(within(det).getByRole("link", { name: "Otwórz w bibliotece reklam Meta" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(det).getByTestId("meta-artifact")).toHaveAttribute("data-status", "asset_unavailable");
    const an = await screen.findByTestId("meta-analysis");
    expect(an).toHaveAttribute("data-status", "partial");
    expect(within(an).getByTestId("analysis-correlation")).toHaveTextContent("Nowy wzorzec");
    expect(within(an).getByTestId("analysis-triage")).toHaveTextContent("Obietnica zysku");
    expect(within(an).getByTestId("manip-video")).toHaveTextContent("brak sygnału nie oznacza autentyczności");
    expect(within(an).getByTestId("report-json")).toHaveAttribute("href", "/api/v1/watch/rt/meta/analyses/7/report?format=json");
    expect(within(an).getByTestId("report-md")).toHaveAttribute("href", "/api/v1/watch/rt/meta/analyses/7/report?format=md");
    expect(screen.getByTestId("period-report-json")).toHaveAttribute("href", "/api/v1/watch/rt/meta/report?mode=synthetic&format=json");
    // the banner stays after interaction
    expect(screen.getByTestId("meta-mode-banner")).toHaveAttribute("data-mode", "synthetic");
    fireEvent.click(within(an).getByRole("button", { name: "Potwierdź podejrzenie" }));
    await waitFor(() => expect(within(screen.getByTestId("meta-analysis")).getByTestId("review-status")).toHaveTextContent("potwierdzone przez analityka"));
    expect(calls.some((c) => c.url.includes("/analyses/7/review") && (c.body as { decision: string }).decision === "confirmed_by_analyst")).toBe(true);
    // ads of synthetic mode are requested with mode=synthetic, never mixed with live
    expect(calls.filter((c) => c.url.includes("/meta/ads?")).every((c) => c.url.includes("mode=synthetic"))).toBe(true);
  });

  it("switching the mode updates the badge and refetches that mode only", async () => {
    const calls = setup();
    renderWithProviders(<WatchPage />, { route: "/watch?view=meta" });
    await screen.findByTestId("meta-empty");
    fireEvent.click(screen.getByTestId("meta-mode-recorded"));
    await waitFor(() => expect(screen.getByTestId("meta-mode-banner")).toHaveAttribute("data-mode", "recorded"));
    expect(screen.getByTestId("meta-mode-banner")).toHaveTextContent("ODTWORZENIE");
    await waitFor(() => expect(calls.some((c) => c.url.includes("/meta/status?mode=recorded"))).toBe(true));
  });

  it("config form saves queries and keeps media fetch opt-in off by default", async () => {
    const calls = setup();
    renderWithProviders(<WatchPage />, { route: "/watch?view=meta" });
    const form = await screen.findByTestId("meta-config");
    expect(within(form).getByTestId("media-fetch")).not.toBeChecked();
    fireEvent.click(within(form).getByRole("button", { name: "Dodaj zapytanie" }));
    fireEvent.change(within(form).getByLabelText("Wartość"), { target: { value: "inwestycja" } });
    fireEvent.click(within(form).getByTestId("meta-config-save"));
    await waitFor(() => expect(within(form).getByText(/wersja 2/)).toBeInTheDocument());
    const put = calls.find((c) => c.method === "PUT");
    expect((put!.body as MetaConfig).queries).toEqual([{ kind: "keyword", value: "inwestycja", label: null, enabled: true }]);
    expect((put!.body as MetaConfig).languages).toEqual([]);
  });

  it("P1: scheme panel shows versions with the domain change, same-scheme ads and look-alikes not merged, defanged hosts", async () => {
    setup({ ads: [adRow()] });
    renderWithProviders(<WatchPage />, { route: "/watch?view=meta&mode=synthetic" });
    fireEvent.click(await screen.findByTestId("meta-ad-row"));
    const panel = await screen.findByTestId("meta-scheme");
    expect(within(panel).getByTestId("mode-badge")).toHaveTextContent("DANE SYNTETYCZNE");
    expect(within(panel).getByTestId("scheme-note")).toHaveTextContent("hipotezami do weryfikacji");
    const change = within(panel).getByTestId("domain-change");
    expect(change).toHaveTextContent("zysk-a[.]example");
    expect(change).toHaveTextContent("zysk-b[.]example");
    expect(within(panel).getByTestId("scheme-versions")).toHaveTextContent("zachowane telefony: +48512000111");
    const same = within(panel).getByTestId("scheme-same");
    expect(same).toHaveTextContent("900000000000002");
    expect(same).toHaveTextContent("hosty: zysk-b[.]example");
    expect(same).toHaveTextContent("0123456789ab");
    const look = within(panel).getByTestId("scheme-lookalike");
    expect(look).toHaveTextContent("900000000000003");
    expect(look).toHaveTextContent("samo zdjęcie nie dowodzi wspólnego operatora");
    expect(panel.querySelectorAll("a")).toHaveLength(0);
    expect(defang("a.b.example")).toBe("a[.]b[.]example");
    expect(defang("a[.]b")).toBe("a[.]b");
  });

  it("P1: correlation section lists missing-observation tasks with the heuristic-order note", async () => {
    const base = analysis();
    setup({ ads: [adRow()], analysis: { correlation: { ...base.correlation, evidence_tasks: [
      { kind: "landing_page", adapter: "landing_fetch", task_pl: "Pobierz stronę docelową reklamy." },
      { kind: "ad_video", adapter: null, task_pl: "Dołącz plik wideo reklamy ręcznie." },
    ] } } });
    renderWithProviders(<WatchPage />, { route: "/watch?view=meta&mode=synthetic" });
    fireEvent.click(await screen.findByTestId("meta-ad-row"));
    const tasks = await screen.findByTestId("evidence-tasks");
    expect(tasks).toHaveTextContent("Brakujące obserwacje (zadania dla analityka)");
    expect(tasks).toHaveTextContent("Pobierz stronę docelową reklamy.");
    expect(tasks).toHaveTextContent("adapter: landing_fetch");
    expect(tasks).toHaveTextContent("brak adaptera: zadanie dla analityka");
    expect(tasks).toHaveTextContent("Kolejność heurystyczna, bez zmierzonej przewagi.");
  });

  it("no evidence tasks: the block is absent", async () => {
    setup({ ads: [adRow()] });
    renderWithProviders(<WatchPage />, { route: "/watch?view=meta&mode=synthetic" });
    fireEvent.click(await screen.findByTestId("meta-ad-row"));
    await screen.findByTestId("meta-analysis");
    expect(screen.queryByTestId("evidence-tasks")).toBeNull();
  });
});
