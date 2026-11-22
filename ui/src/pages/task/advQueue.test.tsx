import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AssignmentMeta } from "../../api/task";
import { DecisionRule } from "../../components/DecisionRule";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { QueuePage } from "./QueuePage";

// shapes of hydra/review_queue.py + hydra/queue_gain.py and hydra/assign.py explain(); values are fixtures, not results
const item = {
  key: "i1|i2", kinds: ["near_threshold"], a: "i1", b: "i2", incident_id: null, campaigns: ["c1", "c2"], bits: 8, distance_bits: -2,
  impact: 2, uncertainty: 0.5, ev: 1, reason_pl: "8,0 bit wobec progu 10", gain: 2.4, effort: 1, gain_per_effort: 2.4, p_confirm: 0.2,
  counterfactual: { confirm: { decisions: 12, incidents: 3, campaigns: ["c1", "c2"], sizes: [4, 3], effect: "merge" }, reject: { decisions: 0, incidents: 0, campaigns: [], effect: "none" } },
  why_first_pl: "Potwierdzenie połączy c1 i c2 (4 + 3 incydentów, 12 decyzji o parach); odrzucenie niczego nie zmieni w podziale.",
};

describe("ADV-QUEUE", () => {
  it("queue: shows why the first item is first and O switches the order (keyboard flow kept)", async () => {
    const urls: string[] = [];
    mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/v1/review/queue")) {
        urls.push(url);
        const order = url.includes("order=ev") ? "ev" : "gain";
        return jsonResponse({ run_id: "r", order, threshold_bits: 10, method_pl: "Kolejka.", items: [item], skipped: { decided: 0, unsure: 0, decided_between_campaigns: 0 }, unsure: [] });
      }
      return jsonResponse({ run_id: "r", score: { a: "i1", b: "i2", total_bits: 8, probability: 0.4, calibrated: false, hypothesis: "abstain", model: "m", family_bits: { TXT: 8 }, missing_families: [], reasons: [], contradictions: [], unknown_families: [] }, review: null, reviews: [] });
    });
    renderWithProviders(<QueuePage />);
    const why = await screen.findByTestId("why-first");
    expect(why).toHaveTextContent("Dlaczego to pierwsze");
    expect(why).toHaveTextContent("Potwierdzenie połączy c1 i c2");
    expect(screen.getAllByText(/zysk 2,4/).length).toBeGreaterThan(0);
    fireEvent.keyDown(window, { key: "o" });
    await waitFor(() => expect(urls.some((u) => u.includes("order=ev"))).toBe(true));
  });

  it("B10: a viewer sees disabled write buttons with the read-only tooltip and A does not post", async () => {
    const posts: string[] = [];
    mockFetch(async (input, init) => {
      const url = String(input);
      if ((init?.method ?? "GET") !== "GET") posts.push(url);
      if (url.includes("/v1/auth/session")) return jsonResponse({ auth_enabled: true, authenticated: true, user: { id: 3, username: "qa-viewer", role: "viewer" } });
      if (url.includes("/v1/review/queue")) return jsonResponse({ run_id: "r", order: "gain", threshold_bits: 10, method_pl: "Kolejka.", items: [item], skipped: { decided: 0, unsure: 0, decided_between_campaigns: 0 }, unsure: [] });
      return jsonResponse({ run_id: "r", score: { a: "i1", b: "i2", total_bits: 8, probability: 0.4, calibrated: false, hypothesis: "abstain", model: "m", family_bits: { TXT: 8 }, missing_families: [], reasons: [], contradictions: [], unknown_families: [] }, review: null, reviews: [] });
    });
    renderWithProviders(<QueuePage />);
    const confirm = await screen.findByRole("button", { name: /Potwierdź \(A\)/ });
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(confirm).toHaveAttribute("title", "Brak uprawnień (rola: przeglądający)");
    expect(screen.getByRole("button", { name: /Odrzuć \(R\)/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Niepewne \(N\)/ })).toBeDisabled();
    fireEvent.keyDown(window, { key: "a" });
    expect(posts).toEqual([]);
  });

  it("B10: an analyst keeps the write buttons enabled", async () => {
    mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/v1/auth/session")) return jsonResponse({ auth_enabled: true, authenticated: true, user: { id: 2, username: "a", role: "analyst" } });
      if (url.includes("/v1/review/queue")) return jsonResponse({ run_id: "r", order: "gain", threshold_bits: 10, method_pl: "Kolejka.", items: [item], skipped: { decided: 0, unsure: 0, decided_between_campaigns: 0 }, unsure: [] });
      return jsonResponse({ run_id: "r", score: { a: "i1", b: "i2", total_bits: 8, probability: 0.4, calibrated: false, hypothesis: "abstain", model: "m", family_bits: { TXT: 8 }, missing_families: [], reasons: [], contradictions: [], unknown_families: [] }, review: null, reviews: [] });
    });
    renderWithProviders(<QueuePage />);
    const confirm = await screen.findByRole("button", { name: /Potwierdź \(A\)/ });
    await waitFor(() => expect(confirm).toBeEnabled());
    expect(confirm).not.toHaveAttribute("title");
  });

  it("decision card: caveats from top_candidates[0] when there is no best pair, and frame-level pointers", () => {
    const meta: AssignmentMeta = {
      top_candidates: [{
        campaign_id: "C-a", support: 9, pair: ["i9", "i1"], contradictions: 1, missing_families: ["INF"],
        contradiction_list: [{ kind: "scheme", family: "NAR", bits: -3, explanation_pl: "Inny typ oszustwa" }],
        evidence: [{ family: "VIS", kind: "pdq", value: "ff", bits: 12, evidence_a: { timecode_s: 3.5 }, evidence_b: { timecode_s: 0, bbox: null }, refined: true }],
      }],
      link_threshold: 10, link_rule_met: false, margin_bits: 9, missing_bits_to_link: 1,
    } as AssignmentMeta;
    render(<DecisionRule meta={meta} />);
    expect(screen.getByTestId("assignment-caveats")).toHaveTextContent("Inny typ oszustwa");
    expect(screen.getByTestId("assignment-caveats")).toHaveTextContent("infrastruktura");
    const ptr = screen.getByTestId("evidence-pointers");
    expect(ptr).toHaveTextContent("i9 t=0:03,5");
    expect(ptr).toHaveTextContent("dokładniejsze wskazanie");
  });
});
