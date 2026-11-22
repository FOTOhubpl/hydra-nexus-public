import { describe, expect, it } from "vitest";
import { discreteBits, hubLimit, qualityWeight, smoothBounds, smoothWeight, uEstimate } from "./rarity";

// Arithmetic examples from docs/METHODOLOGY.md §5 "Repeated values" (not measurements).
describe("HYDRA-RGE client mirror", () => {
  it("reproduces the registrable-domain example: 10 incidents, held only by the pair = 10.5 bits", () => {
    const r = discreteBits({ n: 10, holders: 2, u0: 2 ** -11 });
    expect(r.nOthers).toBe(8);
    expect(r.dfOthers).toBe(0);
    expect(r.bits).toBeCloseTo(10.49, 2);
  });

  it("one more holder drops it to 3.8 bits", () => {
    expect(discreteBits({ n: 10, holders: 3, u0: 2 ** -11 }).bits).toBeCloseTo(3.79, 2);
  });

  it("clips at 14 bits and zeroes hub values", () => {
    const r = discreteBits({ n: 1000, holders: 2, u0: 2 ** -16 });
    expect(r.bits).toBe(14);
    expect(r.capped).toBe(true);
    expect(hubLimit(10)).toBe(5);
    expect(discreteBits({ n: 10, holders: 6, u0: 2 ** -11 }).bits).toBe(0);
  });

  it("background prior is one-sided (max rule) and ignores thin kinds", () => {
    const loc = uEstimate(0, 8, 2 ** -11);
    expect(uEstimate(0, 8, 2 ** -11, { df: 50, n: 3000 })).toBeGreaterThan(loc);
    expect(uEstimate(0, 8, 2 ** -11, { df: 0, n: 3000 })).toBe(loc);
    expect(uEstimate(0, 8, 2 ** -11, { df: 50, n: 199 })).toBe(loc);
  });

  it("quality weight: < 0.25 → 0, linear to 0.5, then 1", () => {
    expect(qualityWeight(0.2)).toBe(0);
    expect(qualityWeight(0.4)).toBeCloseTo(0.8);
    expect(qualityWeight(0.9)).toBe(1);
  });

  it("smooth operator units: w = (U+1−h)/(U+1−L)", () => {
    expect(smoothBounds(100)).toEqual({ L: 20, U: 50 });
    expect(smoothWeight(100, 20)).toBe(1);
    expect(smoothWeight(100, 21)).toBeCloseTo(30 / 31);
    expect(smoothWeight(100, 50)).toBeCloseTo(1 / 31);
    expect(smoothWeight(100, 51)).toBe(0);
    expect(smoothBounds(10)).toEqual({ L: 5, U: 10 });
  });
});
