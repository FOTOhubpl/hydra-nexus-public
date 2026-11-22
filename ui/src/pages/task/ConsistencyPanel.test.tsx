import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CampaignConsistency } from "../../api/task";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { ConsistencyPanel } from "./ConsistencyPanel";

// Hand-written synthetic fixture (never organiser data).
const REPORT: CampaignConsistency = {
  campaign_id: "syn-c1", run_id: "syn-run-1", method: "consistency_gate@1", applicable: true, verdict: "gate_would_split",
  verdict_pl: "bramka spójności rozdzieliłaby kampanię na 2 części", size: 3, gate_enabled: false,
  link_threshold_bits: 10, low_threshold_bits: 4, strong_bits: 20, cohesion: 0.41, weakest_member: "syn-3",
  weakest_link_bits: 12, median_scored_bits: 12, median_ok: true, scored_pairs: 3, possible_pairs: 3, links: 2,
  single_pair_members: ["syn-3"], strong_single_members: [], failing_members: ["syn-3"], contradiction_pairs: [],
  gate_parts: [["syn-1", "syn-2"], ["syn-3"]],
  members: [
    { id: "syn-3", supporting_pairs: 1, scored_pairs: 2, possible_pairs: 2, median_bits: 6, median_ok: true, status: "single_pair",
      contradictions: [], best_pair: { with: "syn-2", bits: 12, reasons_pl: ["wspólny piksel"] } },
    { id: "syn-1", supporting_pairs: 2, scored_pairs: 2, possible_pairs: 2, median_bits: 13, median_ok: true, status: "ok", contradictions: [] },
  ],
};

afterEach(() => vi.unstubAllGlobals());

describe("ConsistencyPanel", () => {
  it("renders verdict, weakest link and members held by a single pair", async () => {
    const fetch = mockFetch(async () => jsonResponse(REPORT));
    renderWithProviders(<ConsistencyPanel campaignId="syn-c1" runId="syn-run-1" />);
    expect(await screen.findByText("Bramka spójności (M6)")).toBeInTheDocument();
    expect(String(fetch.mock.calls[0]![0])).toContain("/v1/campaigns/syn-c1/consistency?run_id=syn-run-1");
    expect(screen.getByText("bramka rozdzieliłaby")).toBeInTheDocument();
    expect(screen.getByText("trzyma go jedna para")).toBeInTheDocument();
    expect(screen.getByText(/wspólny piksel/)).toBeInTheDocument();
    expect(screen.getByText(/raport; bramka nie zmienia grupowania/)).toBeInTheDocument();
  });

  it("stays hidden when the route is unavailable", async () => {
    mockFetch(async () => jsonResponse({ detail: "no" }, { status: 404 }));
    const { container } = renderWithProviders(<ConsistencyPanel campaignId="nope" />);
    await new Promise((r) => setTimeout(r, 20));
    expect(container).toBeEmptyDOMElement();
  });
});
