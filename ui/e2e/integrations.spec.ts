/**
 * "Integracje & API" (route /integrations): connection hub and webhooks against the throwaway e2e server
 * (HYDRA_OFFLINE=1, HYDRA_SECRETS_DIR under the temp root, so no test touches ~/.config/hydra).
 * Read-only checks run in the desktop projects; the webhook round trip (adds an endpoint, sends a test to the
 * built-in loopback sink) is @mutating. Optional screenshots: E2E_SHOTS=<dir>.
 */
import { expect, shellReady, test } from "./fixtures";
import type { Page } from "playwright/test";

const SHOTS = process.env.E2E_SHOTS;

async function shot(page: Page, name: string, theme: string) {
  if (!SHOTS) return;
  const w = page.viewportSize()?.width ?? 0;
  await page.screenshot({ path: `${SHOTS}/${theme}-${w}-${name}.png`, fullPage: false });
}

async function noOverflow(page: Page) {
  const o = await page.evaluate(() => {
    const main = document.querySelector("main");
    return { doc: document.documentElement.scrollWidth - document.documentElement.clientWidth, main: main ? main.scrollWidth - main.clientWidth : 0 };
  });
  expect(o.doc, "document horizontal overflow").toBeLessThanOrEqual(0);
  expect(o.main, "main horizontal overflow").toBeLessThanOrEqual(0);
}

test.describe("integrations hub", () => {
  test("hub loads with honest levels, filters, planned has no connect, drawer + dry-run preview", async ({ page, theme }) => {
    await page.goto("/integrations");
    await shellReady(page);
    await expect(page.locator("header h1")).toHaveText("Integracje & API");
    await expect(page.getByRole("tab", { name: "Centrum integracji" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("offline-banner")).toContainText("HYDRA_OFFLINE=1");
    const hub = page.getByTestId("hub-tab");
    await expect(hub.getByTestId("connector-virustotal")).toBeVisible();
    await expect(hub.getByTestId("level-virustotal")).toHaveText("Na żywo (live)");
    await expect(hub.getByTestId("level-misp")).toHaveText("Format eksportu");
    await shot(page, "hub", theme);
    await hub.getByTestId("connector-virustotal").scrollIntoViewIfNeeded();
    await shot(page, "cards", theme);
    await hub.getByTestId("connect-virustotal").click();
    const vtForm = hub.getByTestId("card-virustotal-keyform");
    await expect(vtForm).toBeVisible();
    await expect(vtForm.getByTestId("key-callout-virustotal")).toContainText("Wymaga klucza API");
    await expect(vtForm.getByTestId("card-virustotal-input-VT_API_KEY")).toHaveAttribute("type", "password");
    await expect(vtForm.getByTestId("card-virustotal-input-VT_API_KEY")).toBeFocused();
    await shot(page, "connect", theme);
    await hub.getByTestId("connect-virustotal").click();
    await expect(hub.getByTestId("card-virustotal-keyform")).toHaveCount(0);

    await page.getByTestId("hub-level").selectOption("planned");
    const planned = hub.locator('[data-level="planned"]');
    await expect(planned.first()).toBeVisible();
    expect(await hub.locator('[data-level="live"]').count()).toBe(0);
    const n = await planned.count();
    for (let i = 0; i < n; i++) {
      await expect(planned.nth(i).getByRole("button", { name: /Połącz/ })).toHaveCount(0);
      await expect(planned.nth(i).getByText("Planowane").first()).toBeVisible();
    }
    await page.getByTestId("hub-level").selectOption("");
    await page.getByTestId("hub-search").fill("neo4j");
    await expect(hub.getByTestId("connector-neo4j")).toBeVisible();
    await expect(hub.getByTestId("connector-virustotal")).toHaveCount(0);
    await page.getByTestId("hub-search").fill("");

    await hub.getByTestId("open-csv-export").click();
    const drawer = page.getByTestId("connector-drawer");
    await expect(drawer).toBeVisible();
    await expect(drawer.getByText("Czego nigdy nie wysyłamy")).toBeVisible();
    await drawer.getByTestId("preview-button").click();
    await expect(drawer.getByTestId("preview-result")).toBeVisible();
    await expect(drawer.getByTestId("preview-payload")).not.toBeEmpty();
    await shot(page, "drawer", theme);
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);
  });

  test("tabs are keyboard reachable and synced to ?tab=", async ({ page, theme }) => {
    await page.goto("/integrations");
    await shellReady(page);
    const hubTab = page.getByRole("tab", { name: "Centrum integracji" });
    await hubTab.focus();
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "Webhooki" })).toBeFocused();
    await expect(page).toHaveURL(/tab=webhooks/);
    await expect(page.getByTestId("webhooks-tab")).toBeVisible();
    await shot(page, "webhooks", theme);
  });

  test("no horizontal overflow at 390 px (hub, drawer, webhooks)", async ({ page, theme }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/integrations");
    await shellReady(page);
    await expect(page.getByTestId("connector-virustotal")).toBeVisible();
    await noOverflow(page);
    await shot(page, "hub", theme);
    await page.getByTestId("open-meta-adlibrary").click();
    await expect(page.getByTestId("meta-scheduler")).toBeVisible();
    await shot(page, "drawer", theme);
    await page.keyboard.press("Escape");
    await page.goto("/integrations?tab=webhooks");
    await expect(page.getByTestId("webhooks-tab")).toBeVisible();
    await noOverflow(page);
    await shot(page, "webhooks", theme);
  });
});

test("webhooks: loopback sink endpoint, signed test delivered, signature ok @mutating", async ({ page, theme }) => {
  await page.goto("/integrations?tab=webhooks");
  await shellReady(page);
  const tab = page.getByTestId("webhooks-tab");
  await expect(tab).toBeVisible();
  const name = `e2e sink ${Date.now()}`;
  await page.getByTestId("use-sink").click();
  await page.getByLabel("Nazwa endpointu").fill(name);
  await page.getByTestId("generate-secret").click();
  await expect(page.getByTestId("copy-secret")).toBeVisible();
  await page.getByTestId("webhook-submit").click();
  await expect(tab.getByText(/Dodano endpoint/)).toBeVisible();
  await expect(page.getByTestId("webhook-secret")).toHaveValue("");

  const row = tab.locator('li[data-testid^="endpoint-"]', { hasText: name });
  await expect(row).toBeVisible();
  await expect(row.getByText("loopback")).toBeVisible();
  await row.getByRole("button", { name: "Wyślij test" }).click();
  await expect(row.getByTestId("delivery-status").last()).toHaveText("Dostarczono", { timeout: 20_000 });
  await expect(page.getByTestId("delivery-log").getByText("Dostarczono").first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("sink-receipts").getByTestId("signature-pill").first()).toHaveText("Podpis poprawny", { timeout: 20_000 });
  await shot(page, "webhooks", theme);
});

test("connect: key saved, test blocked offline, explained @mutating", async ({ page, theme }) => {
  await page.goto("/integrations");
  await shellReady(page);
  await page.getByTestId("connect-urlscan").click();
  const form = page.getByTestId("card-urlscan-keyform");
  const input = form.getByTestId("card-urlscan-input-URLSCAN_API_KEY");
  await input.fill("e2e-fake-key-0000000000");
  await form.getByTestId("card-urlscan-submit").click();
  await expect(form.getByTestId("card-urlscan-result")).toHaveAttribute("data-kind", "offline");
  await expect(form.getByTestId("card-urlscan-result")).toContainText("Klucz zapisany");
  await expect(input).toHaveValue("");
  const stored = await page.evaluate(() => JSON.stringify(window.localStorage));
  expect(stored).not.toContain("e2e-fake-key");
  await shot(page, "connect-result", theme);
  await expect(page.getByTestId("status-urlscan")).toHaveText("Offline");
  await expect(form.getByTestId("card-urlscan-saved-URLSCAN_API_KEY")).toContainText("zapisany");
});

test("preset: missing key → inline form → success state (mocked connect endpoint) @mutating", async ({ page, theme }) => {
  // The e2e server is offline, so a real provider test cannot succeed; the connect endpoint is mocked for this one
  // request to show the success path of the inline form. Nothing reaches the provider.
  await page.route("**/api/v1/integrations/virustotal/connect", async (route) => {
    const body = route.request().postDataJSON() as { secrets?: Record<string, string> };
    expect(body.secrets?.VT_API_KEY).toBe("e2e-vt-key-0000000000");
    await route.fulfill({ json: {
      ok: true,
      steps: [{ id: "secret", ok: true, message_pl: "Klucz zapisany." }, { id: "test", ok: true, message_pl: "Test udany." }, { id: "enable", ok: true, message_pl: "Konektor włączony." }],
      connector: { id: "virustotal", secrets: [{ name: "VT_API_KEY", configured: true, source: "file", last4: "0000" }] },
    } });
  });
  await page.goto("/integrations");
  await shellReady(page);
  const card = page.getByTestId("preset-reputation");
  await expect(card.getByTestId("preset-summary-reputation")).toContainText("gotowe");
  await expect(card.getByTestId("apply-reputation")).toContainText("Uzupełnij klucze");
  await expect(card.getByTestId("preset-state-reputation-virustotal")).toHaveText(/Wymaga klucza/);
  await card.getByTestId("preset-connect-reputation-virustotal").click();
  const form = card.getByTestId("preset-reputation-virustotal-keyform");
  const input = form.getByTestId("preset-reputation-virustotal-input-VT_API_KEY");
  await expect(input).toBeFocused();
  await input.fill("e2e-vt-key-0000000000");
  await form.getByTestId("preset-reputation-virustotal-submit").click();
  await expect(form.getByTestId("preset-reputation-virustotal-result")).toHaveAttribute("data-kind", "ok");
  await expect(form.getByTestId("preset-reputation-virustotal-result")).toContainText("Połączono");
  await expect(input).toHaveValue("");
  await shot(page, "preset-key", theme);
  await noOverflow(page);
});

test("preset cards: apply buttons aligned, results without '.;' @mutating", async ({ page, theme }) => {
  await page.goto("/integrations");
  await shellReady(page);
  // cert-misp and siem share the first grid row at 1440 px: their buttons must sit at the same height
  const a = await page.getByTestId("apply-cert-misp").boundingBox();
  const b = await page.getByTestId("apply-siem").boundingBox();
  expect(a && b).toBeTruthy();
  if (a!.x !== b!.x) expect(Math.abs(a!.y - b!.y)).toBeLessThanOrEqual(1);
  await page.getByTestId("apply-cert-misp").click();
  const res = page.getByTestId("preset-result-cert-misp");
  await expect(res.locator('[data-testid^="preset-msg-"]').first()).not.toBeEmpty();
  expect(await res.textContent()).not.toMatch(/\.;/);
  await shot(page, "presets", theme);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByTestId("preset-connect-reputation-abuseipdb").click();
  await expect(page.getByTestId("preset-reputation-abuseipdb-keyform")).toBeVisible();
  await noOverflow(page);
  await page.getByTestId("preset-reputation-abuseipdb-keyform").scrollIntoViewIfNeeded();
  await shot(page, "preset-keyform", theme);
});
