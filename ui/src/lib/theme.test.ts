import { afterEach, describe, expect, it, vi } from "vitest";
import { resolveTheme, systemTheme } from "./theme";

afterEach(() => vi.unstubAllGlobals());

function stubMatchMedia(dark: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: dark && q.includes("dark"), media: q, addEventListener() {}, removeEventListener() {} }));
}

describe("theme default (B14)", () => {
  it("follows prefers-color-scheme when nothing is stored", () => {
    stubMatchMedia(true);
    expect(systemTheme()).toBe("dark");
    expect(resolveTheme(null, systemTheme())).toBe("dark");
    stubMatchMedia(false);
    expect(resolveTheme(undefined, systemTheme())).toBe("light");
  });

  it("an explicit stored choice wins over the OS preference", () => {
    expect(resolveTheme("light", "dark")).toBe("light");
    expect(resolveTheme("dark", "light")).toBe("dark");
    expect(resolveTheme("garbage", "dark")).toBe("dark");
  });

  it("falls back to light without matchMedia", () => {
    vi.stubGlobal("matchMedia", undefined);
    expect(systemTheme()).toBe("light");
  });
});
