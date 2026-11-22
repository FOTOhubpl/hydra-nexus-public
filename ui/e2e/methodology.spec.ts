/**
 * /methodology explainer (desktop projects; 390 px checked by resizing inside the test): TOC with 11 entries,
 * every section present, the illustrative calculator reproduces the METHODOLOGY §5 example, no sideways overflow.
 * Optional screenshots: E2E_SHOTS=<dir> (e.g. ../qa/methodology).
 */
import { expect, shellReady, test } from "./fixtures";

const SHOTS = process.env.E2E_SHOTS;

test("methodology: TOC, sections, calculator, 390 px", async ({ page }, info) => {
  const theme = info.project.name.includes("dark") ? "dark" : "light";
  await page.goto("/methodology");
  await shellReady(page);
  const main = page.locator("main");
  await expect(main.getByRole("heading", { name: "Metoda w skrócie" })).toBeVisible();
  await expect(main.getByTestId("method-toc").getByRole("link")).toHaveCount(11);
  await expect(main.getByTestId("calc-bits")).toHaveText("10,5");
  await main.getByRole("link", { name: /Hipotezy par/ }).click();
  await expect(main.getByTestId("method-section-m-pary")).toBeInViewport();
  if (SHOTS) {
    await page.screenshot({ path: `${SHOTS}/${theme}-1440-methodology.png`, fullPage: false });
    await main.getByTestId("method-section-m-rzadkosc").scrollIntoViewIfNeeded();
    await main.getByTestId("method-section-m-rzadkosc").locator("summary").click();
    await main.getByTestId("method-section-m-rzadkosc").screenshot({ path: `${SHOTS}/${theme}-1440-rarity.png` });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/methodology");
  await shellReady(page);
  await expect(main.getByTestId("rarity-calculator")).toBeAttached();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/${theme}-390-methodology.png`, fullPage: false });
});
