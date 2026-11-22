import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";

function Bomb({ explode }: { explode: boolean }) {
  if (explode) throw new Error("kaboom");
  return <span>safe</span>;
}

describe("ErrorBoundary", () => {
  it("renders the fallback with the error message and recovers on retry", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const user = userEvent.setup();

    function Harness() {
      const [explode, setExplode] = useState(true);
      return (
        <>
          <button onClick={() => setExplode(false)}>defuse</button>
          <ErrorBoundary label="Widget">
            <Bomb explode={explode} />
          </ErrorBoundary>
        </>
      );
    }

    render(<Harness />);
    expect(screen.getByText("Widget: błąd wyświetlania")).toBeInTheDocument();
    expect(screen.getByText("kaboom")).toBeInTheDocument();

    await user.click(screen.getByText("defuse"));
    await user.click(screen.getByRole("button", { name: "Spróbuj ponownie" }));
    expect(screen.getByText("safe")).toBeInTheDocument();
  });

  it("uses a custom fallback and resets when resetKeys change", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { rerender } = render(
      <ErrorBoundary resetKeys={["a"]} fallback={(error) => <span>custom: {error.message}</span>}>
        <Bomb explode />
      </ErrorBoundary>,
    );
    expect(screen.getByText("custom: kaboom")).toBeInTheDocument();
    rerender(
      <ErrorBoundary resetKeys={["b"]} fallback={(error) => <span>custom: {error.message}</span>}>
        <Bomb explode={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("safe")).toBeInTheDocument();
  });
});
