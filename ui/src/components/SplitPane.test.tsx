import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { SplitPane } from "./SplitPane";

describe("SplitPane", () => {
  it("renders both panes with their headers", () => {
    render(<SplitPane leftHeader="Left head" rightHeader="Right head" left={<p>left body</p>} right={<p>right body</p>} />);
    expect(screen.getByText("Left head")).toBeInTheDocument();
    expect(screen.getByText("Right head")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Lewy panel" })).toHaveTextContent("left body");
    expect(screen.getByRole("region", { name: "Prawy panel" })).toHaveTextContent("right body");
    // exactly one accessible separator even though the divider is drawn in both rows
    expect(screen.getAllByRole("separator")).toHaveLength(1);
  });

  it("resizes with the keyboard within bounds", async () => {
    const user = userEvent.setup();
    render(<SplitPane left="a" right="b" minRatio={0.3} />);
    const separator = screen.getByRole("separator");
    expect(separator).toHaveAttribute("aria-valuenow", "50");
    separator.focus();
    await user.keyboard("{ArrowRight}");
    expect(separator).toHaveAttribute("aria-valuenow", "52");
    await user.keyboard("{End}");
    expect(separator).toHaveAttribute("aria-valuenow", "70");
    await user.keyboard("{Enter}");
    expect(separator).toHaveAttribute("aria-valuenow", "50");
  });
});
