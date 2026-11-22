import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../test/utils";
import { SettingsPage } from "./SettingsPage";

afterEach(() => vi.unstubAllGlobals());

describe("B17: Ustawienia written for an analyst", () => {
  it("summarises the server state in plain Polish and keeps proxy/endpoint detail in a collapsed section", async () => {
    mockFetch(async () => jsonResponse({ status: "ok", version: "1.2.3" }));
    renderWithProviders(<SettingsPage />);
    expect(await screen.findByText("Serwer HYDRA działa (wersja 1.2.3)")).toBeInTheDocument();
    expect(screen.getByText(/działa w całości na tym komputerze/)).toBeInTheDocument();
    const details = screen.getByTestId("tech-details");
    expect(details.tagName).toBe("DETAILS");
    expect(details).not.toHaveAttribute("open");
    expect(within(details).getByText("Szczegóły techniczne")).toBeInTheDocument();
    expect(within(details).getByText(/127\.0\.0\.1:8000/)).toBeInTheDocument();
    // developer wording stays out of the visible summary and the field help
    const summary = screen.getByTestId("server-summary");
    expect(summary.textContent).not.toMatch(/Vite|proxy|\/health/);
    expect(screen.queryByText(/ForceAtlas2|GET \/v1\/jobs/)).toBeNull();
  });
});
