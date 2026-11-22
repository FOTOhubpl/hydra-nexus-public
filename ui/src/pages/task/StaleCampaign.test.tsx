import { render, screen } from "@testing-library/react";
import { Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { ApiErrorState } from "../../components/ApiErrorState";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { isStaleCampaignError } from "./CampaignCardPage";
import { bestSuccessor, StaleCampaign, StaleRedirectNote } from "./StaleCampaign";

afterEach(() => vi.unstubAllGlobals());

const notFound = (detail: string) => new ApiError({ kind: "http", status: 404, message: detail, path: "/v1/campaigns/C-old", body: { detail } });

describe("B3: resource 404 vs unknown route", () => {
  it("a campaign 404 is a missing resource, a FastAPI 'Not Found' is a missing route", () => {
    expect(notFound("campaign not found in this run").isResourceNotFound).toBe(true);
    expect(notFound("Not Found").isResourceNotFound).toBe(false);
    expect(notFound("unknown API path").isResourceNotFound).toBe(false);
    expect(isStaleCampaignError(notFound("campaign not found in this run"))).toBe(true);
    expect(isStaleCampaignError(notFound("no analysis run yet"))).toBe(false);
  });

  it("ApiErrorState says the object is missing in Polish instead of 'endpoint not available'", () => {
    render(<ApiErrorState error={notFound("campaign not found in this run")} resource="kartę kampanii" />);
    expect(screen.getByText(/Ta kampania nie istnieje w bieżącym przebiegu \(identyfikatory zmieniają się po ponownej analizie\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Punkt końcowy API jeszcze niedostępny/)).toBeNull();
  });

  it("bestSuccessor picks the current campaign holding most old members", () => {
    const cs = [
      { id: "C-a", label: "a", members: ["i1", "x"] },
      { id: "C-b", label: "b", members: ["i2", "i3", "i4"] },
    ];
    expect(bestSuccessor(["i1", "i2", "i3"], cs)).toEqual({ id: "C-b", label: "b", overlap: 2 });
    expect(bestSuccessor(["z"], cs)).toBeNull();
  });
});

function Target() {
  const loc = useLocation();
  return <div data-testid="target">{loc.pathname}<StaleRedirectNote /></div>;
}

describe("StaleCampaign", () => {
  it("redirects to the campaign that now holds the old members", async () => {
    mockFetch(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/v1/campaigns?")) return jsonResponse({ items: [{ id: "C-new", label: "n", members: ["i1", "i2", "i9"] }], total: 1, limit: 2000, offset: 0, run_id: "run-2" });
      if (url.startsWith("/api/v1/runs")) return jsonResponse({ items: [{ id: "run-2" }, { id: "run-1" }] });
      if (url.includes("/v1/campaigns/C-old?run_id=run-1")) return jsonResponse({ id: "C-old", label: "stara", members: ["i1", "i2"] });
      return jsonResponse({ detail: "campaign not found in this run" }, { status: 404 });
    });
    renderWithProviders(
      <Routes>
        <Route path="/campaigns/C-old" element={<StaleCampaign id="C-old" />} />
        <Route path="/campaigns/:id" element={<Target />} />
      </Routes>,
      { route: "/campaigns/C-old" },
    );
    expect(await screen.findByTestId("target")).toHaveTextContent("/campaigns/C-new");
    expect(screen.getByTestId("stale-redirect-note")).toHaveTextContent("zawiera 2 z 2 jej incydentów");
  });

  it("without a successor explains the id change and offers both actions", async () => {
    mockFetch(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/v1/campaigns?")) return jsonResponse({ items: [], total: 0, limit: 2000, offset: 0, run_id: "run-2" });
      if (url.startsWith("/api/v1/runs")) return jsonResponse({ items: [{ id: "run-2" }] });
      return jsonResponse({ detail: "campaign not found in this run" }, { status: 404 });
    });
    renderWithProviders(<StaleCampaign id="C-gone" />);
    expect(await screen.findByText("Ta kampania nie istnieje w bieżącym przebiegu")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Szukaj po incydentach tej kampanii" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Przejdź do listy kampanii" })).toHaveAttribute("href", "/campaigns");
  });
});
