import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderWithProviders } from "../../test/utils";
import { RunExports } from "./Panels";

describe("RunExports", () => {
  it("links the whole-run Neo4j export and the dataset ZIP of the run", () => {
    renderWithProviders(<RunExports runId="analyse-x y" />);
    const cypher = screen.getByRole("link", { name: /Neo4j Cypher/ });
    expect(cypher.getAttribute("href")).toContain("/v1/export/neo4j?format=cypher&min_p=0&run_id=analyse-x%20y");
    expect(screen.getByRole("link", { name: /Neo4j CSV \(ZIP\)/ }).getAttribute("href")).toContain("format=zip");
    const ds = screen.getByRole("link", { name: /Zbiór danych \(ZIP\)/ });
    expect(ds.getAttribute("href")).toContain("/v1/export/dataset?run_id=analyse-x%20y");
    expect(screen.getByTestId("export-signing-key").textContent).toContain("hydra verify-export");
  });

  it("renders nothing without a run", () => {
    const { container } = renderWithProviders(<RunExports runId={null} />);
    expect(container.textContent).toBe("");
  });
});
