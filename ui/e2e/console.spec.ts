/**
 * "Integracje & API" → Konsola API and Dokumentacja against the throwaway e2e server (tests/e2e/server.py,
 * HYDRA_OFFLINE=1). Read-only: the console only sends GET here. Runs in the desktop projects; the 390 px checks
 * resize the viewport inside the test. Optional screenshots: E2E_SHOTS=<dir> (e.g. qa/integrations).
 */
import { expect, shellReady, test } from "./fixtures";
import type { Page } from "playwright/test";

const SHOTS = process.env.E2E_SHOTS;

async function shot(page: Page, name: string, theme: string, fullPage = false) {
  if (!SHOTS) return;
  const w = page.viewportSize()?.width ?? 0;
  await page.screenshot({ path: `${SHOTS}/${theme}-${w}-${name}.png`, fullPage });
}

/** Document/main must not scroll sideways and no element in main may stick out of the viewport. */
async function noOverflow(page: Page) {
  const o = await page.evaluate(() => {
    const main = document.querySelector("main");
    const vw = document.documentElement.clientWidth;
    const offenders: string[] = [];
    for (const el of Array.from(main?.querySelectorAll<HTMLElement>("*") ?? [])) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      // inside a scroll container (code blocks, tables) is fine
      let p: HTMLElement | null = el.parentElement;
      let clipped = false;
      while (p && p !== main) {
        const ox = getComputedStyle(p).overflowX;
        if (ox === "auto" || ox === "scroll" || ox === "hidden" || ox === "clip") {
          clipped = true;
          break;
        }
        p = p.parentElement;
      }
      if (!clipped && r.right > vw + 1) offenders.push(`${el.tagName.toLowerCase()}.${String(el.className).slice(0, 60)} right=${Math.round(r.right)}`);
    }
    return { doc: document.documentElement.scrollWidth - vw, main: main ? main.scrollWidth - main.clientWidth : 0, offenders: offenders.slice(0, 5) };
  });
  expect(o.doc, "document horizontal overflow").toBeLessThanOrEqual(0);
  expect(o.main, "main horizontal overflow").toBeLessThanOrEqual(0);
  expect(o.offenders, "elements wider than the viewport").toEqual([]);
}

test.describe("integrations console + docs", () => {
  test("console: search health, send GET, 200 + snippet", async ({ page, theme }) => {
    await page.goto("/integrations?tab=console");
    await shellReady(page);
    await expect(page.getByRole("tab", { name: "Konsola API" })).toHaveAttribute("aria-selected", "true");
    const search = page.getByTestId("console-search");
    await expect(search).toBeVisible();
    await page.locator("body").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("/");
    await expect(search).toBeFocused();
    await search.fill("health");
    await page.getByTestId("op-health_health_get").click();
    await expect(page).toHaveURL(/op=health_health_get/);
    await page.getByTestId("console-send").click();
    await expect(page.getByTestId("response-status")).toHaveText("200");
    await expect(page.getByTestId("response-time")).toContainText("ms");
    await expect(page.getByTestId("response-body")).toContainText('"');
    await expect(page.getByTestId("console-snippet")).toContainText("curl -sS");
    await expect(page.getByTestId("console-snippet")).toContainText("/health");
    await shot(page, "console-health", theme);
  });

  test("console: deep link, query params, Ctrl+Enter, SSE note, unsafe confirm", async ({ page, theme }) => {
    await page.goto("/integrations?tab=console&op=list_campaigns_v1_campaigns_get");
    await shellReady(page);
    await expect(page.getByTestId("console-operation")).toContainText("/v1/campaigns");
    await page.getByTestId("console-search").focus();
    await page.keyboard.press("Control+Enter");
    await expect(page.getByTestId("response-status")).toHaveText("200");
    await expect(page.getByTestId("response-body")).toContainText("items");
    await shot(page, "console-campaigns", theme);

    await page.getByTestId("console-search").fill("stream");
    await page.getByTestId("op-stream_v1_stream_get").click();
    await expect(page.getByTestId("console-streaming-note")).toBeVisible();
    await expect(page.getByTestId("console-send")).toHaveCount(0);
    await expect(page.getByTestId("console-snippet")).toContainText("curl -N");

    await page.getByTestId("console-search").fill("watchlist");
    await page.getByTestId("op-change_watchlist_v1_watchlist_post").click();
    await page.getByTestId("console-send").click();
    await expect(page.getByTestId("console-confirm")).toContainText("lokalnej instancji");
    await shot(page, "console-confirm", theme);
    await page.getByRole("button", { name: "Anuluj" }).click();
    await expect(page.getByTestId("console-confirm")).toHaveCount(0);
  });

  test("docs: search HMAC, TOC, API reference opens console", async ({ page, theme }) => {
    await page.goto("/integrations?tab=docs");
    await shellReady(page);
    await expect(page.getByTestId("api-reference")).toBeVisible();
    await shot(page, "docs", theme);
    await page.getByTestId("docs-search").fill("HMAC");
    const hooks = page.getByTestId("doc-section-webhooki");
    await expect(hooks).toBeVisible();
    await expect(hooks).toContainText("X-Hydra-Signature");
    await expect(page.getByTestId("doc-section-strumien")).toHaveCount(0);
    await shot(page, "docs-hmac", theme);
    await page.getByTestId("docs-search").fill("token Meta");
    const guides = page.getByTestId("doc-section-konektory");
    await expect(guides).toContainText("Jak uzyskać token Meta Ad Library");
    await expect(guides.getByTestId("connector-guides")).toContainText("Meta Ad Library API");
    await guides.locator("#conn-meta-adlibrary summary").click();
    await expect(guides.locator("#conn-meta-adlibrary")).toContainText("Licencja");
    await shot(page, "docs-meta", theme);
    await page.getByTestId("docs-search").fill("");
    await page.getByTestId("toc-strumien").click();
    await expect(page.getByTestId("doc-section-strumien")).toBeInViewport();

    const ref = page.getByTestId("api-reference");
    await ref.locator("summary", { hasText: "System" }).first().click();
    const health = page.locator("#api-health_health_get");
    await health.locator("summary").click();
    await health.getByRole("button", { name: "Otwórz w konsoli" }).click();
    await expect(page).toHaveURL(/tab=console.*op=health_health_get/);
    await expect(page.getByTestId("console-operation")).toContainText("/health");
  });

  test("390 px: console and docs have no horizontal overflow", async ({ page, theme }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/integrations?tab=console&op=campaign_stix_v1_campaigns__campaign_id__stix_get");
    await shellReady(page);
    await expect(page.getByTestId("console-operation")).toBeVisible();
    await noOverflow(page);
    await page.getByTestId("console-search").fill("health");
    await page.getByTestId("op-health_health_get").click();
    await page.getByTestId("console-send").click();
    await expect(page.getByTestId("response-status")).toHaveText("200");
    await noOverflow(page);
    await shot(page, "console-health", theme);

    await page.goto("/integrations?tab=docs");
    await shellReady(page);
    await expect(page.getByTestId("api-reference")).toBeVisible();
    await noOverflow(page);
    await shot(page, "docs", theme);
    const toggle = page.getByRole("button", { name: /Spis treści/ });
    await toggle.click();
    await expect(page.getByTestId("toc-webhooki")).toBeVisible();
    await shot(page, "docs-toc", theme);
    await page.getByTestId("toc-webhooki").click();
    await expect(page.getByTestId("doc-section-webhooki")).toBeInViewport();
    await page.locator("#api-tag-exports summary").click().catch(() => undefined);
    await noOverflow(page);
  });
});
