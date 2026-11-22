import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import type { AutoOverview, CampaignLinks, LinkedAd } from "../../api/autoSearch";
import { metaAnalysisEntry } from "../../lib/activity";
import { CampaignAutoLinks } from "../../components/watch/meta/AutoLinks";
import { WatchPage } from "./WatchPage";

const linked = (over: Partial<LinkedAd> = {}): LinkedAd => ({
  analysis_id: 5, mode: "synthetic", mode_label_pl: "DANE SYNTETYCZNE", ad_id: "syn-auto-1", revision: 1, current_revision: true,
  incident_id: "metaad-syn-syn-auto-1-abcd1234", base_run_id: "run-1", campaign_id: "C-0001", campaign_label: "pko-bonus",
  page_id: "100000000000101", page_name: "Okazje Inwestycyjne Demo (SYNTETYCZNE)", first_observed_at: "2026-11-22T05:00:00Z",
  last_observed_at: "2026-11-22T05:01:00Z", analysed_at: "2026-11-22T05:01:10Z", triage_priority: "medium", triage_priority_pl: "średni",
  review_status: "unreviewed", total_bits: 14.2, probability: 0.9, evidence: [{ kind: "landing_host", value: "pko-bonus24.com", bits: 9.1 }],
  reason_pl: null, library_link: null, ...over,
});

const overview = (): AutoOverview => ({
  mode: "synthetic", mode_label_pl: "DANE SYNTETYCZNE",
  rules: { keywords_sent: ["zarabiaj"], page_ids_sent: [], countries: ["PL"], ad_active_status: "ACTIVE", languages: [], date_window_days: 7, overlap_days: 2,
    date_from: "2026-11-01", date_to: null, platforms: ["FACEBOOK"], local_keywords_default: 40, local_keywords: ["lokata życia"], celebrities: [],
    page_name_patterns: ["inwest"], same_text_min_pages: 3, alert_min_priority: "medium", expansion: { enabled: false } },
  sources: [{ id: "meta-adlibrary", label_pl: "Biblioteka reklam Meta (Ad Library API)", enabled: true, state: "replay", interval_s: 1800, next_due: null, last_poll: null, last_ok: null, total_polls: 2, total_items: 7, last_error: null }],
  cycles: [{ mode: "synthetic", trigger: "replay", trigger_pl: "odtworzenie", started_at: "2026-11-22T05:01:00Z", finished_at: "2026-11-22T05:01:01Z", status: "ok", message_pl: "", requests: 0, pages: 1, records: 4, ads_seen: 4, new_ads: 1, changed_ads: 1, unchanged_ads: 2, ended_ads: 1, queued_analyses: 2 }],
  last_update: "2026-11-22T05:01:01Z",
  totals: { cycles_recorded: 2, unique_ads: 4, records_fetched: 7, new_revisions: 1, analyses_done: 0, analyses_partial: 5, assigned: 1, new_patterns: 3, abstained: 1, alerts_open: 3, analyst_confirmed: 0, analyst_dismissed: 0, linked_campaigns: 1 },
  queue: { queued: 0, running: 0, done: 0, partial: 5, failed: 0, deferred: 0 },
  triage: { high: 1, medium: 2, low: 1, unknown: 1 },
  linked_ads: [linked()],
  watch_matches: { counts: { total: 3, to_campaigns: 2, new: 3, new_to_campaigns: 2 }, latest: [{ match_id: 1, source: "cert-pl", source_pl: "CERT Polska: lista ostrzeżeń", indicator_kind: "host", indicator_display: "pko-bonus24[.]com", campaign_id: "C-0001", campaign_label: "pko-bonus", run_id: "run-1", strength: "strong", bits: 3.2, status: "new", replay: true, matched_at: "2026-11-22T05:01:00Z", item_first_seen: null, item_defanged: "pko-bonus24[.]com" }] },
  collector: { state: "replay", state_pl: "Odtworzenie", message_pl: null, enabled: true, engine_state: "replay", next_due: null, last_poll: null, last_success: null, last_error_pl: null, interval_s: 1800 },
  budget: { limit_per_h: 60, used_60m: 0, remaining: 60, label_pl: "Własny limit HYDRY", deferred_until: null, last_usage: null },
  offline: true, token_configured: false, engine: { state: "replay", replay: true },
});

describe("WatchAuto (Automatyczne wyszukiwanie, BONUS-AUTO)", () => {
  it("shows the loop, rules mapped to the task phrases, cycles and ads linked to known campaigns from the API", async () => {
    const calls: string[] = [];
    mockFetch(async (input, init) => {
      const url = String(input);
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url.includes("/v1/watch/rt/meta/auto")) return jsonResponse(overview());
      if (url.includes("/v1/watch/rt/watchlist/rebuild")) return jsonResponse({ added: 0, removed: 0, total: 0 });
      if (url.includes("/v1/watch/rt/replay")) return jsonResponse({ state: "replay" });
      return jsonResponse({ detail: "not found" }, { status: 404 });
    });
    renderWithProviders(<WatchPage />, { route: "/watch?view=auto&mode=synthetic" });
    expect(await screen.findByRole("tab", { name: "Automatyczne wyszukiwanie" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("meta-mode-banner")).toHaveTextContent("DANE SYNTETYCZNE");
    const rules = await screen.findByTestId("auto-rules");
    expect(within(rules).getAllByText("słowa kluczowe").length).toBeGreaterThan(0);
    expect(within(rules).getAllByText("data wystąpienia").length).toBe(2);
    expect(within(rules).getByText(/2026-11-01/)).toBeInTheDocument();
    expect(within(rules).getByText("FACEBOOK")).toBeInTheDocument();
    const row = within(screen.getByTestId("auto-linked")).getByTestId("linked-ad-row");
    expect(row).toHaveAttribute("data-ad-id", "syn-auto-1");
    expect(within(row).getByTestId("linked-ad-incident")).toHaveAttribute("href", "/incident/metaad-syn-syn-auto-1-abcd1234");
    expect(within(row).getByText("C-0001")).toBeInTheDocument();
    expect(screen.getByTestId("cycle-new")).toHaveTextContent("1");
    expect(screen.getByTestId("cycle-changed")).toHaveTextContent("1");
    expect(screen.getByTestId("watch-links")).toHaveTextContent("pko-bonus24[.]com");
    fireEvent.click(screen.getByTestId("auto-demo"));
    await waitFor(() => expect(calls.some((c) => c.startsWith("POST") && c.includes("/v1/watch/rt/replay"))).toBe(true));
  });

  it("campaign card section lists the linked ad and the WATCH-RT match", async () => {
    const links: CampaignLinks = { campaign_id: "C-0001", run_id: "run-1", meta_ads: [linked()], watch_matches: overview().watch_matches.latest, note_pl: "Kandydaci do sprawdzenia." };
    const seen: string[] = [];
    mockFetch(async (input) => { seen.push(String(input)); return jsonResponse(links); });
    renderWithProviders(<CampaignAutoLinks campaignId="C-0001" runId="run-1" />);
    expect(await screen.findByText(/Nowe reklamy z biblioteki Meta powiązane z tą kampanią \(1\)/)).toBeInTheDocument();
    expect(within(screen.getByTestId("campaign-meta-links")).getByTestId("linked-ad-row")).toHaveAttribute("data-ad-id", "syn-auto-1");
    expect(screen.getByTestId("campaign-watch-links")).toHaveTextContent("CERT Polska");
    expect(seen[0]).toContain("/v1/watch/rt/meta/campaigns/C-0001/links?run=run-1");
  });

  it("top-bar entry only for an assignment to a known campaign", () => {
    expect(metaAnalysisEntry({ decision: "new_campaign", ad_id: "1" })).toBeNull();
    const e = metaAnalysisEntry({ decision: "assign", campaign_id: "C-0001", ad_id: "syn-auto-1", mode: "synthetic", page_name: "Demo" });
    expect(e?.title).toBe("DANE SYNTETYCZNE · Nowa reklama Meta powiązana z kampanią C-0001");
    expect(e?.to).toBe("/campaigns/C-0001");
  });
});
