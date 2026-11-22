import { describe, expect, it } from "vitest";
import { bitsWord, formatBits, formatDatePl, formatProbability, waterfallSteps } from "./evidence";

describe("evidence formatting", () => {
  it("uses Polish decimal comma and signed bits", () => {
    expect(formatProbability(0.9912)).toBe("0,991");
    expect(formatBits(6.25)).toBe("+6,3 b");
    expect(formatBits(-3)).toBe("−3,0 b");
    expect(formatProbability(null)).toBe("—");
  });

  it("names bit strength per spec thresholds", () => {
    expect(bitsWord(5)).toBe("słabe");
    expect(bitsWord(12)).toBe("możliwe");
    expect(bitsWord(24.4)).toBe("silne");
  });

  it("shows date-only values as a calendar day without an invented time", () => {
    expect(formatDatePl("2026-10-01")).toBe("1 paź 2026");
    expect(formatDatePl("2026-12-31")).toBe("31 gru 2026");
    expect(formatDatePl("2026-02-31")).toBe("2026-02-31");
    expect(formatDatePl(null)).toBe("—");
    expect(formatDatePl("2026-10-01T10:11:00Z")).toMatch(/^1 paź 2026, \d{2}:\d{2}$/);
  });
});

describe("waterfallSteps", () => {
  const fams = ["VIS", "TXT", "INF", "ID"];

  it("marks missing families as unknown with zero bits and ends at the total", () => {
    const steps = waterfallSteps({ VIS: 6, INF: 4 }, fams, 10, ["ID"]);
    expect(steps.map((s) => s.key)).toEqual(["VIS", "TXT", "INF", "ID", "total"]);
    expect(steps.find((s) => s.key === "ID")?.kind).toBe("unknown");
    expect(steps.find((s) => s.key === "TXT")?.kind).toBe("unknown");
    expect(steps.at(-1)?.end).toBe(10);
  });

  it("adds a residual step when contradictions are outside the family bits", () => {
    const steps = waterfallSteps({ VIS: 9 }, fams, 6, []);
    const residual = steps.find((s) => s.key === "residual");
    expect(residual?.bits).toBe(-3);
    expect(residual?.kind).toBe("against");
    expect(steps.at(-1)?.end).toBe(6);
  });
});
