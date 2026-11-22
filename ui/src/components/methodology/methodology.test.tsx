import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MethodologyPage } from "../../pages/task/MethodologyPage";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";

describe("/methodology explainer", () => {
  it("renders the TOC, all 11 sections, formulas and the illustrative calculator", async () => {
    mockFetch(async () => jsonResponse({ run: null }));
    renderWithProviders(<MethodologyPage />);
    expect(screen.getByRole("heading", { name: "Metoda w skrócie" })).toBeInTheDocument();
    const toc = screen.getByTestId("method-toc");
    expect(within(toc).getAllByRole("link")).toHaveLength(11);
    for (const id of ["m-przeglad", "m-genom", "m-rzadkosc", "m-pary", "m-grupowanie", "m-przypisanie", "m-operator", "m-geometria", "m-kolejka", "m-ewaluacja", "m-ograniczenia"]) {
      expect(screen.getByTestId(`method-section-${id}`)).toBeInTheDocument();
    }
    expect(screen.getAllByText("Szczegóły techniczne").length).toBeGreaterThanOrEqual(10);
    expect(document.querySelectorAll("math").length).toBeGreaterThanOrEqual(8);
    // default calculator state = METHODOLOGY §5 worked example: N=10, domain held only by the pair → 10,5 b
    expect(screen.getByTestId("calc-bits")).toHaveTextContent("10,5");
    fireEvent.change(screen.getByLabelText(/Incydenty z tą wartością/), { target: { value: "3" } });
    expect(screen.getByTestId("calc-bits")).toHaveTextContent("3,8");
    expect(await screen.findByText("Brak przebiegu ewaluacji")).toBeInTheDocument();
  });

  it("CR-ASTRA-RT-02: describes the deployed SW-01 semantics (budget-skipped geometry -> abstain, 0-bit note)", () => {
    mockFetch(async () => jsonResponse({ run: null }));
    renderWithProviders(<MethodologyPage />);
    const pairs = screen.getByTestId("method-section-m-pary");
    expect(pairs.textContent).toMatch(/independent.*jest zamieniana na.*abstain/);
    expect(pairs.textContent).toContain("geom_skipped");
    expect(pairs.textContent).toContain("nie wynik konkretnej pary");
    expect(pairs.textContent).not.toContain("decyzja nie jest wymuszana");
    expect(pairs.textContent).not.toContain("stałe projektowe, nie dopasowane");
    expect(document.body.textContent).not.toMatch(/(?<!nie )wykrywa\w* deepfake/i); // only "nie wykrywa deepfake"
  });
});
