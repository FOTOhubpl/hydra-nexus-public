/**
 * BONUS-AUTO "Automatyczne wyszukiwanie" (/watch?view=auto) on the SYNTHETIC replay fixture "auto-search-demo"
 * against the throwaway e2e server (HYDRA_OFFLINE=1: no request leaves the machine; base corpus = synthetic e2e
 * campaigns). The jury click path: Monitoring → Automatyczne wyszukiwanie → "Demonstracja offline" → two cycles
 * (new / changed / unchanged / ended ads) → the probe ad syn-auto-1 is assigned to an EXISTING campaign → the row in
 * "Nowe materiały powiązane z wcześniej wykrytymi kampaniami" → the campaign card section → the top-bar entry.
 * Screenshots go to AUTO_QA_DIR (default /tmp/hydra-qa/bonus-auto/).
 * @mutating: the replay writes synthetic ads, analyses, incidents and watch matches into the server state.
 */
import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, shellReady, test } from "./fixtures";
import type { Page } from "playwright/test";

const QA_DIR = process.env.AUTO_QA_DIR ?? "/tmp/hydra-qa/bonus-auto/";

async function shot(page: Page, name: string) {
  mkdirSync(QA_DIR, { recursive: true });
  const w = page.viewportSize()?.width ?? 0;
  await page.screenshot({ path: path.join(QA_DIR, `${w}-${name}.png`) });
}

test.describe("automatic search loop @mutating", () => {
  test("offline synthetic replay: new material linked to an existing campaign on the overview, campaign card and top bar @mutating", async ({ page }) => {
    test.setTimeout(300_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/watch?view=auto");
    await shellReady(page);
    const main = page.locator("main");
    await expect(main.getByRole("tab", { name: "Automatyczne wyszukiwanie" })).toHaveAttribute("aria-selected", "true");
    await expect(main.getByTestId("auto-rules")).toBeVisible();
    await expect(main.getByTestId("auto-loop")).toContainText("Okresowe aktualizowanie");
    await shot(page, "01-overview-live");

    await main.getByTestId("auto-demo").click();
    await expect(page).toHaveURL(/mode=synthetic/);
    await expect(main.getByTestId("meta-mode-banner")).toContainText("DANE SYNTETYCZNE");

    // two cycles recorded (the replay tick interval is 1 s at speed 60)
    await expect(main.getByTestId("auto-cycle-row")).toHaveCount(2, { timeout: 120_000 });
    await expect(main.getByTestId("cycle-new").first()).toHaveText("1");
    await expect(main.getByTestId("cycle-changed").first()).toHaveText("1");

    // the probe ad is analysed and assigned to a campaign of the base run
    const row = main.getByTestId("auto-linked").locator('[data-testid="linked-ad-row"][data-ad-id="syn-auto-1"]');
    await expect(row).toBeVisible({ timeout: 180_000 });
    await expect(row).toContainText("DANE SYNTETYCZNE");
    await expect(row.getByTestId("linked-ad-incident")).toHaveAttribute("href", /\/incident\/metaad-syn-/);
    await main.evaluate((m) => m.scrollTo(0, 0));
    await shot(page, "02-overview-synthetic");
    await row.scrollIntoViewIfNeeded();
    await shot(page, "03-linked-ad");
    await main.getByTestId("auto-cycles").scrollIntoViewIfNeeded();
    await shot(page, "04-cycles");
    await main.getByTestId("auto-rules").scrollIntoViewIfNeeded();
    await shot(page, "05-rules");

    // top-bar notification from the meta.analysis stream event
    await page.getByTestId("notifications").first().click();
    await expect(page.getByText(/Nowa reklama Meta powiązana z kampanią/).first()).toBeVisible({ timeout: 30_000 });
    await shot(page, "06-notification");
    await page.keyboard.press("Escape");

    // campaign card section
    const campaignHref = await row.locator('a[href^="/campaigns/"]').first().getAttribute("href");
    expect(campaignHref).toBeTruthy();
    await page.goto(campaignHref!);
    await shellReady(page);
    const section = page.getByTestId("campaign-meta-links");
    await section.scrollIntoViewIfNeeded();
    await expect(section.locator('[data-testid="linked-ad-row"][data-ad-id="syn-auto-1"]')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/Nowe reklamy z biblioteki Meta powiązane z tą kampanią/)).toBeVisible();
    await shot(page, "07-campaign-card");

    // the config form exposes the rules
    await page.goto("/watch?view=meta");
    await shellReady(page);
    const rules = page.getByTestId("meta-rules");
    await rules.scrollIntoViewIfNeeded();
    await expect(rules).toContainText("Data wystąpienia od");
    await expect(rules).toContainText("Wzorce nazwy strony");
    await shot(page, "08-config-rules");
  });
});
