/**
 * Every route of ui/src/App.tsx, in all four projects (desktop/mobile x light/dark): the page renders its key
 * element with real values from the synthetic corpus, no console/page errors and no HTTP 5xx (fixtures.ts guard).
 * Horizontal overflow on mobile is recorded as an annotation (soft, non-failing).
 */
import { apiJson, campaignByLabel, expect, shellReady, test } from "./fixtures";
import { apiAuthHeaders } from "./authState";
import type { Page, TestInfo } from "playwright/test";

interface Ctx {
  campaignA: { id: string; members: string[] };
  pairs: number;
  campaigns: number;
  dashIncidents: number;
  incidents: number;
  artifacts: number;
  artifactA1: string;
}

let ctx: Ctx;

test.beforeAll(async ({ playwright }) => {
  const request = await playwright.request.newContext({ baseURL: process.env.E2E_BASE_URL, extraHTTPHeaders: apiAuthHeaders() });
  const a = await campaignByLabel(request, "Inwestycja PKO Bonus");
  // the newest *analyse* run is what the UI shows by default; GUI uploads add "assign" runs on top of it
  const runs = await apiJson<{ items: { kind: string; counts: { pairs: number } }[] }>(request, "/v1/runs");
  const run = runs.items.find((r) => r.kind === "analyse");
  expect(run, "an analyse run in GET /v1/runs").toBeTruthy();
  const health = await apiJson<{ artifacts: number }>(request, "/health");
  // counts as the UI shows them: base run + live (uploaded) incidents
  const dash = await apiJson<{ kpis: { incidents: number; campaigns: number } }>(request, "/v1/dashboard");
  const incidents = await apiJson<{ total: number }>(request, "/v1/incidents?limit=1");
  const inc = await apiJson<{ artifacts: { artifact_id: string }[] }>(request, "/v1/incidents/nask-a1");
  ctx = {
    campaignA: a,
    pairs: run!.counts.pairs,
    campaigns: dash.kpis.campaigns,
    dashIncidents: dash.kpis.incidents,
    incidents: incidents.total,
    artifacts: health.artifacts,
    artifactA1: inc.artifacts[0]!.artifact_id,
  };
  await request.dispose();
});

const main = (page: Page) => page.locator("main");
const title = (page: Page) => page.locator("header h1");

type Check = (page: Page) => Promise<void>;
interface RouteCase {
  name: string;
  path: () => string;
  title: string;
  check: Check;
}

const ROUTES: RouteCase[] = [
  {
    name: "index (dashboard)", path: () => "/", title: "Pulpit",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Kampanie", exact: true })).toBeVisible();
      // stat tiles: "Incydenty 20 …", "Kampanie 8 4 z ≥ 2 incydentami"
      await expect(main(page).getByRole("link", { name: new RegExp(`^Incydenty ${ctx.dashIncidents}\\b`) })).toBeVisible();
      await expect(main(page).getByRole("link", { name: new RegExp(`^Kampanie ${ctx.campaigns}\\b`) })).toBeVisible();
      await expect(main(page).getByText("Inwestycja PKO Bonus", { exact: true }).first()).toBeAttached();
    },
  },
  {
    name: "campaigns", path: () => "/campaigns", title: "Kampanie",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Lista kampanii" })).toBeVisible();
      await expect(main(page).getByText("Inwestycja PKO Bonus").first()).toBeVisible();
      await expect(main(page).getByText("Buty wyprzedaż -90%").first()).toBeVisible();
    },
  },
  {
    name: "campaigns/:id", path: () => `/campaigns/${ctx.campaignA.id}`, title: "Karta kampanii",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: new RegExp(`${ctx.campaignA.id} · Inwestycja PKO Bonus`) })).toBeVisible();
      await expect(main(page).getByRole("heading", { name: `Wsparcie członków (${ctx.campaignA.members.length})` })).toBeVisible();
      const table = page.getByTestId("member-support");
      for (const m of ctx.campaignA.members) await expect(table).toContainText(m);
    },
  },
  {
    // B3 (audit f450a70): an id that no run knows is a missing campaign, not a missing API endpoint
    name: "campaigns/:unknown-id", path: () => "/campaigns/C-00000000", title: "Karta kampanii",
    check: async (page) => {
      await expect(main(page).getByText("Ta kampania nie istnieje w bieżącym przebiegu", { exact: true })).toBeVisible();
      await expect(main(page).getByRole("link", { name: "Przejdź do listy kampanii" })).toBeVisible();
      await expect(main(page).getByText(/Punkt końcowy API/)).toHaveCount(0);
    },
  },
  {
    // B8: the lab route has its own title
    name: "lab/:incident", path: () => "/lab/nask-a1", title: "Laboratorium hipotez · nask-a1",
    check: async () => {},
  },
  {
    name: "graph", path: () => "/graph", title: "Graf powiązań",
    check: async (page) => {
      await expect(page.getByTestId("constellation")).toBeVisible();
      await expect(page.getByTestId("constellation").locator("canvas").first()).toBeAttached();
    },
  },
  {
    name: "analyse", path: () => "/analyse", title: "Nowa analiza",
    check: async (page) => {
      await expect(main(page).getByRole("tab", { name: "Plik lub ZIP incydentu" })).toBeVisible();
      await expect(main(page).getByRole("button", { name: "Analizuj", exact: true })).toBeDisabled();
    },
  },
  {
    name: "simulation", path: () => "/simulation", title: "Symulacja potoku",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Import" })).toBeVisible();
    },
  },
  {
    name: "how", path: () => "/how", title: "Jak to działa",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Decyzje par" })).toBeVisible();
    },
  },
  {
    name: "diff/:a/:b", path: () => "/diff/nask-a1/nask-a2", title: "Co się zmieniło",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Tekst", exact: true })).toBeVisible();
      await expect(main(page).getByRole("heading", { name: "Infrastruktura", exact: true })).toBeVisible();
    },
  },
  {
    name: "pairs", path: () => "/pairs", title: "Pary i dowody",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: `${ctx.pairs} par` })).toBeVisible();
    },
  },
  {
    name: "queue", path: () => "/queue", title: "Kolejka analityka",
    check: async (page) => {
      await expect(page.getByTestId("queue-list")).toBeVisible();
      await expect(page.getByTestId("queue-list").locator("li").first()).toBeVisible();
    },
  },
  {
    name: "pair/:a/:b", path: () => "/pair/nask-a1/nask-a2", title: "Dowód pary",
    check: async (page) => {
      await expect(page.getByTestId("waterfall")).toBeVisible();
      await expect(page.getByTestId("waterfall")).toContainText("Suma");
    },
  },
  {
    name: "incidents", path: () => "/incidents", title: "Incydenty",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: `${ctx.incidents} incydentów` })).toBeVisible();
      await expect(main(page).getByText("nask-a1", { exact: true }).first()).toBeVisible();
    },
  },
  {
    name: "incident/:id", path: () => "/incident/nask-a1", title: "Incydent",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: /^nask-a1/ })).toBeVisible();
      await expect(main(page).getByRole("heading", { name: "Materiały incydentu (3)" })).toBeVisible();
    },
  },
  {
    name: "search", path: () => "/search", title: "Wyszukiwanie",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Wyszukiwanie" })).toBeVisible();
    },
  },
  {
    name: "methodology", path: () => "/methodology", title: "Metodyka i wyniki",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Metoda w skrócie" })).toBeVisible();
    },
  },
  {
    name: "scheduler", path: () => "/scheduler", title: "Harmonogram",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Stan harmonogramu" })).toBeVisible();
    },
  },
  {
    name: "watch", path: () => "/watch", title: "Monitoring domen",
    check: async (page) => {
      // default tab: the realtime engine; the classic ticker is the second tab
      await expect(main(page).getByRole("heading", { name: "Strumień na żywo" })).toBeVisible();
      await expect(main(page).getByTestId("engine-pill")).toBeVisible();
      await main(page).getByRole("tab", { name: "Odkrywanie domen (nagrania)" }).click();
      await expect(main(page).getByRole("heading", { name: "Ticker" })).toBeVisible();
    },
  },
  {
    name: "hunt", path: () => "/hunt", title: "Poszukiwanie wariantów",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Poszukiwanie wariantów" })).toBeVisible();
    },
  },
  {
    name: "data", path: () => "/data", title: "Import danych",
    check: async (page) => {
      await expect(page.getByTestId("file-input")).toBeAttached();
      await expect(main(page).getByRole("heading", { name: `Artefakty ${ctx.artifacts}` })).toBeVisible();
    },
  },
  {
    name: "item/:id", path: () => `/item/${ctx.artifactA1}`, title: "Artefakt",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Podgląd" })).toBeVisible();
      await expect(main(page)).toContainText(ctx.artifactA1.slice(0, 12));
    },
  },
  {
    name: "settings", path: () => "/settings", title: "Ustawienia",
    check: async (page) => {
      await expect(main(page).getByRole("heading", { name: "Serwer" })).toBeVisible();
    },
  },
  {
    name: "unknown path -> NotFound", path: () => "/no-such-page/x1", title: "Nie znaleziono",
    check: async (page) => {
      await expect(main(page).getByText("Nie ma takiej strony")).toBeVisible();
      await expect(main(page).getByText("/no-such-page/x1")).toBeVisible();
      await expect(main(page).getByRole("link", { name: "Wróć do pulpitu" })).toBeVisible();
    },
  },
];

async function noteOverflow(page: Page, testInfo: TestInfo): Promise<void> {
  const o = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  if (o.sw > o.cw + 1) {
    testInfo.annotations.push({ type: "soft: horizontal overflow", description: `scrollWidth ${o.sw} > clientWidth ${o.cw}` });
  }
}

for (const r of ROUTES) {
  test(`route ${r.name}`, async ({ page, theme }, testInfo) => {
    await page.goto(r.path());
    await shellReady(page);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await expect(title(page)).toHaveText(r.title);
    await r.check(page);
    await page.waitForTimeout(500); // let late requests / effects report their errors to the guard
    if (testInfo.project.name.startsWith("mobile")) await noteOverflow(page, testInfo);
  });
}
