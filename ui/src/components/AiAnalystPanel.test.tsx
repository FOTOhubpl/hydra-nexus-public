import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../test/utils";
import { AiAnalystPanel } from "./AiAnalystPanel";

describe("AiAnalystPanel", () => {
  it("shows the calm empty state when the layer is off", async () => {
    mockFetch(async () => jsonResponse({ status: "unavailable", reason_pl: "Warstwa AI wyłączona — wyniki opierają się wyłącznie na dowodach." }));
    renderWithProviders(<AiAnalystPanel kind="campaign" a="C-1" />);
    expect(await screen.findByText(/Warstwa AI wyłączona/)).toBeInTheDocument();
  });

  it("renders citations as links to evidence rows", async () => {
    mockFetch(async () => jsonResponse({ status: "ok", text_pl: "Wspólny piksel [1] i domena [2].", citations: [
      { n: 1, section: "core_genome", index: 0, label: "tracker_id" }, { n: 2, section: "core_genome", index: 1, label: "domain" }], model: "m" }));
    renderWithProviders(<AiAnalystPanel kind="pair" a="a" b="b" />);
    expect(await screen.findByRole("button", { name: "[1]" })).toBeInTheDocument();
    expect(screen.getByText("[2] domain")).toBeInTheDocument();
  });
});
