import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { DocsTab } from "./DocsTab";
import { MINI_OPENAPI } from "./console/__fixtures__/openapi";
import { DOC_SECTIONS, sectionText } from "./docs/docsContent";

function LocationProbe() {
  const loc = useLocation();
  return <output data-testid="loc">{loc.search}</output>;
}

function setup() {
  mockFetch(async (input) => {
    const url = String(input);
    if (url.endsWith("/api/openapi.json")) return jsonResponse(MINI_OPENAPI);
    if (url.endsWith("/api/v1/integrations"))
      return jsonResponse({
        connectors: [
          { id: "misp", name: "MISP", level: "export-format", auth: "api_key", setup_pl: ["Pobierz plik."], data_out: ["domain", "url"], rate_limit_pl: "brak", licence_pl: "AGPL serwer; format otwarty" },
        ],
        categories: [],
      });
    if (url.endsWith("/api/v1/integrations/webhooks"))
      return jsonResponse({ event_types: [{ id: "delta", label_pl: "Zmiana", description_pl: "Nowe przypisanie", example: { seq: 1 } }] });
    return jsonResponse({}, { status: 404 });
  });
  renderWithProviders(
    <>
      <DocsTab />
      <LocationProbe />
    </>,
    { route: "/integrations?tab=docs" },
  );
}

describe("DocsTab", () => {
  it("renders every section with a TOC anchor", async () => {
    setup();
    for (const s of DOC_SECTIONS) {
      const link = screen.getByTestId(`toc-${s.id}`);
      expect(link).toHaveAttribute("href", `#doc-${s.id}`);
      expect(document.getElementById(`doc-${s.id}`)).not.toBeNull();
    }
  });

  it("search filters sections (HMAC → Webhooki)", async () => {
    setup();
    await userEvent.type(screen.getByTestId("docs-search"), "HMAC");
    expect(screen.getByTestId("doc-section-webhooki")).toBeInTheDocument();
    expect(screen.queryByTestId("doc-section-strumien")).not.toBeInTheDocument();
    expect(screen.getByTestId("docs-count")).toHaveTextContent(/Pasujące sekcje: \d+ z 9/);
  });

  it("search is diacritics-insensitive", async () => {
    setup();
    await userEvent.type(screen.getByTestId("docs-search"), "prywatnosc");
    expect(screen.getByTestId("doc-section-prywatnosc")).toBeInTheDocument();
  });

  it("copy buttons copy the code block", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    setup();
    const block = within(screen.getByTestId("doc-section-strumien")).getAllByTestId("doc-code")[0]!;
    await userEvent.click(within(block).getByRole("button"));
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining("curl -N"));
    expect(await within(block).findByText("Skopiowano")).toBeInTheDocument();
  });

  it("generates the API reference and opens an endpoint in the console", async () => {
    setup();
    const ref = await screen.findByTestId("api-reference");
    expect(within(ref).getAllByTestId("api-endpoint")).toHaveLength(6);
    await userEvent.click(within(ref).getAllByRole("button", { name: "Otwórz w konsoli" })[0]!);
    await waitFor(() => expect(screen.getByTestId("loc").textContent).toMatch(/tab=console&op=health_health_get/));
  });

  it("renders connector guides and webhook events from the API", async () => {
    setup();
    expect(await screen.findByTestId("connector-guides")).toHaveTextContent("MISP");
    expect(await screen.findByTestId("webhook-events")).toHaveTextContent("Nowe przypisanie");
  });

  it("docs content mentions the verified CLI and env names", () => {
    const all = DOC_SECTIONS.map(sectionText).join("\n");
    for (const s of ["hydra verify-export", "HYDRA_OFFLINE=1", "HYDRA_ENRICH=1", "Last-Event-ID", "X-Hydra-Signature", "META_AD_LIBRARY_TOKEN", "/v1/exports/signing-key"]) {
      expect(all).toContain(s);
    }
  });
});
