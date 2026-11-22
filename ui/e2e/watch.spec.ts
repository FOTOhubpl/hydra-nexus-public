/**
 * /watch "Na żywo" (WATCH-RT) against the throwaway e2e server (HYDRA_OFFLINE=1, so no source is polled over the
 * network): starts the synthetic replay fixture "demo" at 600x through the UI, waits for feed items and at least one
 * watchlist match, checks the non-dismissable SYNTETYCZNE / ODTWORZENIE banner, opens the match, and takes screenshots
 * at 1440 px and 390 px into WATCH_QA_DIR (default /tmp/hydra-qa/watch/).
 * @mutating: the replay writes items, matches and watchlist entries into the server state.
 */
import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, shellReady, test } from "./fixtures";
import type { Page } from "playwright/test";

const QA_DIR = process.env.WATCH_QA_DIR ?? "/tmp/hydra-qa/watch/";

async function shot(page: Page, name: string, theme: string, fullPage = false) {
  mkdirSync(QA_DIR, { recursive: true });
  const w = page.viewportSize()?.width ?? 0;
  await page.screenshot({ path: path.join(QA_DIR, `${theme}-${w}-${name}.png`), fullPage });
}

/** The app scrolls inside <main>; full-page captures would only show the viewport, so take sections instead. */
async function sections(page: Page, theme: string) {
  await page.evaluate(() => { document.querySelector("main")?.scrollTo(0, 0); window.scrollTo(0, 0); });
  await page.waitForTimeout(150);
  await shot(page, "replay-top", theme);
  await page.getByTestId("source-urlhaus").scrollIntoViewIfNeeded();
  await shot(page, "replay-sources", theme);
  await page.getByRole("heading", { name: "Strumień na żywo" }).scrollIntoViewIfNeeded();
  await shot(page, "replay-feed", theme);
  await page.getByTestId("match-detail").scrollIntoViewIfNeeded();
  await shot(page, "replay-match", theme);
  await page.getByTestId("demo-control").scrollIntoViewIfNeeded();
  await shot(page, "replay-timeline", theme);
  await page.getByTestId("watchlist").scrollIntoViewIfNeeded();
  await shot(page, "replay-watchlist", theme);
}

async function overflow(page: Page) {
  return page.evaluate(() => {
    const main = document.querySelector("main");
    return { doc: document.documentElement.scrollWidth - document.documentElement.clientWidth, main: main ? main.scrollWidth - main.clientWidth : 0 };
  });
}

test.describe("watch realtime @mutating", () => {
  test("replay of the demo fixture at 600x: banner, feed items, a match, screenshots at 1440 and 390 px @mutating", async ({ page, theme }) => {
    test.setTimeout(180_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/watch");
    await shellReady(page);
    const main = page.locator("main");
    await expect(main.getByRole("tab", { name: "Na żywo" })).toHaveAttribute("aria-selected", "true");
    await expect(main.getByTestId("engine-pill")).toBeVisible();
    await expect(main.getByRole("heading", { name: "Strumień na żywo" })).toBeVisible();
    await shot(page, "before", theme);

    const fixture = main.getByTestId("demo-fixture");
    await expect(fixture.locator('option[value="demo"]')).toHaveCount(1);
    await fixture.selectOption("demo");
    await main.getByTestId("demo-speed").selectOption("600");
    await main.getByTestId("demo-start").click();

    const banner = main.getByTestId("replay-banner");
    await expect(banner).toContainText("SYNTETYCZNE / ODTWORZENIE");
    await expect(banner).toContainText("dane nie pochodzą z sieci na żywo");
    await expect(main.getByTestId("engine-pill")).toHaveAttribute("data-state", "replay");
    await expect(banner.getByRole("button")).toHaveCount(0);

    await expect(main.getByTestId("feed-row").first()).toBeVisible({ timeout: 90_000 });
    await expect(main.getByTestId("match-row").first()).toBeVisible({ timeout: 90_000 });
    // replayed rows carry the tag, values are defanged text, never links
    await expect(main.getByTestId("feed-row").first()).toContainText("odtworzenie");
    expect(await main.getByTestId("feed-viewport").locator("a").count()).toBe(0);

    await main.getByTestId("match-row").first().click();
    const detail = main.getByTestId("match-detail");
    await expect(detail).toBeVisible();
    await expect(detail.getByTestId("match-rarity")).toContainText("to nie prawdopodobieństwo");
    await sections(page, theme);

    // filters: only matched rows
    await main.getByTestId("feed-matched").selectOption("1");
    await expect(main.getByTestId("feed-row").first()).toHaveAttribute("data-matched", "true");
    await main.getByTestId("feed-matched").selectOption("");

    await page.setViewportSize({ width: 390, height: 844 });
    await expect(banner).toBeVisible();
    await page.waitForTimeout(300);
    const o = await overflow(page);
    expect(o.doc, "document horizontal overflow at 390 px").toBeLessThanOrEqual(0);
    expect(o.main, "main horizontal overflow at 390 px").toBeLessThanOrEqual(0);
    await sections(page, theme);

    await page.setViewportSize({ width: 1440, height: 900 });
    await main.getByTestId("demo-stop").click();
    await expect(main.getByTestId("engine-pill")).not.toHaveAttribute("data-state", "replay", { timeout: 30_000 });
  });
});
