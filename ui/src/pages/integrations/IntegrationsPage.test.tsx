import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { IntegrationsPage, OFFLINE_BANNER } from "./IntegrationsPage";
import { index, webhooks } from "./hub/testData";

vi.mock("./ConsoleTab", () => ({ ConsoleTab: () => <div>konsola</div> }));
vi.mock("./DocsTab", () => ({ DocsTab: () => <div>dokumentacja</div> }));

afterEach(() => vi.unstubAllGlobals());

function setup(route = "/integrations") {
  mockFetch(async (input) => {
    const url = String(input);
    if (url === "/api/v1/integrations") return jsonResponse(index());
    if (url === "/api/v1/integrations/webhooks") return jsonResponse(webhooks());
    if (url.includes("/deliveries")) return jsonResponse({ deliveries: [] });
    if (url.includes("/sink")) return jsonResponse({ receipts: [] });
    return jsonResponse({}, { status: 404 });
  });
  return renderWithProviders(<IntegrationsPage />, { route });
}

describe("IntegrationsPage", () => {
  it("has an accessible tablist with roving tabindex and arrow-key navigation", async () => {
    setup();
    expect(screen.getByRole("heading", { name: "Integracje & API" })).toBeInTheDocument();
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Centrum integracji", "Konsola API", "Webhooki", "Dokumentacja"]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(tabs[0]).toHaveAttribute("tabindex", "0");
    expect(tabs[1]).toHaveAttribute("tabindex", "-1");
    expect(tabs[0]).toHaveAttribute("aria-controls", "integ-panel-hub");
    expect(screen.getByRole("tabpanel")).toHaveAttribute("id", "integ-panel-hub");

    tabs[0]!.focus();
    fireEvent.keyDown(tabs[0]!, { key: "ArrowRight" });
    const console = screen.getByRole("tab", { name: "Konsola API" });
    expect(console).toHaveAttribute("aria-selected", "true");
    expect(console).toHaveFocus();
    expect(screen.getByText("konsola")).toBeInTheDocument();
    fireEvent.keyDown(console, { key: "End" });
    expect(screen.getByRole("tab", { name: "Dokumentacja" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("tab", { name: "Dokumentacja" }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Centrum integracji" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Centrum integracji" }), { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "Dokumentacja" })).toHaveAttribute("aria-selected", "true");
  });

  it("opens the tab from ?tab= and shows the offline banner", async () => {
    setup("/integrations?tab=webhooks");
    expect(screen.getByRole("tab", { name: "Webhooki" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("offline-banner")).toHaveTextContent(OFFLINE_BANNER);
    expect(await screen.findByTestId("webhooks-tab")).toBeInTheDocument();
  });
});
