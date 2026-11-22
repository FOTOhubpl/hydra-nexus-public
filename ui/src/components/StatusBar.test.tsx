import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HealthIndicator } from "./StatusBar";
import { jsonResponse, mockFetch, renderWithProviders } from "../test/utils";

describe("HealthIndicator", () => {
  it("shows status, version, device and offline flag from /health", async () => {
    mockFetch(async () => jsonResponse({ status: "ok", version: "1.2.3", device: "cpu", offline: true }));
    renderWithProviders(<HealthIndicator />);
    expect(await screen.findByText("backend działa")).toBeInTheDocument();
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(screen.getByText("1.2.3")).toBeInTheDocument();
    expect(screen.getByText("cpu")).toBeInTheDocument();
    expect(screen.getByText("tryb offline")).toBeInTheDocument();
  });

  it("omits fields the backend does not send", async () => {
    mockFetch(async () => jsonResponse({ status: "ok" }));
    renderWithProviders(<HealthIndicator />);
    expect(await screen.findByText("backend działa")).toBeInTheDocument();
    expect(screen.queryByText("wersja")).toBeNull();
    expect(screen.queryByText("urządzenie")).toBeNull();
  });

  it("degrades to 'backend offline' when unreachable", async () => {
    mockFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    renderWithProviders(<HealthIndicator />);
    expect(await screen.findByText("backend niedostępny")).toBeInTheDocument();
  });
});
