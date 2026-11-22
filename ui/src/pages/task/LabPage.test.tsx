import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { LabPage } from "./LabPage";

// Test fixture only (hydra.kontra.v1 shape); the page itself renders whatever the API returns.
const EXPERIMENT = {
  schema_version: "hydra.kontra.v1",
  experiment_id: "exp1",
  incident_id: "i1",
  mode: "shadow",
  created_at: "2026-11-22T01:00:00+01:00",
  context: {
    base_run: "run1", fingerprint: "fp0123456789abcdef", corpus_signature: "cs", product_commit: "abc1234567890",
    scorer: { assign_model: "m", scorer: "s", thresholds: { link: 10, kit: 6, low: 3, asset: 2 }, margin: 2, abstain_min: 1, max_candidates: 20, prior: null, fusion: null },
  },
  baseline: {
    decision: "assign", campaign_id: "c1", group_id: null, reason_pl: "wsparcie powyżej progu", support_bits: 14.2, link_threshold: 10,
    margin_bits: 5.1, winner: "h-c1", link_rule_met: true, stored: { decision: "assign", campaign_id: "c1" }, matches_stored: false,
  },
  hypotheses: [
    {
      id: "h-c1", kind: "campaign", label_pl: "Kampania c1", campaign_id: "c1", group_id: null, rank: 1, leading: true,
      support_bits: 14.2, campaign_bits: 9, kit_bits: 3, operator_bits: 2, supporting_members: 3, pair_hypothesis: "same_campaign",
      families: { INF: 9.5, VIS: 4.7 }, gap_to_link_bits: -4.2, note_pl: "",
      reasons: [{ family: "INF", kind: "domain", bits: 9.5, explanation_pl: "Wspólna rzadka domena", value: "rare.example", group: "g1",
        evidence: { artifact_id: "art0123456789", bbox: null, char_range: [3, 9], timecode_s: null, har_entry: null, dom_path: null, note: null } }],
    },
    {
      id: "h-c2", kind: "campaign", label_pl: "Kampania c2", campaign_id: "c2", group_id: null, rank: 2, leading: false,
      support_bits: 9.1, campaign_bits: null, kit_bits: null, operator_bits: null, supporting_members: null, pair_hypothesis: null,
      families: { VIS: 9.1 }, gap_to_link_bits: 0.9, note_pl: "konkurent", reasons: [],
    },
  ],
  groups: [{ group_id: "g1", unit: "dependency_group", family: "INF", label_pl: "domena rare.example", n_genes: 2, kinds: ["domain"], relevance_bits: 9.5 }],
  scenarios: [
    { scenario_id: "s0", type: "baseline", class: "baseline", label_pl: "Bez zmian", removed: { groups: [], families: [], artefacts: [], campaigns: [], n_genes: 0 },
      decision: "assign", campaign_id: "c1", group_id: null, winner: "h-c1", support_bits: 14.2, link_threshold: 10, margin_bits: 5.1,
      decision_changed: false, campaign_changed: false, runtime_ms: 12, reason_pl: "" },
    { scenario_id: "s1", type: "remove_group", class: "observation_uncertainty", label_pl: "Bez domeny rare.example", removed: { groups: ["g1"], families: [], artefacts: [], campaigns: [], n_genes: 2 },
      decision: "new_campaign", campaign_id: null, group_id: null, winner: "h-new", support_bits: 4.7, link_threshold: 10, margin_bits: -1,
      decision_changed: true, campaign_changed: true, runtime_ms: 30, reason_pl: "" },
  ],
  flip_set: { found: true, groups: ["g1"], labels_pl: ["domena rare.example"], decision: "new_campaign", campaign_id: null, searched: 7, budget: 64, note_pl: "zestaw wystarczający, nie globalnie najmniejszy" },
  next_observations: {
    heuristic: true, rule: "r", note_pl: "Uwaga heurystyczna.", pair: ["h-c1", "h-c2"],
    ranking: [{ action: "whois", label_pl: "Sprawdź WHOIS domeny", source: "INF", families: ["INF"], separates: true, observed_in_query: false, cost: "low", priority: 1, why_pl: "rozdziela c1 i c2" }],
  },
  retrieval_scope: { truncated: false, bounded_by: "max_candidates" },
  candidates: null,
  limits: { max_groups: 12, max_scenarios: 40, groups_total: 5, groups_tested: 5, scenarios_run: 2, scenarios_skipped: 0, scenario_space_exhausted: true, retrieval_complete: null },
  stats: { recomputations: 2, runtime_ms_total: 42, scenario_stability_fraction: 0.5 },
};

const PACKAGE = {
  package_id: "pkg1", sha256: "deadbeef", created_at: "2026-11-22T01:05:00+01:00", experiment_id: "exp1",
  files: { json: "/v1/lab/packages/pkg1/package.json", markdown: "/v1/lab/packages/pkg1/report.md", html: "/v1/lab/packages/pkg1/report.html" },
};

const render = () => renderWithProviders(<Routes><Route path="/lab/:incident" element={<LabPage />} /></Routes>, { route: "/lab/i1" });

afterEach(() => vi.unstubAllGlobals());

describe("LabPage", () => {
  it("renders hypotheses, scenarios with change badge, flip set, next observations and scope", async () => {
    mockFetch(async () => jsonResponse(EXPERIMENT));
    render();
    const hyps = await screen.findByTestId("lab-hypotheses");
    expect(within(hyps).getByTestId("lab-hypothesis-h-c1")).toHaveAttribute("data-leading", "true");
    expect(within(hyps).getByTestId("lab-hypothesis-h-c2")).toBeInTheDocument();
    expect(screen.getByText("Wspólna rzadka domena")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /artefakt art0123456/ })).toHaveAttribute("href", "/incident/i1?art=art0123456789");
    expect(screen.getByText("Symulacja (shadow) — nie zmienia decyzji produktu")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("różni się od zapisanej decyzji");

    const changed = screen.getByTestId("lab-scenario-s1");
    expect(within(changed).getByText("zmiana decyzji")).toBeInTheDocument();
    expect(within(changed).getByText("niepewność obserwacji")).toBeInTheDocument();
    expect(within(screen.getByTestId("lab-scenario-s0")).queryByText("zmiana decyzji")).toBeNull();
    expect(screen.getByText(/nie prawdopodobieństwo/)).toBeInTheDocument();

    const flip = screen.getByTestId("lab-flip-set");
    expect(within(flip).getByText("domena rare.example")).toBeInTheDocument();
    expect(within(flip).getByText("sprawdzono 7 / budżet 64")).toBeInTheDocument();
    expect(within(flip).getByText("zestaw wystarczający, nie globalnie najmniejszy")).toBeInTheDocument();

    const next = screen.getByTestId("lab-next-observations");
    expect(within(next).getByText("Sprawdź WHOIS domeny")).toBeInTheDocument();
    expect(within(next).getByText("h-c1 ↔ h-c2")).toBeInTheDocument();
    expect(within(next).getByText("koszt: niski")).toBeInTheDocument();
    expect(screen.getByText("rekomendacja heurystyczna, bez wykazanej przewagi")).toBeInTheDocument();
    expect(screen.queryByTestId("lab-stale")).toBeNull();

    const scope = screen.getByTestId("lab-retrieval-scope");
    expect(within(scope).getByText("bounded_by")).toBeInTheDocument();
    expect(within(scope).getByText("retrieval_complete: brak certyfikacji")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it("shows the not-found message when no flipping set was found", async () => {
    mockFetch(async () => jsonResponse({ ...EXPERIMENT, flip_set: { ...EXPERIMENT.flip_set, found: false, groups: [], labels_pl: [], decision: null }, retrieval_scope: null }));
    render();
    const flip = await screen.findByTestId("lab-flip-set");
    expect(within(flip).getByText("W budżecie nie znaleziono zestawu zmieniającego decyzję")).toBeInTheDocument();
    expect(within(screen.getByTestId("lab-retrieval-scope")).getByText("Brak danych o zakresie wyszukiwania")).toBeInTheDocument();
  });

  it("creates a package and shows the receiver acknowledgement", async () => {
    const calls: string[] = [];
    mockFetch(async (input, init) => {
      const url = String(input);
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url.endsWith("/v1/lab/i1/package")) return jsonResponse(PACKAGE);
      if (url.endsWith("/v1/lab/packages/pkg1/send")) {
        return jsonResponse({
          status: "acknowledged", receiver_url: "http://127.0.0.1:8765/receive", reason: null,
          ack: { receipt_id: "rcpt-1", package_id: "pkg1", received_at: "2026-11-22T01:06:00+01:00", duplicate: true, synthetic_case_id: "SYN-0001", note: "demo" },
        });
      }
      return jsonResponse(EXPERIMENT);
    });
    render();
    fireEvent.click(await screen.findByTestId("lab-package"));
    expect(await screen.findByText("pkg1")).toBeInTheDocument();
    expect(screen.getByText("deadbeef")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "HTML" })).toHaveAttribute("href", "/api/v1/lab/packages/pkg1/report.html");
    fireEvent.click(screen.getByTestId("lab-send"));
    const ack = await screen.findByTestId("lab-ack");
    expect(within(ack).getByText("rcpt-1")).toBeInTheDocument();
    expect(within(ack).getByText("SYN-0001")).toBeInTheDocument();
    expect(within(ack).getByText("duplikat")).toBeInTheDocument();
    expect(screen.getByText("lokalny odbiornik demonstracyjny (loopback), numer sprawy syntetyczny")).toBeInTheDocument();
    expect(calls).toContain("POST /api/v1/lab/i1/package");
    expect(calls).toContain("POST /api/v1/lab/packages/pkg1/send");
  });

  it("shows the honest unavailable reason with the receiver hint", async () => {
    mockFetch(async (input) => {
      const url = String(input);
      if (url.endsWith("/package")) return jsonResponse(PACKAGE);
      if (url.endsWith("/send")) return jsonResponse({ status: "unavailable", receiver_url: "http://127.0.0.1:8765/receive", ack: null, reason: "connection refused" });
      return jsonResponse(EXPERIMENT);
    });
    render();
    fireEvent.click(await screen.findByTestId("lab-package"));
    fireEvent.click(await screen.findByTestId("lab-send"));
    const ack = await screen.findByTestId("lab-ack");
    expect(within(ack).getByText("odbiornik niedostępny")).toBeInTheDocument();
    expect(within(ack).getByText("connection refused")).toBeInTheDocument();
    expect(within(ack).getByText("uv run python scripts/kontra_receiver.py")).toBeInTheDocument();
  });

  it.each([
    ["an empty object", { status: "unconfirmed", receiver_url: "http://127.0.0.1:8765/receive", ack: null, reason: "brak identyfikatora pokwitowania (receipt_id); odbiór niepotwierdzony" }],
    ["an acknowledgement of another package", { status: "acknowledged", receiver_url: "http://127.0.0.1:8765/receive", reason: null,
      ack: { receipt_id: "rcpt-x", package_id: "other", received_at: "2026-11-22T01:06:00+01:00", duplicate: false, synthetic_case_id: "SYN-X", note: "" } }],
    ["an acknowledgement without receipt", { status: "acknowledged", receiver_url: "http://127.0.0.1:8765/receive", reason: null, ack: {} }],
  ])("never shows %s as received (ASTRA-DELTA-03)", async (_name, result) => {
    mockFetch(async (input) => {
      const url = String(input);
      if (url.endsWith("/package")) return jsonResponse(PACKAGE);
      if (url.endsWith("/send")) return jsonResponse(result);
      return jsonResponse(EXPERIMENT);
    });
    render();
    fireEvent.click(await screen.findByTestId("lab-package"));
    fireEvent.click(await screen.findByTestId("lab-send"));
    const ack = await screen.findByTestId("lab-ack");
    expect(within(ack).getByText("niepotwierdzone")).toBeInTheDocument();
    expect(within(ack).queryByText("potwierdzono odbiór")).toBeNull();
    expect(within(ack).queryByText("SYN-X")).toBeNull();
    expect(ack).not.toHaveAttribute("data-confirmed");
  });

  it("shows a stale context in Polish and blocks the package (ASTRA-DELTA-02/10)", async () => {
    mockFetch(async () => jsonResponse({ ...EXPERIMENT, status: "stale_context", stale_reasons: ["background_prior"], stale_reasons_pl: ["prior tła (tryb lub plik)"] }));
    render();
    const stale = await screen.findByTestId("lab-stale");
    expect(stale).toHaveTextContent("Kontekst nieaktualny");
    expect(stale).toHaveTextContent("prior tła (tryb lub plik)");
    expect(screen.getByTestId("lab-package")).toBeDisabled();
  });

  it("lists observations without their artefact as not rankable (§7)", async () => {
    const next = { ...EXPERIMENT.next_observations, unavailable: [{ action: "reread_har", label_pl: "Ponowny odczyt zapisanego HAR", source: "har", requires: "har", cost: "low", why_pl: "brak" }] };
    mockFetch(async () => jsonResponse({ ...EXPERIMENT, next_observations: next }));
    render();
    const un = await screen.findByTestId("lab-next-unavailable");
    expect(un).toHaveTextContent("Ponowny odczyt zapisanego HAR: brak HAR");
    expect(within(screen.getByTestId("lab-next-observations")).getAllByRole("listitem").map((li) => li.textContent)).not.toContain("1.Ponowny odczyt zapisanego HAR");
  });

  it("shows an empty state with the run button on 404 and runs the experiment", async () => {
    let ran = false;
    const posts: string[] = [];
    mockFetch(async (input, init) => {
      const url = String(input);
      if (init?.method === "POST" && url.endsWith("/v1/lab/i1/run")) {
        posts.push(String(init.body));
        ran = true;
        return jsonResponse(EXPERIMENT);
      }
      if (!ran) return url.includes("missing=null") ? jsonResponse(null) : jsonResponse({ detail: "no experiment" }, { status: 404 });
      return jsonResponse(EXPERIMENT);
    });
    render();
    const empty = await screen.findByTestId("lab-empty");
    const button = within(empty).getByTestId("lab-run");
    expect(button).toHaveTextContent("Uruchom przeliczenie");
    fireEvent.click(button);
    expect(await screen.findByTestId("lab-hypotheses")).toBeInTheDocument();
    await waitFor(() => expect(posts).toEqual(["{}"]));
  });
});
