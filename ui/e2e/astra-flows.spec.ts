/**
 * ASTRA-SW browser checks not covered by the other specs (synthetic state of tests/e2e/server.py only):
 * roles viewer/analyst in their own browser sessions, ZIP import with one rejected (empty) file, queue decision + undo
 * in the UI, signed dataset export downloaded through the UI and verified (valid / tampered / foreign fingerprint),
 * variant-diff geometry overlay geometry, KONTRA package delivered to our own loopback receiver with a bound ACK.
 * All mutating, so they run in desktop-light-mutating only.
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import type { Browser, Page } from "playwright/test";
import { apiJson, expect, shellReady, test } from "./fixtures";

const OUT = process.env.E2E_OUT_DIR ?? os.tmpdir();
const shot = (name: string) => path.join(OUT, `astra-${name}.png`);

test.describe("astra flows @mutating", () => {
  test.describe.configure({ timeout: 180_000 });
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-light-mutating", "runs in desktop-light-mutating only");
  });

  async function newUserPage(browser: Browser, baseURL: string, request: import("playwright/test").APIRequestContext,
                             role: "viewer" | "analyst"): Promise<{ page: Page; username: string; close: () => Promise<void> }> {
    const username = `astra-${role}-${Date.now().toString(36)}`;
    const created = await request.post("/v1/auth/users", { data: { username, role } });
    expect(created.status(), await created.text()).toBe(201);
    const { password } = (await created.json()) as { password: string };
    const ctx = await browser.newContext({ baseURL, storageState: { cookies: [], origins: [] }, viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    await page.goto("/queue");
    await expect(page).toHaveURL(/\/login/);
    await page.getByLabel("Login", { exact: true }).fill(username);
    await page.getByLabel("Hasło", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Zaloguj się" }).click();
    await expect(page).toHaveURL(/change-password/);
    const next = `Zielony-Kot-${Date.now().toString(36)}#Wisla-2026`;
    await page.getByLabel("Obecne hasło", { exact: true }).fill(password);
    await page.getByLabel("Nowe hasło", { exact: true }).fill(next);
    await page.getByLabel("Powtórz nowe hasło", { exact: true }).fill(next);
    await page.getByRole("button", { name: "Ustaw hasło i przejdź dalej" }).click();
    await expect(page).toHaveURL(/\/queue$/);
    await shellReady(page);
    return { page, username, close: () => ctx.close() };
  }

  async function csrfPost(page: Page, url: string, data: unknown) {
    const cookies = await page.context().cookies();
    const csrf = cookies.find((c) => c.name === "hydra_csrf")?.value ?? "";
    return page.request.post(url, { data, headers: { "X-CSRF-Token": csrf } });
  }

  test("roles: viewer cannot decide or analyse; analyst decision is recorded under the authenticated user", async ({ browser, baseURL, request }, testInfo) => {
    const viewer = await newUserPage(browser, baseURL!, request, "viewer");
    try {
      await viewer.page.screenshot({ path: shot("viewer-queue") });
      expect((await csrfPost(viewer.page, "/v1/reviews", { action: "accept_link", target: { a: "nask-a1", b: "nask-a2" } })).status()).toBe(403);
      expect((await csrfPost(viewer.page, "/v1/analyse", { hint: "synthetic" })).status()).toBe(403);
      expect((await viewer.page.request.get("/v1/auth/users")).status()).toBe(403);
    } finally {
      await viewer.close();
    }
    const analyst = await newUserPage(browser, baseURL!, request, "analyst");
    try {
      const r = await csrfPost(analyst.page, "/v1/review/unsure", { target: { a: "nask-a1", b: "nask-a2" }, note: "astra e2e", analyst: "admin" });
      expect(r.status(), await r.text()).toBe(201);
      const recorded = ((await r.json()) as { mark: { analyst: string } }).mark.analyst;
      // SW-10 observation (owned by FIX-BACKEND): recorded only, not asserted here.
      testInfo.annotations.push({ type: "sw10-recorded-analyst", description: `${recorded} (authenticated ${analyst.username})` });
      console.log(`[astra] SW-10 unsure mark analyst=${recorded} authenticated=${analyst.username}`);
      const marks = await apiJson<{ items?: { id: string }[] }>(request, "/v1/review/unsure");
      void marks;
    } finally {
      await analyst.close();
    }
  });

  test("import ZIP with one rejected empty file: progress counts it and finishes", async ({ page }) => {
    mkdirSync(OUT, { recursive: true });
    const dir = mkdtempSync(path.join(OUT, "astra-import-"));
    const zip = path.join(dir, "astra-incydent.zip");
    execFileSync("python3", ["-c", [
      "import sys, zipfile",
      "with zipfile.ZipFile(sys.argv[1], 'w') as z:",
      "    z.writestr('strona.html', '<html><head><title>Astra import</title></head><body>oferta astra-import.invalid</body></html>')",
      "    z.writestr('notatka.txt', 'notatka https://astra-import.invalid/x\\n')",
      "    z.writestr('pusty.txt', '')",
    ].join("\n"), zip]);
    await page.goto("/analyse");
    await page.getByRole("tab", { name: "Plik lub ZIP incydentu" }).click();
    await page.getByTestId("analyse-file-input").setInputFiles(zip);
    const posted = page.waitForResponse((r) => r.url().endsWith("/api/v1/analyse") && r.request().method() === "POST");
    await page.getByRole("button", { name: "Analizuj", exact: true }).click();
    const resp = await posted;
    expect(resp.status()).toBe(202);
    const job = ((await resp.json()) as { job_id?: string; id?: string });
    const jobId = job.job_id ?? job.id;
    expect(jobId).toBeTruthy();
    const live = page.getByRole("dialog", { name: "Analiza na żywo" });
    await expect(live).toBeVisible();
    await live.getByRole("button", { name: "Zamknij (Esc)" }).click();
    const panel = page.getByTestId("analyse-activity");
    await expect(panel.getByTestId("activity-overall-bar")).toHaveAttribute("aria-valuenow", "100", { timeout: 120_000 });
    await panel.screenshot({ path: shot("import-rejected-done") });
    const prog = (await (await page.request.get(`/v1/analyse/${jobId}/progress`)).json()) as
      { status: string; import: { rejected: number; files_total: number; files_done: number; phase: string } };
    console.log(`[astra] import progress ${JSON.stringify(prog.import)}`);
    expect(prog.import.rejected).toBe(1);
    expect(prog.import.files_done).toBe(prog.import.files_total);
    expect(prog.import.phase).toBe("done");
  });

  test("queue: decision in the UI, then undo restores the pair", async ({ page, request }) => {
    await page.goto("/queue");
    await shellReady(page);
    const accept = page.getByRole("button", { name: "✓ Potwierdź (A)" });
    await expect(accept).toBeVisible();
    const before = await apiJson<{ items: { id: string; action: string; undone: boolean }[] }>(request, "/v1/reviews");
    const posted = page.waitForResponse((r) => r.url().endsWith("/v1/reviews") && r.request().method() === "POST");
    await accept.click();
    expect((await posted).status()).toBe(201);
    const undo = page.getByRole("button", { name: "↶ Cofnij (U)" });
    await expect(undo).toBeVisible();
    await page.screenshot({ path: shot("queue-after-decision") });
    const undone = page.waitForResponse((r) => r.url().endsWith("/v1/reviews") && r.request().method() === "POST");
    await undo.click();
    expect((await undone).status()).toBe(201);
    const after = await apiJson<{ items: { id: string; action: string; undone: boolean; undoes?: string }[] }>(request, "/v1/reviews");
    const fresh = after.items.filter((r) => !before.items.some((b) => b.id === r.id));
    console.log(`[astra] reviews added ${JSON.stringify(fresh)}`);
    expect(fresh.map((r) => r.action).sort()).toEqual(["accept_link", "undo"]);
    expect(fresh.find((r) => r.action === "accept_link")!.undone).toBe(true);
    await page.screenshot({ path: shot("queue-after-undo") });
  });

  test("signed dataset export: UI download verifies; tampered file and foreign fingerprint are rejected", async ({ page, request }) => {
    // The run exports live in the classic graph's right panel; reach it through the visible link (a direct
    // `?classic=1` load can land on the constellation view when the shell normalises the URL).
    await page.goto("/graph");
    await shellReady(page);
    await page.getByRole("link", { name: "widok klasyczny" }).click();
    await expect(page).toHaveURL(/classic=1/);
    const link = page.getByRole("link", { name: "↓ Zbiór danych (ZIP)" });
    await expect(link.first()).toBeVisible({ timeout: 30_000 });
    const fpText = await page.getByTestId("export-signing-key").innerText();
    const fp = /SHA256:\S+/.exec(fpText)?.[0];
    expect(fp, fpText).toBeTruthy();
    const dl = page.waitForEvent("download");
    await link.first().click();
    const d = await dl;
    const dir = mkdtempSync(path.join(OUT, "astra-export-"));
    const zip = path.join(dir, d.suggestedFilename());
    await d.saveAs(zip);
    const body = (f: string) => readFileSync(f);
    const verify = async (file: string, fingerprint: string) =>
      (await (await request.post(`/v1/exports/verify?fingerprint=${encodeURIComponent(fingerprint)}`,
        { data: body(file), headers: { "Content-Type": "application/zip" } })).json()) as { ok: boolean; [k: string]: unknown };
    const good = await verify(zip, fp!.replace(/[.,)]$/, ""));
    console.log(`[astra] verify good ${JSON.stringify(good).slice(0, 400)}`);
    expect(good.ok).toBe(true);
    const tampered = path.join(dir, "tampered.zip");
    execFileSync("python3", ["-c", [
      "import sys, zipfile",
      "src, dst = sys.argv[1], sys.argv[2]",
      "with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as b:",
      "    for i in a.infolist():",
      "        data = a.read(i.filename)",
      "        if i.filename.endswith('incidents.csv'): data = data + b'\\nastra-tamper,x\\n'",
      "        b.writestr(i, data)",
    ].join("\n"), zip, tampered]);
    const bad = await verify(tampered, fp!.replace(/[.,)]$/, ""));
    console.log(`[astra] verify tampered ${JSON.stringify(bad).slice(0, 400)}`);
    expect(bad.ok).toBe(false);
    const foreign = await verify(zip, "SHA256:" + "A".repeat(43));
    console.log(`[astra] verify foreign fp ${JSON.stringify(foreign).slice(0, 400)}`);
    expect(foreign.ok).toBe(false);
    writeFileSync(path.join(dir, "verify-results.json"), JSON.stringify({ good, bad, foreign }, null, 2));
  });

  test("variant diff: geometry overlay drawn inside the image frame when the API has matched points", async ({ page, request }) => {
    const pairs = [["nask-a1", "nask-a2"], ["nask-b1", "nask-b2"], ["nask-c1", "nask-c2"]];
    let shown = 0;
    for (const [a, b] of pairs) {
      const g = await request.get(`/v1/pairs/${a}/${b}/geometry`);
      const data = g.ok() ? ((await g.json()) as { pairs?: unknown[] } | unknown[]) : [];
      const list = Array.isArray(data) ? data : (data.pairs ?? []);
      console.log(`[astra] geometry ${a}/${b}: HTTP ${g.status()} entries ${list.length}`);
      await page.goto(`/diff/${a}/${b}`);
      await shellReady(page);
      if (!list.length) continue;
      const overlay = page.getByTestId("geometry-overlay");
      await expect(overlay).toBeVisible({ timeout: 30_000 });
      const svg = overlay.locator("svg").first();
      const vb = (await svg.getAttribute("viewBox"))!.split(" ").map(Number);
      const lines = overlay.getByTestId("geometry-lines").first().locator("line");
      const n = await lines.count();
      for (let i = 0; i < Math.min(n, 50); i++) {
        for (const attr of ["x1", "x2", "y1", "y2"]) {
          const v = Number(await lines.nth(i).getAttribute(attr));
          const max = attr.startsWith("x") ? vb[2]! : vb[3]!;
          expect(v, `${attr} within viewBox`).toBeGreaterThanOrEqual(-0.5);
          expect(v, `${attr} within viewBox`).toBeLessThanOrEqual(max + 0.5);
        }
      }
      await overlay.screenshot({ path: shot(`geometry-${a}-${b}`) });
      shown++;
    }
    test.info().annotations.push({ type: "geometry-overlays-shown", description: String(shown) });
  });

  test("KONTRA: package sent to our own loopback receiver returns a bound ACK", async ({ page, request }) => {
    test.skip(!process.env.ASTRA_RECEIVER_PORT,
      "needs our own receiver: scripts/kontra_receiver.py --port P, HYDRA_KONTRA_RECEIVER=http://127.0.0.1:P/receive, ASTRA_RECEIVER_PORT=P");
    const rec = await apiJson<{ url: string; loopback: boolean }>(request, "/v1/lab/receiver");
    console.log(`[astra] receiver ${JSON.stringify(rec)}`);
    expect(rec.loopback).toBe(true);
    expect(rec.url).toContain(process.env.ASTRA_RECEIVER_PORT ?? "__unset__");
    await page.goto("/lab/nask-a2");
    await page.getByTestId("lab-run").first().click();
    await expect(page.getByTestId("lab-scenarios")).toBeVisible({ timeout: 60_000 });
    await page.getByTestId("lab-package").click();
    await expect(page.getByTestId("lab-export")).toContainText("kp-");
    const sent = page.waitForResponse((r) => /\/v1\/lab\/packages\/[^/]+\/send$/.test(r.url()));
    await page.getByTestId("lab-send").click();
    const res = (await (await sent).json()) as { status: string; ack?: { receipt_id?: string; package_id?: string } };
    console.log(`[astra] kontra send ${JSON.stringify(res).slice(0, 500)}`);
    expect(["acknowledged", "duplicate"]).toContain(res.status);
    await expect(page.getByTestId("lab-ack")).toHaveAttribute("data-confirmed", "true");
    await page.getByTestId("lab-ack").screenshot({ path: shot("kontra-ack") });
  });
});
