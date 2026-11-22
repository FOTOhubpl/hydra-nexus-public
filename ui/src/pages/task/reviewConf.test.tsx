/** feat/review-conf: campaign support in bits (CR-ASTRA-9), review queue (ADOPT #14), similar cases. Synthetic fixtures. */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { describe, expect, it } from "vitest";
import { ReviewBadge } from "../../components/evidence";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { CampaignCardPage } from "./CampaignCardPage";
import { CampaignsPage } from "./CampaignsPage";
import { IncidentPage } from "./IncidentPage";
import { QueuePage } from "./QueuePage";

const support = {
  calibrated: false, kind: "heuristic_evidence_bits", singleton: false, links: 4, possible_links: 6, density: 0.667,
  median_link_bits: 18.4, weakest_link_bits: 7.2, cohesion: 0.61, min_member_index: 0.3, weakest_member: "i4",
  bridges: [{ a: "i3", b: "i4", bits: 7.2, split_sizes: [3, 1] }, { a: "i1", b: "i2", bits: 9.0, split_sizes: [1, 3] }],
  articulation_points: ["i3"], members: [], stability: { families: {}, stable: true, fragile_families: [] }, rank: 1, rank_of: 2,
};

const campaign = (id: string, meta: Record<string, unknown>) => ({
  id, label: `Kampania ${id}`, members: ["i1", "i2", "i3", "i4"], core_genome: [], scheme: null, first_seen: null, last_seen: null,
  confidence: 0.873, status: "active", group_id: null, meta, size: 4,
});

const queue = {
  run_id: "r1", threshold_bits: 10, method_pl: "Kolejność według wartości decyzji (heurystyka).",
  items: [
    { key: "i3|i4", kinds: ["bridge", "near_threshold"], a: "i3", b: "i4", incident_id: null, campaigns: ["c1"], bits: 12.3, distance_bits: 2.3, impact: 3, uncertainty: 0.8, ev: 2.4, reason_pl: "Jedyne powiązanie utrzymujące i4 w kampanii." },
    {
      key: "i9", kinds: ["abstain"], a: null, b: null, incident_id: "i9", campaigns: ["c1", "c2"], bits: null, distance_bits: null, impact: 1, uncertainty: 1, ev: 1.1, reason_pl: "Dwie kampanie z podobnym wsparciem.",
      candidates: [
        { campaign_id: "c1", bits: 11.0, supporting_members: 2, top_reasons: [{ family: "INF", kind: "domain", value: "a.example", bits: 8, explanation_pl: "rzadka domena" }] },
        { campaign_id: "c2", bits: 10.5, supporting_members: 1, top_reasons: [] },
      ],
    },
  ],
  skipped: { decided: 1, unsure: 0, decided_between_campaigns: 0 },
  unsure: [{ id: "u7", target: { a: "i5", b: "i6" }, note: null, analyst: "operator", created_at: "2026-11-21T20:00:00+01:00" }],
};

const pairDetail = {
  run_id: "r1",
  score: { a: "i3", b: "i4", family_bits: { INF: 12.3 }, missing_families: [], unknown_families: [], contradictions: [], reasons: [], total_bits: 12.3, probability: 0.6, calibrated: false, hypothesis: "same_campaign", model: "m" },
  dominant_family: "INF", incident_a: null, incident_b: null, same_campaign_in_run: true, review: null, reviews: [],
};

describe("campaigns list (CR-ASTRA-9)", () => {
  it("shows evidence bits, the weakest link and bridges instead of a confidence percentage", async () => {
    mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/v1/runs/latest")) return jsonResponse({ run: { id: "r1", kind: "analyse", params: {}, metrics: {}, created_at: null, finished_at: null }, counts: { incidents: 6 }, contracts_version: "2.1.0" });
      if (url.includes("/v1/campaign-groups")) return jsonResponse({ run_id: "r1", items: [] });
      return jsonResponse({ items: [campaign("c1", { support }), campaign("c2", {})], total: 2, limit: 2000, offset: 0, run_id: "r1" });
    });
    renderWithProviders(<CampaignsPage />);
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Dowody (bit)")).toBeInTheDocument();
    expect(within(table).getByText("Najsłabsze ogniwo")).toBeInTheDocument();
    expect(within(table).queryByText(/Pewność/)).toBeNull();
    expect(within(table).getByText("+18,4 b")).toBeInTheDocument();
    expect(within(table).getByText("+7,2 b")).toBeInTheDocument();
    expect(within(table).getByText("mosty: 2")).toBeInTheDocument();
    // the old confidence 0.873 must not be shown in any form, and no cell carries a percentage
    expect(within(table).queryByText(/0,873|87[,.]3|%/)).toBeNull();
    // a run without meta.support renders unknown, not zero
    expect(within(table).getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });
});

describe("campaign card (CR-ASTRA-9)", () => {
  it("shows bit tiles, members weakest first, bridges, family stability and the disclaimer", async () => {
    const members = [
      { id: "i1", bits_to_rest: 40, anchor_bits: 20, links: 3, families: ["INF", "VIS"], n_families: 2, index: 0.9, articulation: false, analyst_link: true },
      { id: "i4", bits_to_rest: 7.2, anchor_bits: 7.2, links: 1, families: ["TXT"], n_families: 1, index: 0.3, articulation: false, analyst_link: false },
      { id: "i3", bits_to_rest: 30, anchor_bits: 18, links: 3, families: ["INF"], n_families: 1, index: 0.7, articulation: true, analyst_link: false },
    ];
    const stability = { families: { INF: { parts: 2, largest_share: 0.75, detached: ["i4"] }, VIS: { parts: 1, largest_share: 1, detached: [] } }, stable: false, fragile_families: ["INF"] };
    const card = {
      ...campaign("c1", { support: { ...support, members, stability } }),
      member_incidents: [], infrastructure: {}, persons: {}, timeline: [], internal_pairs: [], cross_links: [], group: null, run_id: "r1",
    };
    mockFetch(async (input) => (/\/v1\/campaigns\/c1(\?|$)/.test(String(input)) ? jsonResponse(card) : jsonResponse({ detail: "not found" }, { status: 404 })));
    renderWithProviders(<Routes><Route path="/campaigns/:id" element={<CampaignCardPage />} /></Routes>, { route: "/campaigns/c1" });
    expect(await screen.findByText("Dowody (mediana)")).toBeInTheDocument();
    expect(screen.queryByText("Pewność")).toBeNull();
    expect(screen.queryByText(/Średnie p/)).toBeNull();
    expect(screen.getByText("gęstość 0,67 (4/6 powiązań)")).toBeInTheDocument();
    expect(screen.getByText("Indeks spójności (heurystyka)")).toBeInTheDocument();
    const rows = within(screen.getByTestId("member-support")).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("link")[0]!.textContent)).toEqual(["i4", "i3", "i1"]);
    expect(within(rows[1]!).getByText("punkt artykulacji")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("analityk")).toBeInTheDocument();
    expect(screen.getByText("Mosty (2)")).toBeInTheDocument();
    expect(screen.getByText("Stabilność bez jednej rodziny")).toBeInTheDocument();
    expect(screen.getByText("krucha")).toBeInTheDocument();
    expect(screen.getAllByText(/nie są skalibrowanym prawdopodobieństwem/).length).toBeGreaterThan(0);
  });
});

describe("review queue (ADOPT #14)", () => {
  it("renders queue items with Polish kind badges, bits vs threshold, and N posts an unsure mark", async () => {
    const posts: { url: string; body: unknown }[] = [];
    mockFetch(async (input, init) => {
      const url = String(input);
      if (init?.method === "POST") {
        posts.push({ url, body: JSON.parse(String(init.body)) });
        return jsonResponse({ mark: { id: "u8", target: { a: "i3", b: "i4" }, note: "kolejka analityka", analyst: "operator", created_at: "x" }, event_hash: "h" });
      }
      if (url.includes("/v1/review/queue")) return jsonResponse(queue);
      if (url.includes("/v1/pairs/")) return jsonResponse(pairDetail);
      return jsonResponse({}, { status: 404 });
    });
    renderWithProviders(<QueuePage />);
    const list = await screen.findByTestId("queue-list");
    expect(within(list).getByText("most")).toBeInTheDocument();
    expect(within(list).getAllByText("blisko progu").length).toBe(1);
    expect(within(list).getByText("wstrzymane")).toBeInTheDocument();
    expect(within(list).getByText("12,3 bit (próg 10)")).toBeInTheDocument();
    expect(screen.getByText("Jedyne powiązanie utrzymujące i4 w kampanii.")).toBeInTheDocument();
    expect(screen.getByText(/wartość decyzji \(heurystyka\)/)).toBeInTheDocument();
    expect(screen.getByText(/nie zmienia kampanii/, { selector: "p" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "n" });
    await waitFor(() => expect(posts).toEqual([{ url: expect.stringContaining("/v1/review/unsure"), body: { target: { a: "i3", b: "i4" }, note: "kolejka analityka" } }]));
    expect(posts[0]!.url).not.toContain("/undo");
  });

  it("abstain items show the three-column card; A/R are disabled, N targets the incident", async () => {
    const posts: { url: string; body: unknown }[] = [];
    mockFetch(async (input, init) => {
      const url = String(input);
      if (init?.method === "POST") {
        posts.push({ url, body: JSON.parse(String(init.body)) });
        return jsonResponse({ mark: { id: "u9", target: { incident_id: "i9" }, note: null, analyst: "operator", created_at: "x" }, event_hash: "h" });
      }
      if (url.includes("/v1/review/queue")) return jsonResponse(queue);
      if (url.includes("/v1/pairs/")) return jsonResponse(pairDetail);
      return jsonResponse({ detail: "not found" }, { status: 404 });
    });
    renderWithProviders(<QueuePage />);
    await screen.findByTestId("queue-list");
    fireEvent.keyDown(window, { key: "j" });
    const card = await screen.findByTestId("abstain-card");
    expect(within(card).getByText("Kampania A: dowody")).toBeInTheDocument();
    expect(within(card).getByText("Kampania B: dowody")).toBeInTheDocument();
    expect(within(card).getByText("a.example")).toBeInTheDocument();
    expect(within(card).getByText("wspierający członkowie: 2")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: /strona incydentu/ })).toHaveAttribute("href", "/incident/i9");
    expect(screen.getByRole("button", { name: /Potwierdź \(A\)/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Odrzuć \(R\)/ })).toBeDisabled();
    fireEvent.keyDown(window, { key: "a" });
    fireEvent.click(screen.getByRole("button", { name: /Niepewne \(N\)/ }));
    await waitFor(() => expect(posts.map((p) => p.body)).toEqual([{ target: { incident_id: "i9" }, note: "kolejka analityka" }]));
  });

  it("lists active unsure marks with an undo button that posts to /v1/review/unsure/undo", async () => {
    const posts: { url: string; body: unknown }[] = [];
    mockFetch(async (input, init) => {
      const url = String(input);
      if (init?.method === "POST") {
        posts.push({ url, body: JSON.parse(String(init.body)) });
        return jsonResponse({ undone: "u7", event_hash: "h" });
      }
      if (url.includes("/v1/review/queue")) return jsonResponse(queue);
      return jsonResponse(pairDetail);
    });
    renderWithProviders(<QueuePage />);
    fireEvent.click(await screen.findByRole("tab", { name: /Niepewne \(N\)/ }));
    const list = screen.getByTestId("unsure-list");
    expect(within(list).getByText("i5 ↔ i6")).toBeInTheDocument();
    fireEvent.click(within(list).getByRole("button", { name: /Cofnij oznaczenie/ }));
    await waitFor(() => expect(posts).toEqual([{ url: expect.stringContaining("/v1/review/unsure/undo"), body: { id: "u7" } }]));
  });
});

describe("review state badge", () => {
  it("renders the unsure pair state as 'niepewne'", () => {
    renderWithProviders(<ReviewBadge value="unsure" />);
    expect(screen.getByText("? niepewne")).toBeInTheDocument();
  });
});

describe("similar cases panel", () => {
  const incident = {
    id: "i1", source: "s", source_ref: "i1", title: "T i1", observed_at: null, meta: {}, artifact_roles: {}, artifact_count: 0, observed_genes: {},
    thumbnail: null, campaign_id: "c1", artifacts: [], families: [], enrichment: [], run_id: "r1", assignment: null, neighbours: [],
  };
  const route = (similar: unknown) =>
    mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/similar")) return jsonResponse(similar);
      if (/\/v1\/incidents\/i1(\?|$)/.test(url)) return jsonResponse(incident);
      return jsonResponse({ detail: "not found" }, { status: 404 });
    });
  const render = () => renderWithProviders(<Routes><Route path="/incident/:id" element={<IncidentPage />} /></Routes>, { route: "/incident/i1" });

  it("renders a row with shared counts, rarity bits, duplicate badge and expandable reasons", async () => {
    route({
      incident_id: "i1", run_id: "r1", total_values: 10, operator_values: 3, excluded_common: 4, method_pl: "Wspólne wartości ważone rzadkością.", empty_reason_pl: null,
      items: [{
        incident_id: "i7", campaign_id: "c2", title: "Inny materiał", shared: 2, total: 10, shared_operator: 1, total_operator: 3, ratio: 0.2, rare_bits: 14.2,
        per_kind: { domain: 1, phone: 1 }, duplicate: true,
        values: [{ kind: "domain", family: "INF", value: "rare.example", df: 2, bits: 9.1, tier: "operator", explanation_pl: "domena w 2 incydentach" }],
        reasons_pl: ["wspólna rzadka domena"],
      }],
    });
    render();
    const row = await screen.findByTestId("similar-row");
    expect(screen.getByText("Podobne przypadki")).toBeInTheDocument();
    expect(screen.getByTestId("incident-lab-link")).toHaveAttribute("href", "/lab/i1");
    expect(screen.getByText("wspólne rzadkie wartości; karta wyjaśniająca, nie wynik powiązania")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "i7" })).toHaveAttribute("href", "/incident/i7");
    expect(within(row).getByRole("link", { name: "c2" })).toHaveAttribute("href", "/campaigns/c2");
    expect(within(row).getByText("wspólne 2/10")).toBeInTheDocument();
    expect(within(row).getByText("operatora 1/3")).toBeInTheDocument();
    expect(within(row).getByText("rzadkość 14,2 bit")).toBeInTheDocument();
    expect(within(row).getByText("duplikat")).toBeInTheDocument();
    expect(within(row).queryByText("rare.example")).toBeNull();
    fireEvent.click(within(row).getByRole("button", { name: /powody/ }));
    expect(within(row).getByText("rare.example")).toBeInTheDocument();
    expect(within(row).getByText("w 2 incydentach")).toBeInTheDocument();
    expect(within(row).getByText("wspólna rzadka domena")).toBeInTheDocument();
  });

  it("shows the empty reason when there are no similar cases", async () => {
    route({ incident_id: "i1", run_id: "r1", total_values: 3, operator_values: 0, excluded_common: 3, method_pl: "m", empty_reason_pl: "Wszystkie wartości tego incydentu są zbyt częste, by coś wyróżnić.", items: [] });
    render();
    expect(await screen.findByText("Wszystkie wartości tego incydentu są zbyt częste, by coś wyróżnić.")).toBeInTheDocument();
    expect(screen.queryByTestId("similar-row")).toBeNull();
  });
});
