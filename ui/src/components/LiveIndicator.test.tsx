import { act, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { liveStatus } from "../api/stream";
import { DashboardPage } from "../pages/task/DashboardPage";
import { jsonResponse, mockFetch, renderWithProviders } from "../test/utils";
import { LiveIndicator } from "./StatusBar";

// sigma needs WebGL, which jsdom lacks; the dashboard graph tile is not under test here
vi.mock("./RelationGraph", () => ({ RelationGraph: () => null }));

describe("LiveIndicator", () => {
  afterEach(() => liveStatus.set("off"));

  it("shows the stream state as text in a polite status region", () => {
    renderWithProviders(<LiveIndicator />);
    expect(screen.queryByTestId("live-status")).toBeNull();
    act(() => liveStatus.set("connecting"));
    const el = screen.getByRole("status");
    expect(el).toHaveAttribute("aria-live", "polite");
    expect(el).toHaveTextContent("łączenie…");
    // pulsing only without prefers-reduced-motion
    expect(el.querySelector("[aria-hidden]")!.className).toContain("motion-safe:animate-pulse");
    act(() => liveStatus.set("live"));
    expect(screen.getByRole("status")).toHaveTextContent("na żywo");
    expect(screen.getByRole("status").querySelector("[aria-hidden]")!.className).not.toContain("animate");
    act(() => liveStatus.set("polling"));
    expect(screen.getByRole("status")).toHaveTextContent("odświeżanie co 15 s");
  });
});

describe("dashboard new material", () => {
  it("marks new previews and shows +N nowe next to campaigns", async () => {
    const dashboard = {
      run: { id: "r1", created_at: null, finished_at: null, alerts: [] },
      kpis: { incidents: 3, artifacts: 3, artifacts_by_role: {}, genes: 0, genes_by_family: {}, campaigns: 1, multi_member_campaigns: 1, groups: 0, assignments: 1, enriched_incidents: 0 },
      previews: [
        { id: "inc-1", title: "a", observed_at: null, thumbnail: null, scheme: null, campaign_id: "cmp-1", video: null },
        { id: "inc-9", title: "n", observed_at: null, thumbnail: null, scheme: null, campaign_id: "cmp-1", video: null, new: true, decision: "assign" },
      ],
      campaigns: [{ id: "cmp-1", label: "C1", size: 3, new: 1, confidence: 1, group_id: null, members: [{ id: "inc-1", thumbnail: null }, { id: "inc-9", thumbnail: null, new: true }], pivots: [] }],
      groups: [], coverage: [], jobs: [],
      live: { run_id: "r1", version: 1, seq: 5, new_incidents: 1, by_decision: { assign: 1 } },
    };
    mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/v1/dashboard")) return jsonResponse(dashboard);
      if (url.includes("/v1/graph")) return jsonResponse({ run_id: null, nodes: [], edges: [], stats: { nodes: 0, edges: 0, edges_total: 0 } });
      return jsonResponse({});
    });
    renderWithProviders(<DashboardPage />);
    expect(await screen.findByText("+1 nowe")).toBeInTheDocument();
    expect(screen.getAllByText("nowe od analizy")).toHaveLength(1);
  });
});
