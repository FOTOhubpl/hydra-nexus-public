import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CampaignLineage } from "../../api/task";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { LineagePanel } from "./LineagePanel";

// Hand-written synthetic fixture (never organiser data).
const item = (kind: string, category: string, category_pl: string, bits: number | null = null) => ({ kind, family: "INF", category, category_pl, bits });
const LINEAGE: CampaignLineage = {
  campaign_id: "syn-c1",
  run_id: "syn-run-1",
  direction: "observation_order",
  direction_pl: "kolejność obserwacji (najwcześniejsza data z materiału)",
  roots: ["syn-1"],
  nodes: [
    { incident_id: "syn-1", observed_at: "2026-10-01T10:00:00+00:00", order: 1, root: true, parent: null, depth: 0 },
    { incident_id: "syn-2", observed_at: "2026-10-03T09:00:00+00:00", order: 2, root: false, parent: "syn-1", depth: 1 },
  ],
  edges: [{
    parent: "syn-1", child: "syn-2", bits: 16.2, hypothesis: "same_campaign", total_bits: 16.2,
    kept: [item("tracker_id", "operator", "identyfikatory operatora", 12), item("asset_sha256", "kit", "pliki zestawu", 4)],
    changed: [item("registrable_domain", "domain", "domena")], added: [], removed: [],
    unknown: [item("face", "person", "osoba / wizerunek")],
    counts: { kept: 2, changed: 1, added: 0, removed: 0, unknown: 1 },
    summary_pl: "wariant syn-2 (po syn-1): nowa domena; bez zmian: piksel, pliki zestawu",
  }],
  note_pl: "Kierunek to kolejność obserwacji, nie udowodniona kolejność edycji.",
};

afterEach(() => vi.unstubAllGlobals());

describe("LineagePanel", () => {
  it("renders edges with changed and kept categories and the honest direction label", async () => {
    const fetch = mockFetch(async () => jsonResponse(LINEAGE));
    renderWithProviders(<LineagePanel campaignId="syn-c1" runId="syn-run-1" />);
    expect(await screen.findByText("Linia wariantów")).toBeInTheDocument();
    expect(String(fetch.mock.calls[0]![0])).toContain("/v1/campaigns/syn-c1/lineage?run_id=syn-run-1");
    expect(screen.getByText("kolejność obserwacji")).toBeInTheDocument();
    expect(screen.getByText(/nowa domena; bez zmian: piksel/)).toBeInTheDocument();
    expect(screen.getByText("domena")).toBeInTheDocument();
    expect(screen.getByText("pliki zestawu")).toBeInTheDocument();
    expect(screen.getByText(/nieznane po jednej stronie: 1/)).toBeInTheDocument();
    expect(screen.getByText(/nie udowodniona kolejność edycji/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "porównaj" })).toHaveAttribute("href", "/diff/syn-1/syn-2");
  });

  it("is hidden for a singleton campaign", async () => {
    const fetch = mockFetch(async () => jsonResponse({ ...LINEAGE, roots: ["syn-6"], edges: [],
      nodes: [{ incident_id: "syn-6", observed_at: null, order: 1, root: true, parent: null, depth: 0 }] }));
    const { container } = renderWithProviders(<LineagePanel campaignId="syn-c3" />);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(container).toBeEmptyDOMElement();
  });

  it("stays hidden when the route is unavailable", async () => {
    mockFetch(async () => jsonResponse({ detail: "campaign not found in this run" }, { status: 404 }));
    const { container } = renderWithProviders(<LineagePanel campaignId="nope" />);
    await new Promise((r) => setTimeout(r, 20));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the mutation tree with mutations on edges when the API returns one", async () => {
    mockFetch(async () => jsonResponse({ ...LINEAGE, mutation_tree: {
      method: "mutation_distance_arborescence@1", root: "syn-1", spanning: true, candidates: "all_member_pairs", total_cost: 1.2,
      agreement_with_link_tree: 1, nodes: LINEAGE.nodes, note_pl: "Drzewo wariantów (hipoteza).",
      edges: [{ parent: "syn-1", child: "syn-2", cost: 1.2, mutation_distance: 1.0, direction_penalty: 0, support_bits: 16.2,
        against_observation_order: false, mutation_count: 1, summary_pl: "rotacja domeny (a.example → b.example)",
        mutations: [{ kind: "registrable_domain", family: "INF", category: "domain", category_pl: "domena", type: "domain_rotation",
          type_pl: "rotacja domeny", cost: 1, distance: 1, before: ["a.example"], after: ["b.example"] }] }],
    } }));
    renderWithProviders(<LineagePanel campaignId="syn-c1" runId="syn-run-1" />);
    expect(await screen.findByTestId("mutation-tree")).toBeInTheDocument();
    expect(screen.getByText("rotacja domeny: domena")).toBeInTheDocument();
    expect(screen.getByText("rotacja domeny (a.example → b.example)")).toBeInTheDocument();
  });

  it("has no mutation tree without the flag", async () => {
    mockFetch(async () => jsonResponse(LINEAGE));
    renderWithProviders(<LineagePanel campaignId="syn-c1" />);
    expect(await screen.findByText("Linia wariantów")).toBeInTheDocument();
    expect(screen.queryByTestId("mutation-tree")).toBeNull();
  });
});
