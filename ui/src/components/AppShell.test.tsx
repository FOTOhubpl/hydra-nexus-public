import { describe, expect, it } from "vitest";
import { crumbsForPath, NAV_GROUPS, NAV_ITEMS, titleForPath } from "./AppShell";

describe("shell naming (plain Polish names)", () => {
  it("groups the navigation into the six sections", () => {
    expect(NAV_GROUPS.map((g) => g.title)).toEqual(["Przegląd", "Analiza", "Kampanie i powiązania", "Dowody", "Monitoring", "Ustawienia"]);
    const labels = NAV_ITEMS.map((n) => n.label).join(" | ");
    expect(labels).not.toMatch(/centrum dowodzenia|kokpit|korpus/i);
    expect(new Set(NAV_ITEMS.map((n) => n.to)).size).toBe(NAV_ITEMS.length);
  });

  it("titles every route", () => {
    expect(titleForPath("/")).toBe("Pulpit");
    expect(titleForPath("/analyse")).toBe("Nowa analiza");
    expect(titleForPath("/campaigns")).toBe("Kampanie");
    expect(titleForPath("/campaigns/c-1")).toBe("Karta kampanii");
    expect(titleForPath("/incidents")).toBe("Incydenty");
    expect(titleForPath("/incident/a1")).toBe("Incydent");
    expect(titleForPath("/pairs")).toBe("Pary i dowody");
    expect(titleForPath("/pair/a/b")).toBe("Dowód pary");
    expect(titleForPath("/diff/a/b")).toBe("Co się zmieniło");
    // B8 (audit f450a70): the lab route had no title and fell through to "Nie znaleziono".
    expect(titleForPath("/lab/nask-incydent_00001")).toBe("Laboratorium hipotez · nask-incydent_00001");
    expect(titleForPath("/lab/a%20b")).toBe("Laboratorium hipotez · a b");
    expect(crumbsForPath("/lab/x").map((c) => c.label)).toContain("Incydenty");
    expect(titleForPath("/item/abc")).toBe("Artefakt");
    expect(titleForPath("/watch")).toBe("Monitoring domen");
    expect(titleForPath("/integrations")).toBe("Integracje & API");
    expect(crumbsForPath("/integrations")).toEqual([{ label: "Monitoring" }]);
    expect(titleForPath("/no-such-page/x1")).toBe("Nie znaleziono");
  });

  it("builds the breadcrumb from the section and, for detail pages, the list page", () => {
    expect(crumbsForPath("/")).toEqual([{ label: "Przegląd" }]);
    expect(crumbsForPath("/graph")).toEqual([{ label: "Kampanie i powiązania" }]);
    expect(crumbsForPath("/campaigns/c-1")).toEqual([{ label: "Kampanie i powiązania" }, { label: "Kampanie", to: "/campaigns" }]);
    expect(crumbsForPath("/pair/a/b")).toEqual([{ label: "Dowody" }, { label: "Pary i dowody", to: "/pairs" }]);
    expect(crumbsForPath("/no-such-page")).toEqual([]);
  });
});
