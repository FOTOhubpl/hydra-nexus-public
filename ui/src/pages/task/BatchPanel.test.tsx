/** B3 batch panel: new patterns shared by several new items, duplicates. Synthetic fixtures. */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { BatchPanel, BatchReportView } from "./BatchPanel";

const report = {
  batch_run: "assign_batch-1", base_run: "analyse-1", job_id: "j1",
  summary: { items: 4, new_campaign_items: 4, new_patterns: 2, multi_member_new_patterns: 1, items_in_multi_member_patterns: 3, duplicates: 1, pairs_scored: 6 },
  new_patterns: [
    { id: "N-aaaa1111", members: ["new-1", "new-2", "new-3"], items: ["x1", "x2", "x3"], size: 3, links: [
      { a: "new-1", b: "new-2", hypothesis: "same_campaign", total_bits: 21.5, support: 21.5, reasons: [{ family: "INF", kind: "host", bits: 9.1, explanation_pl: "Host strony: „fresh.example” — 3 incydenty" }] },
    ], reason_pl: "Nowy wzorzec N-aaaa1111: 3 nowe materiały z tej partii łączą dowody." },
    { id: "N-bbbb2222", members: ["new-4"], items: ["x4"], size: 1, links: [], reason_pl: "Nowy wzorzec N-bbbb2222." },
  ],
  items: [
    { item: "x1", incident_id: "new-1", decision: "new_campaign", new_pattern_id: "N-aaaa1111" },
    { item: "x2", incident_id: "new-2", decision: "new_campaign", new_pattern_id: "N-aaaa1111" },
    { item: "x6", incident_id: "new-6", decision: "new_campaign", new_pattern_id: "N-aaaa1111", duplicate_of: "new-1", duplicate_of_item: "x1" },
    { item: "x5", incident_id: "new-5", decision: "assign", campaign_id: "C-1" },
  ],
  note_pl: "Do znanej kampanii nic nie jest dołączane bez dowodów.",
};

describe("BatchReportView", () => {
  it("shows the shared new pattern with its reasons and the duplicate", () => {
    renderWithProviders(<BatchReportView report={report} />);
    expect(screen.getByText("Nowe wzorce w tej partii")).toBeInTheDocument();
    expect(screen.getAllByText("N-aaaa1111").length).toBeGreaterThan(1);
    expect(screen.getByText(/3 materiały: x1, x2, x3/)).toBeInTheDocument();
    expect(screen.getByText(/Host strony: „fresh.example”/)).toBeInTheDocument();
    expect(screen.getByText("x1", { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByText("N-bbbb2222")).not.toBeInTheDocument(); // singleton patterns are not listed as shared
  });
});

describe("BatchPanel", () => {
  it("posts the server folder and polls the report", async () => {
    const fetch = mockFetch(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/assign-batch") && init?.method === "POST") return jsonResponse({ job_id: "j1", items: 4 }, { status: 202 });
      if (url.includes("/v1/assign-batch/j1")) return jsonResponse({ job: { id: "j1", status: "done", total: 4, done: 4 }, report });
      return jsonResponse({}, { status: 404 });
    });
    renderWithProviders(<BatchPanel />);
    fireEvent.change(screen.getByLabelText("Folder partii"), { target: { value: "/data/nowe" } });
    fireEvent.click(screen.getByText("Analizuj partię"));
    await waitFor(() => expect(screen.getByText("Nowe wzorce w tej partii")).toBeInTheDocument());
    const post = fetch.mock.calls.find(([, i]) => i?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ path: "/data/nowe" });
  });
});
