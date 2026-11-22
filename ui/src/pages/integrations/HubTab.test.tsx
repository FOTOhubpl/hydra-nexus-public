import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { HubTab } from "./HubTab";
import { connector, index } from "./hub/testData";

afterEach(() => vi.unstubAllGlobals());

function route(handlers: Record<string, (init?: RequestInit) => unknown>) {
  return mockFetch(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    for (const [k, h] of Object.entries(handlers)) {
      const [m, p] = k.split(" ");
      if (m === method && url.split("?")[0] === `/api${p}`) return jsonResponse(h(init));
    }
    return jsonResponse({ detail: "nie ma" }, { status: 404 });
  });
}

describe("HubTab", () => {
  it("renders honest level and status pills; planned has no connect button", async () => {
    route({ "GET /v1/integrations": () => index() });
    renderWithProviders(<HubTab />);
    const vt = await screen.findByTestId("connector-virustotal");
    expect(within(vt).getByTestId("level-virustotal")).toHaveTextContent("Na żywo (live)");
    expect(within(vt).getByTestId("status-virustotal")).toHaveTextContent("Nieskonfigurowane");
    expect(within(vt).getByRole("button", { name: /Połącz/ })).toBeInTheDocument();
    expect(within(vt).getByTestId("live-note-virustotal")).toHaveTextContent("zaimplementowany; testowany na fiksturach z dokumentacji, bez wywołań produkcyjnych");

    const misp = screen.getByTestId("connector-misp");
    expect(within(misp).getByTestId("level-misp")).toHaveTextContent("Format eksportu");
    expect(within(misp).getByTestId("status-misp")).toHaveTextContent("Skonfigurowane");
    expect(within(misp).getByRole("button", { name: /Pobierz pakiet/ })).toBeInTheDocument();
    expect(within(misp).queryByRole("button", { name: /Połącz/ })).toBeNull();

    const tf = screen.getByTestId("connector-threatfox");
    expect(within(tf).getByTestId("level-threatfox")).toHaveTextContent("Planowane");
    expect(within(tf).queryByRole("button", { name: /Połącz/ })).toBeNull();
    expect(within(tf).queryByRole("button", { name: /Pobierz/ })).toBeNull();
    expect(within(tf).getByRole("button", { name: "Opis planowanej integracji" })).toBeInTheDocument();
    expect(within(tf).getByRole("switch")).toBeDisabled();
    expect(screen.queryByTestId("live-note-misp")).toBeNull();

    expect(screen.getByTestId("kpi-live")).toHaveTextContent("1");
    expect(screen.getByTestId("kpi-planned")).toHaveTextContent("1");
  });

  it("filters by category chip, level and search", async () => {
    route({ "GET /v1/integrations": () => index() });
    renderWithProviders(<HubTab />);
    await screen.findByTestId("connector-virustotal");
    fireEvent.click(screen.getByRole("button", { name: "Udostępnianie" }));
    expect(screen.queryByTestId("connector-virustotal")).toBeNull();
    expect(screen.getByTestId("connector-misp")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Wszystkie" }));
    fireEvent.change(screen.getByTestId("hub-level"), { target: { value: "planned" } });
    expect(screen.getByTestId("connector-threatfox")).toBeInTheDocument();
    expect(screen.queryByTestId("connector-misp")).toBeNull();
    fireEvent.change(screen.getByTestId("hub-level"), { target: { value: "" } });
    fireEvent.change(screen.getByTestId("hub-search"), { target: { value: "virus" } });
    expect(screen.getByTestId("connector-virustotal")).toBeInTheDocument();
    expect(screen.queryByTestId("connector-threatfox")).toBeNull();
  });

  it("connect flow posts the secret, shows the steps and clears the input; nothing lands in localStorage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    let body: unknown = null;
    const fetchMock = route({
      "GET /v1/integrations": () => index(),
      "POST /v1/integrations/virustotal/connect": (init) => {
        body = JSON.parse(String(init?.body));
        return {
          ok: false,
          steps: [
            { id: "secret", ok: true, message_pl: "Klucz zapisany." },
            { id: "test", ok: false, status: "offline", message_pl: "Test zablokowany: tryb offline." },
            { id: "enable", ok: true, message_pl: "Włączono." },
          ],
          connector: connector({ id: "virustotal", status: "offline" }),
        };
      },
    });
    renderWithProviders(<HubTab />);
    fireEvent.click(await screen.findByTestId("connect-virustotal"));
    const form = await screen.findByTestId("card-virustotal-keyform");
    expect(within(form).getByTestId("key-callout-virustotal")).toHaveTextContent("Wymaga klucza API");
    const input = within(form).getByTestId("card-virustotal-input-VT_API_KEY") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.autocomplete).toBe("off");
    fireEvent.change(input, { target: { value: "sekret-1234567890abcd" } });
    fireEvent.click(within(form).getByTestId("card-virustotal-toggle-VT_API_KEY"));
    expect(input.type).toBe("text");
    fireEvent.click(within(form).getByTestId("card-virustotal-submit"));
    await waitFor(() => expect(within(form).getByTestId("card-virustotal-result")).toHaveAttribute("data-kind", "offline"));
    expect(body).toEqual({ secrets: { VT_API_KEY: "sekret-1234567890abcd" } });
    expect(input.value).toBe("");
    expect(input.type).toBe("password");
    expect(within(form).getByTestId("card-virustotal-result")).toHaveTextContent("Klucz zapisany: test jest zablokowany w trybie offline");
    for (const call of setItem.mock.calls) expect(String(call[1])).not.toContain("sekret-1234567890abcd");
    for (const k of Object.keys(window.localStorage)) expect(window.localStorage.getItem(k)).not.toContain("sekret-1234567890abcd");
    // the key is only in the one POST body, never in a URL
    for (const [url] of fetchMock.mock.calls) expect(String(url)).not.toContain("sekret");
    setItem.mockRestore();
  });

  it("opens the drawer with data-out chips, never-sent text and a dry-run preview", async () => {
    route({
      "GET /v1/integrations": () => index(),
      "GET /v1/integrations/misp": () => ({ ...index().connectors[1], audit: [{ seq: 7, ts: "2026-11-22T01:00:00Z", type: "INTEGRATION_EXPORT", payload: { format: "misp" } }], runs: [] }),
      "POST /v1/integrations/misp/preview": () => ({
        connector: "misp", format: "misp", method: "GET", destination: "plik", headers: {}, content_type: "application/json",
        bytes: 120, sha256: "ab".repeat(32), fields_sent: ["domain"], redactions: [], payload: { Event: { info: "HYDRA" } },
      }),
    });
    renderWithProviders(<HubTab />);
    fireEvent.click(await screen.findByTestId("open-misp"));
    const drawer = await screen.findByTestId("connector-drawer");
    expect(await within(drawer).findByText("Artefakty.")).toBeInTheDocument();
    expect(within(drawer).getByText("INTEGRATION_EXPORT")).toBeInTheDocument();
    fireEvent.click(within(drawer).getByTestId("preview-button"));
    expect(await within(drawer).findByTestId("preview-payload")).toHaveTextContent('"info": "HYDRA"');
    expect(within(drawer).getByText("ab".repeat(32))).toBeInTheDocument();
  });

  it("preset card: summary line, 'Uzupełnij klucze (N)', inline key form, auto re-apply after the last key", async () => {
    let applied = 0;
    let saved = false;
    const vt = (configured: boolean) => connector({ id: "virustotal", name: "VirusTotal", status: configured ? "offline" : "not_configured", secrets: [{ name: "VT_API_KEY", configured, source: configured ? "file" : null, last4: configured ? "abcd" : null }] });
    const data = () => index({
      presets: [{ id: "reputation", name_pl: "Pakiet reputacji", description_pl: "Reputacja.", connectors: ["virustotal", "misp", "threatfox"] }],
      connectors: [vt(saved), index().connectors[1]!, index().connectors[2]!],
    });
    route({
      "GET /v1/integrations": () => data(),
      "POST /v1/integrations/virustotal/connect": () => {
        saved = true;
        return { ok: true, steps: [{ id: "secret", ok: true, message_pl: "Klucz zapisany." }, { id: "test", ok: true, message_pl: "OK." }, { id: "enable", ok: true, message_pl: "Konektor włączony." }], connector: vt(true) };
      },
      "POST /v1/integrations/presets/reputation/apply": () => {
        applied += 1;
        return { preset: "reputation", results: [
          { connector: "virustotal", ok: true, status: "configured", message_pl: "x", steps_pl: ["Test udany (zapytania na żywo włączone).", "Konektor włączony."] },
          { connector: "misp", ok: true, status: "configured", message_pl: "Format pliku: stix21.; Konektor włączony." },
          { connector: "threatfox", ok: false, status: "planned", message_pl: "Konektor planowany: pominięty." },
        ] };
      },
    });
    renderWithProviders(<HubTab />);
    const card = await screen.findByTestId("preset-reputation");
    expect(within(card).getByTestId("preset-summary-reputation")).toHaveTextContent("1 z 3 gotowe, 1 wymaga klucza, 1 planowane");
    expect(within(card).getByTestId("preset-state-reputation-virustotal")).toHaveTextContent("Wymaga klucza");
    expect(within(card).getByTestId("preset-state-reputation-threatfox")).toHaveTextContent("Planowany");
    const btn = within(card).getByTestId("apply-reputation");
    expect(btn).toHaveTextContent("Uzupełnij klucze (1)");
    fireEvent.click(btn);
    const form = await within(card).findByTestId("preset-reputation-virustotal-keyform");
    await waitFor(() => expect(document.activeElement).toBe(within(form).getByTestId("preset-reputation-virustotal-input-VT_API_KEY")));
    expect(within(card).getByText("Jak zdobyć klucz")).toHaveAttribute("href", "/integrations?tab=docs&doc=conn-virustotal");
    fireEvent.change(within(form).getByTestId("preset-reputation-virustotal-input-VT_API_KEY"), { target: { value: "vt-key-1234567890" } });
    fireEvent.click(within(form).getByTestId("preset-reputation-virustotal-submit"));
    await waitFor(() => expect(applied).toBe(1));
    expect(await within(card).findByTestId("preset-reputation-virustotal-result")).toHaveTextContent("Połączono");
    await waitFor(() => expect(within(card).getByTestId("apply-reputation")).toHaveTextContent("Zastosuj pakiet"));
    const misp = within(card).getByTestId("preset-msg-reputation-misp");
    expect(misp).toHaveTextContent("Format pliku: stix21. · Konektor włączony.");
    expect(misp.textContent).not.toMatch(/\.;/);
    expect(within(card).getByTestId("preset-msg-reputation-virustotal")).toHaveTextContent("Test udany (zapytania na żywo włączone). · Konektor włączony.");
  });
});

describe("B5: XARF needs a campaign", () => {
  it("offers a campaign picker defaulting to the largest campaign and sends campaign_id", async () => {
    URL.createObjectURL = () => "blob:x";
    URL.revokeObjectURL = () => {};
    const xarf = connector({ id: "xarf", name: "XARF v4", category: "outbound_sharing", level: "export-format", direction: "outbound", formats: ["xarf"], status: "configured", can_export: true, can_connect: false, can_test: false, secrets: [], usage: null });
    const calls: string[] = [];
    mockFetch(async (input) => {
      const url = String(input);
      calls.push(url);
      if (url.split("?")[0] === "/api/v1/integrations") return jsonResponse(index({ connectors: [xarf] }));
      if (url.startsWith("/api/v1/campaigns")) return jsonResponse({ items: [
        { id: "C-small", label: "mała", members: ["a"], size: 1, core_genome: [], confidence: 1, status: "ok" },
        { id: "C-big", label: "duża", members: ["a", "b", "c"], size: 3, core_genome: [], confidence: 1, status: "ok" },
      ], total: 2, limit: 2000, offset: 0, run_id: "r1" });
      if (url.includes("/export")) return new Response("zip", { status: 200, headers: { "content-disposition": 'attachment; filename="x.zip"' } });
      return jsonResponse({ detail: "nie ma" }, { status: 404 });
    });
    renderWithProviders(<HubTab />);
    const card = await screen.findByTestId("connector-xarf");
    expect(within(card).getByTestId("needs-campaign-xarf")).toHaveTextContent("Wymaga wyboru kampanii");
    const select = await within(card).findByTestId("card-xarf-campaign");
    await waitFor(() => expect(select).toHaveValue("C-big"));
    fireEvent.click(within(card).getByRole("button", { name: /Pobierz pakiet/ }));
    await waitFor(() => expect(calls.some((u) => u.includes("/v1/integrations/xarf/export") && u.includes("campaign_id=C-big"))).toBe(true));
    fireEvent.change(select, { target: { value: "C-small" } });
    await waitFor(() => expect(within(card).getByRole("button", { name: /Pobierz pakiet/ })).toBeEnabled());
    fireEvent.click(within(card).getByRole("button", { name: /Pobierz pakiet/ }));
    await waitFor(() => expect(calls.some((u) => u.includes("campaign_id=C-small"))).toBe(true));
  });
});
