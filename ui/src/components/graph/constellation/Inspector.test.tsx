import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CampaignDetail, GEdge, GNode, Overview, OvCampaign } from "../../../api/graph";
import { jsonResponse, mockFetch, renderWithProviders } from "../../../test/utils";
import { CampaignPanel, EdgePanel, IncidentPanel, OverviewPanel } from "./Inspector";

afterEach(() => vi.unstubAllGlobals());

const camp = (id: string, size: number, extra: Partial<OvCampaign> = {}): OvCampaign => ({
  id, label: `Kampania ${id}`, size, group_id: null, first_seen: null, last_seen: null, confidence: null,
  x: null, y: null, r: null, family_bits: null, thumbnail: null, ...extra,
});

const overview = (o: Partial<Overview> = {}): Overview => ({
  source: "lod", run_id: "syn-run-1", corpus: "syn", synthetic: true, thresholds: { link_bits: 10, kit_bits: 6 },
  stats: {
    nodes: 1234, edges: 50, components: 3, campaigns: 17, multi_campaigns: 9, groups: 4,
    levels: { campaign: 31, kit: 7 }, largest_campaign: null, robustness_scored: 20, robust: 15, must_link: 2, cannot_link: 1, inferred: 0,
  },
  campaigns: [camp("c1", 3), camp("c2", 40), camp("__bez_kampanii__", 99, { unassigned: true })],
  groups: [], links: [], positioned: false, incidents_total: 1234, ...o,
});

const edge = (id: string, s: string, t: string, bits: number, o: Partial<GEdge> = {}): GEdge => ({
  id, source: s, target: t, bits, p: 0, hypothesis: "same_campaign", level: "campaign", family: "VIS", family_bits: { VIS: bits },
  campaign_bits: bits, kit_bits: null, asset_bits: null, inferred: false, robustness: null, review: null,
  reasons: [{ family: "VIS", kind: "pdq", bits, value: null, doc_freq: 2, text: `wspólny obraz ${id}` }], ...o,
});

const node = (id: string, observed_at: string | null): GNode => ({
  id, title: `Tytuł ${id}`, campaign_id: "c1", group_id: null, observed_at, registered_at: null, corpus: "syn", thumbnail: null, degree: 1,
});

describe("OverviewPanel", () => {
  it("renders stats from the payload, the synthetic badge, the legend and focuses a campaign", async () => {
    mockFetch(async () => jsonResponse({}));
    const onFocus = vi.fn();
    renderWithProviders(<OverviewPanel overview={overview()} loaded={2} loading={1} budget={5000} onFocusCampaign={onFocus} />);
    expect(screen.getByText("dane SYNTETYCZNE")).toBeInTheDocument();
    expect(screen.getAllByText((1234).toLocaleString("pl-PL")).length).toBeGreaterThan(0);
    expect(screen.getByText("17")).toBeInTheDocument();
    expect(screen.getByText("15 / 20")).toBeInTheDocument();
    expect(screen.getByText(/Bity to wagi dowodów, nie prawdopodobieństwa/)).toBeInTheDocument();
    expect(screen.getByText(/budżet/)).toHaveTextContent(`budżet ${(5000).toLocaleString("pl-PL")} incydentów`);
    expect(screen.queryByText(/poziomu szczegółowości/)).not.toBeInTheDocument();
    const list = screen.getByRole("heading", { name: "Największe kampanie" }).closest("section")!;
    const buttons = list.querySelectorAll("button");
    expect(buttons).toHaveLength(2); // the unassigned bucket is not a campaign
    expect(buttons[0]).toHaveTextContent("Kampania c2");
    await userEvent.click(buttons[0]!);
    expect(onFocus).toHaveBeenCalledWith("c2");
  });

  it("shows dashes instead of invented stats and the legacy note", () => {
    mockFetch(async () => jsonResponse({}));
    renderWithProviders(<OverviewPanel overview={overview({ stats: null, source: "legacy", synthetic: false })} loaded={0} loading={0} budget={null} onFocusCampaign={() => {}} />);
    expect(screen.getByText(/nie przesłał statystyk/)).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(6);
    expect(screen.getByText(/poziomu szczegółowości/)).toBeInTheDocument();
    expect(screen.queryByText("dane SYNTETYCZNE")).not.toBeInTheDocument();
  });
});

describe("CampaignPanel", () => {
  const summary = () => mockFetch(async (input) => {
    if (String(input).includes("/summary")) return jsonResponse({ campaign_id: "c1", status: "ok", text_pl: "Wspólny rzadki obraz.", core_genes: [{ family: "VIS", kind: "pdq", bits: 12.5, text: "ten sam kreatyw" }], family_bits: { VIS: 12.5 } });
    return jsonResponse({}, { status: 404 });
  });
  const base = { campaign: camp("c1", 3), group: null, onRetry: () => {}, onPickIncident: () => {}, onPickEdge: () => {}, onClose: () => {} };

  it("shows a skeleton while loading and the summary", async () => {
    summary();
    renderWithProviders(<CampaignPanel {...base} detail={undefined} status="loading" />);
    expect(screen.getByTestId("members-skeleton")).toBeInTheDocument();
    expect(screen.getByTestId("members-skeleton").querySelector(".motion-safe\\:animate-pulse")).not.toBeNull();
    expect(await screen.findByText("Wspólny rzadki obraz.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "karta kampanii" })).toHaveAttribute("href", "/campaigns/c1");
  });

  it("shows the error and retries", async () => {
    summary();
    const onRetry = vi.fn();
    renderWithProviders(<CampaignPanel {...base} onRetry={onRetry} detail={undefined} status="error" error="HTTP 500" />);
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 500");
    await userEvent.click(screen.getByRole("button", { name: "Spróbuj ponownie" }));
    expect(onRetry).toHaveBeenCalled();
  });

  it("lists members by date and strongest links when ready", async () => {
    summary();
    const onPickEdge = vi.fn();
    const onPickIncident = vi.fn();
    const detail: CampaignDetail = {
      campaign_id: "c1",
      nodes: [node("i2", "2026-02-01T00:00:00Z"), node("i1", "2026-01-01T00:00:00Z"), node("i3", null)],
      edges: [edge("e1", "i1", "i2", 4), edge("e2", "i2", "i3", 22.25)],
      external: [{ ...edge("x1", "i1", "z9", 3), other_campaign: "c9" }],
      truncated: true,
    };
    renderWithProviders(<CampaignPanel {...base} onPickEdge={onPickEdge} onPickIncident={onPickIncident} detail={detail} status="ready" />);
    const members = screen.getByRole("heading", { name: "Incydenty (3)" }).closest("section")!.querySelectorAll("li button");
    expect([...members].map((b) => b.textContent)).toEqual([expect.stringContaining("i1"), expect.stringContaining("i2"), expect.stringContaining("i3")]);
    expect(screen.getByText(/niepełną listę/)).toBeInTheDocument();
    await userEvent.click(screen.getByText("22,3 b"));
    expect(onPickEdge).toHaveBeenCalledWith(detail.edges[1]);
    await userEvent.click(members[0]!);
    expect(onPickIncident).toHaveBeenCalledWith("i1");
    expect(screen.getByText("c9")).toBeInTheDocument();
  });

  it("explains the unassigned bucket without asking for a summary", () => {
    const f = summary();
    renderWithProviders(<CampaignPanel {...base} campaign={camp("__bez_kampanii__", 5, { unassigned: true })} detail={undefined} status="loading" />);
    expect(screen.getByText(/incydenty bez przypisanej kampanii/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "karta kampanii" })).not.toBeInTheDocument();
    expect(f).not.toHaveBeenCalled();
  });
});

describe("IncidentPanel", () => {
  it("explains missing artefacts and lists links by bits", async () => {
    mockFetch(async () => jsonResponse({
      id: "i1", source: "s", source_ref: "i1", title: "Fałszywa reklama", observed_at: null, meta: {}, artifact_roles: {}, artifact_count: 0,
      observed_genes: {}, thumbnail: null, campaign_id: "c1", artifacts: [], families: [], enrichment: [], run_id: "r", assignment: null, neighbours: [],
    }));
    const onPickEdge = vi.fn();
    const edges = [edge("e1", "i1", "i2", 4), { ...edge("e2", "i3", "i1", 9), other_campaign: "c2" }];
    renderWithProviders(<IncidentPanel id="i1" node={node("i1", null)} edges={edges} onPickEdge={onPickEdge} onPickIncident={() => {}} onClose={() => {}} />);
    expect(await screen.findByText(/nie ma zapisanych artefaktów/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Fałszywa reklama");
    expect(screen.getByRole("link", { name: "otwórz incydent" })).toHaveAttribute("href", "/incident/i1");
    const rows = screen.getByRole("heading", { name: "Powiązania (2)" }).closest("section")!.querySelectorAll("ul li button");
    expect(rows[0]).toHaveTextContent("9,0 b");
    expect(rows[0]).toHaveTextContent("inna kampania");
    await userEvent.click(rows[0]!);
    expect(onPickEdge).toHaveBeenCalledWith(edges[1]);
  });
});

describe("EdgePanel", () => {
  it("states bits are not probabilities and shows stored reasons while the pair loads, then the waterfall", async () => {
    let release: (r: Response) => void = () => {};
    mockFetch(() => new Promise<Response>((res) => { release = res; }));
    const onClose = vi.fn();
    const e = edge("e1", "i1", "i2", 14.2);
    renderWithProviders(<EdgePanel edge={e} a={node("i1", null)} b={node("i2", null)} onClose={onClose} onPickIncident={() => {}} />);
    expect(screen.getAllByText(/nie prawdopodobieństwa/).length).toBeGreaterThan(0);
    expect(screen.getByText("wspólny obraz e1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "porównaj materiały" })).toHaveAttribute("href", "/pair/i1/i2");
    release(jsonResponse({
      run_id: "r1",
      score: { a: "i1", b: "i2", family_bits: { VIS: 14.2 }, missing_families: ["ID"], contradictions: [], reasons: [], total_bits: 14.2, probability: 0.9, calibrated: false, hypothesis: "same_campaign", model: "m" },
      dominant_family: "VIS", incident_a: null, incident_b: null, same_campaign_in_run: true, review: null, reviews: [],
    }));
    expect(await screen.findByTestId("waterfall")).toBeInTheDocument();
    expect(screen.getByText(/nie wskazuje miejsca w artefaktach/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Zamknij" }));
    expect(onClose).toHaveBeenCalled();
  });
});
