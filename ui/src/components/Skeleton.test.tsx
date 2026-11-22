import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryState } from "../pages/task/common";
import { SLOW_HINT_MS, SLOW_HINT_TEXT } from "./Skeleton";

afterEach(() => vi.useRealTimers());

describe("QueryState loading skeleton", () => {
  it("shows a busy skeleton while pending and the slow-load hint after ~1.5 s", () => {
    vi.useFakeTimers();
    render(<QueryState query={{ isPending: true, isError: false, error: null }} what="graf" />);
    expect(screen.getByTestId("skeleton")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("Ładowanie: graf…")).toBeInTheDocument();
    expect(screen.queryByText(SLOW_HINT_TEXT)).toBeNull();
    act(() => vi.advanceTimersByTime(SLOW_HINT_MS + 10));
    expect(screen.getByText(SLOW_HINT_TEXT)).toBeInTheDocument();
  });

  it("renders nothing once the query has data", () => {
    const { container } = render(<QueryState query={{ isPending: false, isError: false, error: null }} what="graf" />);
    expect(container).toBeEmptyDOMElement();
  });
});
