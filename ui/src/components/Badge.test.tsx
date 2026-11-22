import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Badge, type BadgeVariant } from "./Badge";
import { statusVariant } from "../lib/status";

describe("Badge", () => {
  it("renders its label with the neutral variant by default", () => {
    render(<Badge>queued</Badge>);
    const badge = screen.getByText("queued").closest("[data-variant]");
    expect(badge).toHaveAttribute("data-variant", "neutral");
  });

  it.each<BadgeVariant>(["neutral", "info", "success", "warning", "danger"])("applies the %s variant", (variant) => {
    render(<Badge variant={variant}>label-{variant}</Badge>);
    const badge = screen.getByText(`label-${variant}`).closest("[data-variant]");
    expect(badge).toHaveAttribute("data-variant", variant);
    expect(badge?.className).toContain("whitespace-nowrap");
  });

  it("renders a decorative status dot only when asked", () => {
    const { container, rerender } = render(<Badge variant="success">done</Badge>);
    expect(container.querySelector("[aria-hidden='true']")).toBeNull();
    rerender(
      <Badge variant="success" dot>
        done
      </Badge>,
    );
    expect(container.querySelector("[aria-hidden='true']")).not.toBeNull();
  });
});

describe("statusVariant", () => {
  it("maps common statuses onto badge variants", () => {
    expect(statusVariant("done")).toBe("success");
    expect(statusVariant("Running")).toBe("info");
    expect(statusVariant("in progress")).toBe("info");
    expect(statusVariant("failed")).toBe("danger");
    expect(statusVariant("degraded")).toBe("warning");
    expect(statusVariant("queued")).toBe("neutral");
    expect(statusVariant("something-new")).toBe("neutral");
    expect(statusVariant(undefined)).toBe("neutral");
  });
});
