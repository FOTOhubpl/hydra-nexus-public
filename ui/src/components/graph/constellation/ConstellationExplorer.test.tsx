import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../../test/utils";
import { GraphTaskPage } from "../../../pages/task/GraphTaskPage";

// sigma's WebGL programs reference these constructors at import time; jsdom has none
vi.hoisted(() => {
  const g = globalThis as Record<string, unknown>;
  g.WebGL2RenderingContext ??= class {};
  g.WebGLRenderingContext ??= class {};
});

const notFound = () => new Response(JSON.stringify({ detail: "Not Found" }), { status: 404, headers: { "Content-Type": "application/json" } });

describe("constellation explorer", () => {
  it("explains an empty run (legacy fallback after the overview 404)", async () => {
    const seen: string[] = [];
    mockFetch(async (input) => {
      const url = String(input);
      seen.push(url);
      if (url.includes("/v1/graph/overview")) return notFound();
      if (url.includes("/v1/graph/explore")) return jsonResponse({ run_id: null, nodes: [], edges: [], campaigns: [], groups: [], stats: null });
      return jsonResponse({});
    });
    renderWithProviders(<GraphTaskPage />, { route: "/graph" });
    expect(await screen.findByText("Brak przebiegu analizy")).toBeInTheDocument();
    expect(seen.some((u) => u.includes("/v1/graph/overview"))).toBe(true);
    expect(seen.some((u) => u.includes("/v1/graph/explore"))).toBe(true);
  });

  it("reads only the overview when the level-of-detail endpoint exists, and explains missing WebGL", async () => {
    const seen: string[] = [];
    mockFetch(async (input) => {
      const url = String(input);
      seen.push(url);
      if (url.includes("/v1/graph/overview")) return jsonResponse({ run_id: "r1", campaigns: [{ id: "C1", label: "one", size: 3, x: 0, y: 0, r: 1 }], groups: [], links: [], stats: null });
      return jsonResponse({});
    });
    renderWithProviders(<GraphTaskPage />, { route: "/graph" });
    // jsdom has no WebGL2: the explorer must say so instead of drawing nothing
    expect(await screen.findByText("WebGL2 niedostępny")).toBeInTheDocument();
    expect(seen.some((u) => u.includes("/v1/graph/explore"))).toBe(false);
  });

  it("reports a failing server instead of an empty graph", async () => {
    mockFetch(async () => new Response("boom", { status: 500 }));
    renderWithProviders(<GraphTaskPage />, { route: "/graph" });
    expect(await screen.findByText("Nie udało się pobrać grafu")).toBeInTheDocument();
  });
});
