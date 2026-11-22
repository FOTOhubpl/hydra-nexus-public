/**
 * /watch?view=meta "Reklamy Meta" (META-COLLECT) full scenario on the SYNTHETIC replay fixture "meta-demo" against
 * the throwaway e2e server (HYDRA_OFFLINE=1: no request leaves the machine). The replay goes through the production
 * endpoints and code path: WATCH-RT replay -> Meta adapter -> collector store -> durable analysis queue -> worker ->
 * analysis + report. The page must label every ad DANE SYNTETYCZNE; the report JSON must say mode "synthetic".
 * @mutating: the replay writes synthetic ads, revisions, analyses and incidents into the server state.
 */
import { expect, shellReady, test } from "./fixtures";
import { apiAuthHeaders } from "./authState";

test.describe("meta ads collector @mutating", () => {
  test("synthetic replay: ad labelled DANE SYNTETYCZNE, analysis with three separate sections, report JSON mode synthetic @mutating", async ({ page, playwright }) => {
    test.setTimeout(240_000);
    const api = await playwright.request.newContext({ baseURL: process.env.E2E_BASE_URL, extraHTTPHeaders: apiAuthHeaders() });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/watch?view=meta&mode=synthetic");
    await shellReady(page);
    const main = page.locator("main");
    await expect(main.getByRole("tab", { name: "Reklamy Meta" })).toHaveAttribute("aria-selected", "true");
    await expect(main.getByTestId("meta-mode-banner")).toContainText("DANE SYNTETYCZNE");

    const started = await api.post("/v1/watch/rt/replay", { data: { fixture: "meta-demo", speed: 3600 } });
    expect(started.status(), await started.text()).toBe(200);

    const row = main.getByTestId("meta-ad-row").first();
    await expect(row).toBeVisible({ timeout: 90_000 });
    await expect(row.getByTestId("mode-badge")).toHaveText("DANE SYNTETYCZNE");
    await row.click();
    const detail = main.getByTestId("meta-ad-detail");
    await expect(detail).toBeVisible();
    await expect(detail.getByTestId("meta-revisions")).toBeVisible();

    // the worker runs outside the poll transaction: wait for a finished (done / partial) analysis
    const analysis = detail.getByTestId("meta-analysis");
    await expect(analysis).toBeVisible({ timeout: 120_000 });
    await expect(analysis).toHaveAttribute("data-status", /^(done|partial)$/, { timeout: 120_000 });
    await expect(analysis.getByTestId("analysis-correlation")).toBeVisible();
    await expect(analysis.getByTestId("analysis-triage")).toBeVisible();
    await expect(analysis.getByTestId("analysis-manipulation")).toBeVisible();
    await expect(main.getByTestId("meta-mode-banner")).toContainText("DANE SYNTETYCZNE");

    const href = await analysis.getByTestId("report-json").getAttribute("href");
    expect(href).toBeTruthy();
    const rep = await page.request.get(href!);
    expect(rep.status(), `GET ${href}`).toBe(200);
    const body = await rep.text();
    expect((JSON.parse(body) as { mode?: string }).mode, "report mode").toBe("synthetic");

    await api.post("/v1/watch/rt/replay/stop");
    await api.dispose();
  });
});
