import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { Kpi, MiniBars, StackBar } from "./bento";

describe("dashboard bento primitives", () => {
  it("KPI link name starts with label then value", () => {
    render(<MemoryRouter><Kpi label="Incydenty" value="20" hint="zgłoszenia w bazie" to="/incidents" /></MemoryRouter>);
    expect(screen.getByRole("link", { name: /^Incydenty 20\b/ })).toHaveAttribute("href", "/incidents");
  });

  it("mini-bars scale to the largest value and keep a visible value label", () => {
    const { container } = render(<MiniBars rows={[{ id: "a", label: "A", value: 10 }, { id: "b", label: "B", value: 5, valueLabel: "5 inc." }]} />);
    const fills = container.querySelectorAll<HTMLElement>("li span[aria-hidden] > span");
    expect(fills[0]!.style.width).toBe("100%");
    expect(fills[1]!.style.width).toBe("50%");
    expect(screen.getByText("5 inc.")).toBeInTheDocument();
  });

  it("stack bar skips empty parts and renders nothing for an all-zero total", () => {
    const { rerender } = render(<StackBar label="x" parts={[{ key: "a", value: 2, color: "red", title: "a" }, { key: "b", value: 0, color: "blue", title: "b" }]} />);
    expect(screen.getByRole("img", { name: "x" }).children).toHaveLength(1);
    rerender(<StackBar label="x" parts={[{ key: "a", value: 0, color: "red", title: "a" }]} />);
    expect(screen.queryByRole("img")).toBeNull();
  });
});
