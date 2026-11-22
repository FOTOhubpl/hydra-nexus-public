import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../test/utils";
import { ReferenceGalleryPanel } from "./ReferenceGalleryPanel";

afterEach(() => vi.unstubAllGlobals());

describe("GALLERY: reference base panel", () => {
  it("shows counts and the explanation, starts a build and a re-analysis", async () => {
    const calls: string[] = [];
    mockFetch(async (input, init) => {
      const url = String(input);
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url.includes("/v1/reference/status"))
        return jsonResponse({ face_entries: 2, logo_templates: 1, built: null, explain_pl: "Baza zostaje lokalnie.", job: null });
      if (url.includes("/v1/reference/build")) return jsonResponse({ job_id: "j1", already_running: false }, { status: 202 });
      if (url.includes("/v1/reference/reanalyse")) return jsonResponse({ job_id: "a1", mode: "corpus" }, { status: 202 });
      if (url.includes("/v1/analyse/a1")) return jsonResponse({ job: { status: "done" } });
      return jsonResponse({});
    });
    renderWithProviders(<ReferenceGalleryPanel />);
    expect(await screen.findByText("Baza zostaje lokalnie.")).toBeInTheDocument();
    expect(screen.getByTestId("reference-counts").textContent).toMatch(/twarzy 2, wzorców logo 1/);
    fireEvent.click(screen.getByText("Zbuduj galerię referencyjną z zaimportowanych materiałów"));
    fireEvent.click(screen.getByText("Przelicz analizę"));
    expect(await screen.findByText(/geny ID\/logo są w wynikach/)).toBeInTheDocument();
    expect(calls.some((c) => c.startsWith("POST") && c.includes("/v1/reference/build"))).toBe(true);
    expect(calls.some((c) => c.startsWith("POST") && c.includes("/v1/reference/reanalyse"))).toBe(true);
  });
});
