import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../../test/utils";
import { KeyForm, outcomeOf } from "./KeyForm";
import { needsKeyPl } from "./labels";
import { joinSteps, summarize } from "./Presets";
import { connector } from "./testData";

afterEach(() => vi.unstubAllGlobals());

const misp = connector({
  id: "misp", name: "MISP", level: "export-format", secrets: [
    { name: "MISP_URL", configured: false, source: null, last4: null },
    { name: "MISP_KEY", configured: true, source: "file", last4: "wxyz" },
  ],
});

describe("KeyForm", () => {
  it("address secrets are plain text, keys are masked; saved keys show last 4 and can be removed", async () => {
    const calls: string[] = [];
    mockFetch(async (input, init) => {
      calls.push(`${init?.method ?? "GET"} ${String(input)}`);
      return jsonResponse({ name: "MISP_KEY", configured: false, source: null, last4: null });
    });
    renderWithProviders(<KeyForm connector={misp} offline idPrefix="t" />);
    expect((screen.getByTestId("t-input-MISP_URL") as HTMLInputElement).type).toBe("url");
    expect((screen.getByTestId("t-input-MISP_KEY") as HTMLInputElement).type).toBe("password");
    expect(screen.getByTestId("t-saved-MISP_KEY")).toHaveTextContent("…wxyz");
    expect(screen.getByTestId("t-submit")).toBeDisabled(); // MISP_URL still missing
    fireEvent.click(screen.getByTestId("t-remove-MISP_KEY"));
    await waitFor(() => expect(calls).toContain("DELETE /api/v1/integrations/misp/secret?name=MISP_KEY"));
  });

  it("shows a Polish error reason inline when the test fails", async () => {
    mockFetch(async () => jsonResponse({
      ok: false, connector: connector({ id: "virustotal" }),
      steps: [{ id: "secret", ok: true, message_pl: "Klucz zapisany." }, { id: "test", ok: false, status: "error", message_pl: "Dostawca odrzucił klucz (HTTP 401)." }],
    }));
    const vt = connector({ id: "virustotal", secrets: [{ name: "VT_API_KEY", configured: false, source: null, last4: null }] });
    renderWithProviders(<KeyForm connector={vt} offline={false} idPrefix="v" />);
    fireEvent.change(screen.getByTestId("v-input-VT_API_KEY"), { target: { value: "bad-key-000000000" } });
    fireEvent.click(screen.getByTestId("v-submit"));
    await waitFor(() => expect(screen.getByTestId("v-result")).toHaveAttribute("data-kind", "error"));
    expect(screen.getByTestId("v-result")).toHaveTextContent("Błąd: Dostawca odrzucił klucz (HTTP 401).");
  });

  it("connectors without keys offer 'Testuj i włącz'", () => {
    renderWithProviders(<KeyForm connector={connector({ id: "crtsh", auth: "none" })} offline idPrefix="c" />);
    expect(screen.getByTestId("c-submit")).toHaveTextContent("Testuj i włącz");
    expect(screen.queryByTestId("key-callout-crtsh")).toBeNull();
  });

  it("outcome, summary and step joining helpers", () => {
    const base = { connector: connector({ id: "x" }) };
    expect(outcomeOf({ ...base, ok: true, steps: [] }, true).kind).toBe("ok");
    expect(outcomeOf({ ...base, ok: false, steps: [{ id: "test", ok: false, status: "offline", message_pl: "" }] }, true).kind).toBe("offline");
    expect(summarize(["ready", "needs_key", "needs_key", "offline"]).text).toBe("1 z 4 gotowe, 2 wymagają klucza, 1 offline");
    expect(needsKeyPl(5)).toBe("5 wymaga klucza");
    expect(needsKeyPl(22)).toBe("22 wymagają klucza");
    expect(joinSteps({ connector: "a", ok: true, status: "configured", message_pl: "Test udany (zapytania na żywo włączone).; Konektor włączony." }))
      .toBe("Test udany (zapytania na żywo włączone). · Konektor włączony.");
  });
});
