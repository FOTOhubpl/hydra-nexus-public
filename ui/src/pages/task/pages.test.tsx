import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { describe, expect, it } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { CampaignsPage } from "./CampaignsPage";
import { PairPage } from "./PairPage";
import { StartImport } from "./common";

describe("task pages", () => {
  it("shows an honest empty state when no run exists", async () => {
    mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/v1/runs/latest")) return jsonResponse({ run: null, counts: { incidents: 0 }, contracts_version: "2.1.0" });
      return jsonResponse({ items: [], total: 0, limit: 2000, offset: 0, run_id: null });
    });
    renderWithProviders(<CampaignsPage />);
    expect(await screen.findByText("Brak przebiegu analizy")).toBeInTheDocument();
  });

  it("guides a clean state to the dataset import (JURY-READY)", () => {
    renderWithProviders(<StartImport />);
    expect(screen.getByText("Pusta baza: zaimportuj własny zbiór incydentów")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Nowa analiza: plik, folder lub ZIP/ })).toHaveAttribute("href", "/analyse?tab=files");
    expect(screen.getByRole("link", { name: /Analiza folderu/ })).toHaveAttribute("href", "/analyse?tab=path");
  });

  it("renders the pair waterfall with unknown families and the contradiction", async () => {
    const side = (id: string) => ({ id, source: "s", source_ref: id, title: `T ${id}`, observed_at: null, meta: {}, artifact_roles: {}, artifact_count: 0, observed_genes: {}, thumbnail: null, campaign_id: "c1", visuals: [], landing_title: null, hosts: ["x.example"] });
    mockFetch(async () =>
      jsonResponse({
        run_id: "r1",
        score: {
          a: "i1", b: "i2", family_bits: { VIS: 9, INF: -3 }, missing_families: ["ID"], unknown_families: ["TXT", "ID", "NAR", "META"],
          contradictions: [{ family: "INF", kind: "domain", value_a: "a.example", value_b: "b.example", bits: -3, provenance: "OBS", explanation_pl: "Różne domeny docelowe" }],
          reasons: [], total_bits: 6, probability: 0.88, calibrated: false, hypothesis: "same_campaign", model: "rarity_bits@1",
        },
        dominant_family: "VIS", incident_a: side("i1"), incident_b: side("i2"), same_campaign_in_run: true, review: null, reviews: [],
      }),
    );
    renderWithProviders(
      <Routes><Route path="/pair/:a/:b" element={<PairPage />} /></Routes>,
      { route: "/pair/i1/i2" },
    );
    expect(await screen.findByTestId("waterfall")).toBeInTheDocument();
    expect(screen.getAllByText("nieznane").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("Różne domeny docelowe")).toBeInTheDocument();
    expect(screen.getAllByText(/nieskalibrowany/, { selector: "div" })[0]).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Potwierdź powiązanie/ })).toBeInTheDocument();
    expect(screen.getByTestId("pair-lab-link-a")).toHaveAttribute("href", "/lab/i1");
    expect(screen.getByTestId("pair-lab-link-b")).toHaveAttribute("href", "/lab/i2");
  });
});
