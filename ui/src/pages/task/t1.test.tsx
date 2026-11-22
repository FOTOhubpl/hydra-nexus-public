import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CommandPalette } from "../../components/CommandPalette";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { flatten } from "./MethodologyPage";
import { QueuePage } from "./QueuePage";

describe("T1", () => {
  it("opens the command palette with Cmd+K and lists screens", async () => {
    mockFetch(async () => jsonResponse({ q: "", run_id: null, incidents: [], campaigns: [], genes: [] }));
    renderWithProviders(<CommandPalette />);
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(await screen.findByRole("dialog", { name: "Paleta poleceń" })).toBeInTheDocument();
    expect(screen.getByText("Kolejka analityka")).toBeInTheDocument();
  });

  it("queue: A writes an accept_link review for the current pair", async () => {
    const posts: unknown[] = [];
    const pairRow = { a: "i1", b: "i2", probability: 0.5, total_bits: 5, hypothesis: "abstain", calibrated: false, model: "m", family_bits: { VIS: 5 }, missing_families: [], dominant_family: "VIS", review: null };
    const item = { key: "i1|i2", kinds: ["near_threshold"], a: "i1", b: "i2", incident_id: null, campaigns: ["c1"], bits: 5, distance_bits: -5, impact: 2, uncertainty: 0.9, ev: 1.8, reason_pl: "suma bitów blisko progu" };
    mockFetch(async (input, init) => {
      const url = String(input);
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        return jsonResponse({ review: { id: "r1", action: "accept_link", target: {}, analyst: "operator", created_at: "x" }, event_hash: "h" }, { status: 201 });
      }
      if (url.includes("/v1/review/queue")) return jsonResponse({ run_id: "r", threshold_bits: 10, method_pl: "Kolejka heurystyczna.", items: [item], skipped: { decided: 0, unsure: 0, decided_between_campaigns: 0 }, unsure: [] });
      return jsonResponse({ run_id: "r", score: { ...pairRow, reasons: [], contradictions: [], unknown_families: [] }, dominant_family: "VIS", incident_a: null, incident_b: null, same_campaign_in_run: false, review: null, reviews: [] });
    });
    renderWithProviders(<QueuePage />);
    expect((await screen.findAllByText("i1 ↔ i2")).length).toBeGreaterThan(0);
    await screen.findByTestId("waterfall");
    fireEvent.keyDown(window, { key: "a" });
    await waitFor(() => expect(posts).toEqual([{ action: "accept_link", target: { a: "i1", b: "i2" }, note: "kolejka analityka" }]));
  });

  it("flattens EVAL metric blocks to dotted columns", () => {
    expect(flatten({ campaign_pairwise: { f1: 0.7, fp: 1 }, threshold: 10 })).toEqual({ "campaign_pairwise.f1": 0.7, "campaign_pairwise.fp": 1, threshold: 10 });
  });
});
