/**
 * G4 scenario helpers (internal G4 scenario notes, not published). The specs run against our own tests/e2e/g4_server.py
 * (fresh state, auth on, synthetic corpus, models from ~/HydraNexus/models) through ui/e2e/g4.config.ts.
 *
 * Every scenario step goes through `step()`: it runs inside test.step, records PASS/FAIL with the error text into
 * OUT/g4-results.jsonl and keeps going, so one run reports every step; the test fails at the end if any step failed.
 */
import { execFileSync } from "node:child_process";
import { appendFileSync, mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import path from "node:path";
import type { APIRequestContext, Browser, Page } from "playwright/test";
import { expect, shellReady, test } from "./fixtures";

export const OUT = process.env.G4_OUT_DIR ?? "/tmp/hydra-g4";
export const PY = process.env.G4_PYTHON ?? "python3";

export interface G4Info {
  base_url: string;
  pid: number;
  root: string;
  admin_user: string;
  admin_password: string;
  api_token: string;
  base: string;
  g4: { known: string; kit: string; foreign: string; corrupt_png: string; bad_zip: string; pending_zip: string; pending2_zip: string };
}

export function g4Info(): G4Info {
  return JSON.parse(readFileSync(process.env.E2E_SERVER_JSON!, "utf8")) as G4Info;
}

export const shot = (scenario: string, name: string) => {
  mkdirSync(OUT, { recursive: true });
  return path.join(OUT, `${scenario}-${name}.png`);
};

export class Steps {
  failures: string[] = [];
  constructor(private scenario: string) {}

  async step(name: string, fn: () => Promise<void>): Promise<boolean> {
    const t0 = Date.now();
    let ok = true;
    let err = "";
    await test.step(name, async () => {
      try {
        await fn();
      } catch (e) {
        ok = false;
        err = String(e instanceof Error ? e.message : e).split("\n").slice(0, 6).join(" | ").slice(0, 600);
      }
    });
    mkdirSync(OUT, { recursive: true });
    appendFileSync(path.join(OUT, "g4-results.jsonl"), JSON.stringify({ scenario: this.scenario, step: name, ok, ms: Date.now() - t0, error: err || undefined }) + "\n");
    if (!ok) this.failures.push(`${name}: ${err}`);
    console.log(`[g4] ${this.scenario} ${ok ? "PASS" : "FAIL"} ${name}${err ? ` -- ${err}` : ""}`);
    return ok;
  }

  note(name: string, detail: string): void {
    appendFileSync(path.join(OUT, "g4-results.jsonl"), JSON.stringify({ scenario: this.scenario, step: name, note: detail }) + "\n");
    console.log(`[g4] ${this.scenario} NOTE ${name}: ${detail}`);
  }

  finish(): void {
    expect(this.failures, "failed G4 steps").toEqual([]);
  }
}

/** Failed requests (>= 400 and network failures) seen by a page, for the results document. */
export function trackRequests(page: Page, sink: string[]): void {
  page.on("console", (m) => {
    if (m.type() === "error") sink.push(`console.error ${m.text().slice(0, 300)} (${m.location().url ?? ""})`);
  });
  page.on("pageerror", (e) => sink.push(`pageerror ${String(e).slice(0, 300)}`));
  // net::ERR_ABORTED = an SSE stream or fetch cancelled by our own navigation/reload, not a server failure
  page.on("requestfailed", (r) => {
    const e = r.failure()?.errorText ?? "";
    if (!e.includes("ERR_ABORTED")) sink.push(`FAILED ${r.method()} ${r.url()} ${e}`);
  });
  page.on("response", (r) => {
    if (r.status() >= 400) sink.push(`HTTP ${r.status()} ${r.request().method()} ${r.url()}`);
  });
}

export async function newUserPage(browser: Browser, baseURL: string, request: APIRequestContext, role: "viewer" | "analyst",
                                  tag: string): Promise<{ page: Page; username: string; close: () => Promise<void> }> {
  const username = `g4-${tag}-${role}-${Date.now().toString(36)}`;
  const created = await request.post("/v1/auth/users", { data: { username, role } });
  expect(created.status(), await created.text()).toBe(201);
  const { password } = (await created.json()) as { password: string };
  const ctx = await browser.newContext({ baseURL, storageState: { cookies: [], origins: [] }, viewport: { width: 1440, height: 900 }, locale: "pl-PL" });
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

export async function csrfPost(page: Page, url: string, data: unknown) {
  const csrf = (await page.context().cookies()).find((c) => c.name === "hydra_csrf")?.value ?? "";
  return page.request.post(url, { data, headers: { "X-CSRF-Token": csrf } });
}

export async function csrfUpload(page: Page, file: string) {
  const csrf = (await page.context().cookies()).find((c) => c.name === "hydra_csrf")?.value ?? "";
  return page.request.post("/v1/analyse", {
    multipart: { files: { name: path.basename(file), mimeType: "application/octet-stream", buffer: readFileSync(file) } },
    headers: { "X-CSRF-Token": csrf },
  });
}

export interface Assignment {
  incident_id: string;
  decision: "assign" | "new_campaign" | "abstain";
  campaign_id: string | null;
  group_id: string | null;
  reason_pl: string;
  candidates: { campaign_id: string; support: number; best_pair: { a: string; b: string; hypothesis: string; kit_bits?: number; operator_bits?: number } }[];
}

/** Upload files through the Analyse page (file input), refresh the page mid-flow, wait for the job; return it. */
export async function uploadViaUI(page: Page, files: string[], opts: { refreshMidFlow?: boolean; onProgress?: () => Promise<void> } = {}):
  Promise<{ jobId: string; status: number; resumed?: boolean }> {
  await page.goto("/analyse");
  await page.getByRole("tab", { name: "Plik lub ZIP incydentu" }).click();
  await page.getByTestId("analyse-file-input").setInputFiles(files);
  const posted = page.waitForResponse((r) => r.url().endsWith("/api/v1/analyse") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Analizuj", exact: true }).click();
  const resp = await posted;
  const body = (await resp.json().catch(() => ({}))) as { job_id?: string };
  if (resp.status() !== 202) return { jobId: "", status: resp.status() };
  const live = page.getByRole("dialog", { name: "Analiza na żywo" });
  await expect(live).toBeVisible();
  if (opts.onProgress) await opts.onProgress();
  let resumed: boolean | undefined;
  if (opts.refreshMidFlow) {
    await page.reload();
    await shellReady(page);
    // After a reload the page keeps no live panel (job id is component state), but the session job list
    // ("Analiza: ...", sessionStorage) offers the job: reopening it must show the result card of that job.
    const reopen = page.getByRole("button", { name: body.job_id!, exact: true });
    resumed = await reopen.waitFor({ timeout: 10_000 }).then(() => true, () => false);
    if (resumed) {
      await reopen.click();
      await expect(page.getByTestId("analyse-result")).toBeVisible({ timeout: 120_000 });
      await expect(page.getByTestId("analyse-result")).toContainText("zakończone", { timeout: 120_000 });
    }
  }
  return { jobId: body.job_id!, status: resp.status(), resumed };
}

export async function waitJob(request: APIRequestContext, jobId: string, ms = 180_000): Promise<{ job: { status: string; error?: string }; assignment: Assignment | null; result: unknown }> {
  const end = Date.now() + ms;
  for (;;) {
    const r = await request.get(`/v1/analyse/${jobId}`);
    const b = (await r.json()) as { job: { status: string; error?: string }; assignment: Assignment | null; result: unknown };
    if (["done", "failed"].includes(b.job.status)) return b;
    if (Date.now() > end) throw new Error(`job ${jobId} not finished: ${b.job.status}`);
    await new Promise((res) => setTimeout(res, 700));
  }
}

/** Signed dataset export through the UI (classic graph panel), then verify valid / tampered / foreign fingerprint. */
export async function exportAndVerify(page: Page, request: APIRequestContext, s: Steps, scenario: string): Promise<void> {
  let zip = "";
  let fp = "";
  const dir = mkdtempSync(path.join(OUT, `${scenario}-export-`));
  await s.step("signed export downloaded from the UI", async () => {
    await page.goto("/graph");
    await shellReady(page);
    await page.getByRole("link", { name: "widok klasyczny" }).click();
    await expect(page).toHaveURL(/classic=1/);
    const link = page.getByRole("link", { name: "↓ Zbiór danych (ZIP)" });
    await expect(link.first()).toBeVisible({ timeout: 30_000 });
    const fpText = await page.getByTestId("export-signing-key").innerText();
    fp = (/SHA256:\S+/.exec(fpText)?.[0] ?? "").replace(/[.,)]$/, "");
    expect(fp, fpText).toMatch(/^SHA256:/);
    const dl = page.waitForEvent("download");
    await link.first().click();
    const d = await dl;
    zip = path.join(dir, d.suggestedFilename());
    await d.saveAs(zip);
    await page.getByTestId("export-signing-key").screenshot({ path: shot(scenario, "export-signing-key") });
  });
  const verify = async (file: string, fingerprint: string) =>
    (await (await request.post(`/v1/exports/verify?fingerprint=${encodeURIComponent(fingerprint)}`,
      { data: readFileSync(file), headers: { "Content-Type": "application/zip" } })).json()) as { ok: boolean };
  await s.step("verify signed export: PASS", async () => {
    const r = await verify(zip, fp);
    expect(r.ok, JSON.stringify(r).slice(0, 300)).toBe(true);
  });
  await s.step("verify tampered export: FAIL", async () => {
    const tampered = path.join(dir, "tampered.zip");
    execFileSync(PY, ["-c", [
      "import sys, zipfile",
      "src, dst = sys.argv[1], sys.argv[2]",
      "with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as b:",
      "    for i in a.infolist():",
      "        data = a.read(i.filename)",
      "        if i.filename.endswith('incidents.csv'): data = data + b'\\ng4-tamper,x\\n'",
      "        b.writestr(i, data)",
    ].join("\n"), zip, tampered]);
    const r = await verify(tampered, fp);
    expect(r.ok, JSON.stringify(r).slice(0, 300)).toBe(false);
  });
  await s.step("verify with a foreign key fingerprint: FAIL", async () => {
    const r = await verify(zip, "SHA256:" + "A".repeat(43));
    expect(r.ok, JSON.stringify(r).slice(0, 300)).toBe(false);
  });
}

/** Viewer blocked from writes, analyst allowed (own browser sessions). Returns the analyst page (caller closes). */
export async function rolesCheck(browser: Browser, baseURL: string, request: APIRequestContext, s: Steps, scenario: string,
                                 file: string) {
  await s.step("role viewer: upload and decisions blocked (403), read-only note visible", async () => {
    const v = await newUserPage(browser, baseURL, request, "viewer", scenario);
    try {
      expect((await csrfUpload(v.page, file)).status()).toBe(403);
      expect((await csrfPost(v.page, "/v1/reviews", { action: "accept_link", target: { a: "nask-a1", b: "nask-a2" } })).status()).toBe(403);
      await v.page.goto("/analyse");
      await shellReady(v.page);
      await expect(v.page.getByTestId("read-only-note")).toBeVisible();
      await v.page.screenshot({ path: shot(scenario, "viewer-analyse-readonly") });
    } finally {
      await v.close();
    }
  });
  return newUserPage(browser, baseURL, request, "analyst", scenario);
}
