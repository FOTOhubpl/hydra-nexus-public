import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { HowItWorksPage } from "./HowItWorksPage";

const flow = {
  run_id: "analyse-x", created_at: "2026-11-21T17:00:00Z", revision_of: null, timings_s: { genes: 1.5, self_assignment: 0.4 },
  boxes: {
    input: { incidents: 10, by_corpus: { nask: 10 } },
    adapters: { artifacts: 40, by_role: { creative: 10 } },
    genes: {
      by_family: { VIS: 5 }, total: 5, extractor_seconds: {}, extractor_original_seconds: { vis: 12.5 },
      extractors: { vis: { run_seconds: 0, original_seconds: 12.5, incidents_run: 0, incidents_cached: 10, original_unknown: 0, observed: 30, unavailable: 2, failed: 1 } },
      extractors_cached: 10, extractors_run: 0,
    },
    enrichment: { records_by_source: {}, genes_with_enrichment: 0 },
    rarity: { distinct_values_by_level: { campaign: 3 } },
    candidates: { by_index: {}, candidate_pairs: 12, all_pairs: 45 },
    scoring: { pairs_scored: 12, hypotheses: { independent: 12 }, bits_by_level: {}, scorer: "rarity_bits@1" },
    clustering: { campaigns: 7, multi_member: 2, groups: 1 },
    assignment: {
      by_decision: { assign: 5, new_campaign: 5 }, conformal: true, self: 10, new_material: 0, hint_pl: null,
      summary: { mode: "leave_one_out", assignments: 10, by_decision: { assign: 5, new_campaign: 5 }, agreement_with_clustering: 1, abstain_rate: 0, member_return_rate: 1, singleton_new_rate: 1, held_out_campaign_rejection_rate: 1 },
    },
    reviews: { reviews: 0, undos: 0, revisions: 0, constraints_in_run: 0, hint_pl: "Brak recenzji: zaakceptuj" },
    outputs: { llm_enabled: false, exports: { total: 3, by_kind: { campaign_json: 2, stix_run: 1 }, last_at: null }, hint_pl: null },
  },
};

class RO { observe() {} unobserve() {} disconnect() {} }
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver ??= RO;

describe("HowItWorksPage", () => {
  it("shows real per-stage numbers: self-assignment, cached extractor times, exports, review hint", async () => {
    mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/v1/system/flow")) return jsonResponse(flow);
      return jsonResponse({ items: [] });
    });
    renderWithProviders(<HowItWorksPage />);
    expect(await screen.findByText("Przypisanie (zbiór otwarty)")).toBeInTheDocument();
    expect(screen.getByText("zgodne z grupowaniem")).toBeInTheDocument();
    expect(screen.getByText(/leave-one-out: 10/)).toBeInTheDocument();
    expect(screen.getByText("30/2/1")).toBeInTheDocument();
    expect(screen.getByText("eksportów przebiegu")).toBeInTheDocument();
    expect(screen.getByText("campaign_json")).toBeInTheDocument();
    expect(screen.getByText(/Brak recenzji/)).toBeInTheDocument();
  });

  it("stacks the flow in one column below md (phone: labels no longer overlap)", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduced-motion"), media: q, addEventListener() {}, removeEventListener() {} }));
    mockFetch(async (input) => (String(input).includes("/v1/system/flow") ? jsonResponse(flow) : jsonResponse({ items: [] })));
    renderWithProviders(<HowItWorksPage />);
    const col = await screen.findByTestId("flow-column");
    expect(col.tagName).toBe("OL");
    expect(col.querySelectorAll(":scope > li")).toHaveLength(12);
    // no absolutely positioned boxes in the phone layout
    expect(col.querySelector(".absolute")).toBeNull();
    expect(screen.getByText("Przypisanie (zbiór otwarty)")).toBeInTheDocument();
  });

  it("keeps the 4x3 diagram from md up", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("min-width"), media: q, addEventListener() {}, removeEventListener() {} }));
    mockFetch(async (input) => (String(input).includes("/v1/system/flow") ? jsonResponse(flow) : jsonResponse({ items: [] })));
    renderWithProviders(<HowItWorksPage />);
    expect(await screen.findByText("Przypisanie (zbiór otwarty)")).toBeInTheDocument();
    expect(screen.queryByTestId("flow-column")).toBeNull();
  });

  it("explainer: stepper focuses a stage, family chips show a definition, decisions use the shared pills", async () => {
    mockFetch(async (input) => (String(input).includes("/v1/system/flow") ? jsonResponse(flow) : jsonResponse({ items: [] })));
    renderWithProviders(<HowItWorksPage />);
    expect(await screen.findByText("Przypisanie (zbiór otwarty)")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Od zgłoszonej reklamy do pakietu dowodowego" })).toBeInTheDocument();
    const steps = within(screen.getByRole("list", { name: "Etapy przepływu" })).getAllByRole("button");
    expect(steps).toHaveLength(7);
    expect(steps[0]).toHaveAttribute("aria-current", "step");
    fireEvent.click(steps[5]!);
    expect(steps[5]).toHaveAttribute("aria-current", "step");
    expect(document.getElementById("how-stage-assign")).toHaveAttribute("data-active", "true");
    fireEvent.keyDown(steps[5]!, { key: "ArrowRight" });
    expect(steps[6]).toHaveAttribute("aria-current", "step");
    // live numbers only from the API fixture: 12 scored pairs, 7 campaigns
    expect(screen.getByText("12 ocenionych par")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^NAR/ }));
    expect(within(screen.getByTestId("how-family-detail")).getByText(/0 bitów do kampanii/)).toBeInTheDocument();
    expect(screen.getAllByText("wstrzymano (niejednoznaczne)").length).toBeGreaterThan(0);
  });

  it("explainer renders static definitions without a run", async () => {
    mockFetch(async (input) => (String(input).includes("/v1/system/flow") ? jsonResponse({ run_id: null, boxes: null }) : jsonResponse({ items: [] })));
    renderWithProviders(<HowItWorksPage />);
    expect(await screen.findByText("Brak przebiegu analizy")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sześć rodzin genów" })).toBeInTheDocument();
    expect(screen.getAllByText("bez przebiegu: same definicje")).toHaveLength(7);
  });
});
