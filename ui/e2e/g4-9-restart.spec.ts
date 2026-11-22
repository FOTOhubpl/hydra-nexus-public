/**
 * G4 restart check (runs last), on OUR OWN g4 server only (pid from server.json):
 *  1. graceful restart: submit a corpus job, SIGTERM the API while the job runs, start tests/e2e/g4_server.py
 *     --restart on the same root. Expected: the job is drained (finished before exit), state + reviews survive.
 *  2. crash restart: submit another corpus job, SIGKILL the API while it runs, restart. Expected: campaigns,
 *     members, reviews identical; the job pending at restart is reported failed / INTERRUPTED with a re-run hint.
 */
import { spawn } from "node:child_process";
import { existsSync, openSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { request as pwRequest, type APIRequestContext } from "playwright/test";
import { apiJson, expect, test } from "./fixtures";
import { type G4Info, PY, Steps, g4Info, shot } from "./g4-helpers";

const SC = "g4-9-restart";
const WORKTREE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

interface Snap { campaigns: string[]; reviews: number; incidents: number }

async function snapshot(req: APIRequestContext): Promise<Snap> {
  const c = await apiJson<{ items: { id: string; members: string[] }[] }>(req, "/v1/campaigns");
  const r = await apiJson<{ total: number }>(req, "/v1/reviews");
  const i = await apiJson<{ total?: number; items: unknown[] }>(req, "/v1/incidents?limit=1000");
  return { campaigns: c.items.map((x) => `${x.id}:${[...x.members].sort().join(",")}`).sort(), reviews: r.total, incidents: i.total ?? i.items.length };
}

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function api(info: G4Info): Promise<APIRequestContext> {
  return pwRequest.newContext({ baseURL: info.base_url, extraHTTPHeaders: { Authorization: `Bearer ${info.api_token}` } });
}

async function submitRunning(req: APIRequestContext, zip: string, s: Steps, tag: string): Promise<string> {
  const r = await req.post("/v1/analyse", { multipart: { files: { name: path.basename(zip), mimeType: "application/zip", buffer: readFileSync(zip) } } });
  expect(r.status()).toBe(202);
  const jobId = ((await r.json()) as { job_id: string }).job_id;
  let st = "";
  for (let i = 0; i < 50; i++) {
    st = (await apiJson<{ status: string }>(req, `/v1/jobs/${jobId}`)).status;
    if (st === "running") break;
    await new Promise((res) => setTimeout(res, 100));
  }
  s.note(`${tag}: job before restart`, `${jobId} ${st}`);
  expect(st).toBe("running");
  return jobId;
}

async function restart(info: G4Info, signal: "SIGTERM" | "SIGKILL"): Promise<{ info: G4Info; stopMs: number }> {
  const ready = process.env.E2E_SERVER_JSON!;
  const t0 = Date.now();
  process.kill(info.pid, signal);
  for (let i = 0; i < 600 && alive(info.pid); i++) await new Promise((res) => setTimeout(res, 100));
  const stopMs = Date.now() - t0;
  expect(alive(info.pid), "old API process stopped").toBe(false);
  rmSync(ready, { force: true });
  const log = openSync(path.join(path.dirname(info.root), `launcher-restart-${signal}.log`), "w");
  const child = spawn(PY, ["-m", "tests.e2e.g4_server", "--root", info.root, "--ready", ready, "--ui-dist", path.join(WORKTREE, "ui", "dist"), "--restart"],
    { cwd: WORKTREE, detached: true, stdio: ["ignore", log, log], env: { ...process.env } });
  child.unref();
  for (let i = 0; i < 600 && !existsSync(ready); i++) await new Promise((res) => setTimeout(res, 200));
  expect(existsSync(ready), "restarted server ready").toBe(true);
  await new Promise((res) => setTimeout(res, 300));
  return { info: g4Info(), stopMs };
}

test("G4 restart: state survives, a job pending at restart is reported", async ({ browser }) => {
  test.setTimeout(600_000);
  const s = new Steps(SC);
  let info = g4Info();
  let req = await api(info);
  let before: Snap | null = null;
  let jobId = "";
  try {
    await s.step("graceful restart (SIGTERM) while a corpus job runs: job drained or INTERRUPTED, reviews survive", async () => {
      before = await snapshot(req);
      jobId = await submitRunning(req, info.g4.pending_zip, s, "SIGTERM");
      const r = await restart(info, "SIGTERM");
      s.note("SIGTERM restart", `stop took ${r.stopMs} ms; ${info.base_url} -> ${r.info.base_url}`);
      info = r.info;
      await req.dispose();
      req = await api(info);
      const j = await apiJson<{ status: string; error_code?: string }>(req, `/v1/jobs/${jobId}`);
      s.note("SIGTERM: job after restart", `${j.status} ${j.error_code ?? ""}`);
      expect(["done", "failed"]).toContain(j.status);
      if (j.status === "failed") expect(j.error_code).toBe("INTERRUPTED");
      const now = await snapshot(req);
      expect(now.reviews).toBe(before!.reviews);
      expect(now.incidents).toBeGreaterThanOrEqual(before!.incidents);
    });

    await s.step("crash restart (SIGKILL) while a corpus job runs: campaigns, members and reviews identical", async () => {
      before = await snapshot(req);
      jobId = await submitRunning(req, info.g4.pending2_zip, s, "SIGKILL");
      const r = await restart(info, "SIGKILL");
      s.note("SIGKILL restart", `${info.base_url} -> ${r.info.base_url}`);
      info = r.info;
      await req.dispose();
      req = await api(info);
      const now = await snapshot(req);
      s.note("SIGKILL snapshot", `campaigns ${before!.campaigns.length} -> ${now.campaigns.length}, reviews ${before!.reviews} -> ${now.reviews}, incidents ${before!.incidents} -> ${now.incidents}`);
      expect(now.campaigns).toEqual(before!.campaigns);
      expect(now.reviews).toBe(before!.reviews);
    });

    await s.step("job pending at the crash reported as failed / INTERRUPTED with a re-run hint", async () => {
      const j = await apiJson<{ status: string; error_code?: string; error?: string }>(req, `/v1/jobs/${jobId}`);
      s.note("SIGKILL: job after restart", `${j.status} ${j.error_code} ${j.error}`);
      expect(j.status).toBe("failed");
      expect(j.error_code).toBe("INTERRUPTED");
      const a = await apiJson<{ job: { status: string; error_code?: string } }>(req, `/v1/analyse/${jobId}`);
      expect(a.job.error_code).toBe("INTERRUPTED");
    });

    await s.step("browser: admin logs in on the restarted server and sees the campaigns", async () => {
      const ctx = await browser.newContext({ baseURL: info.base_url, storageState: { cookies: [], origins: [] }, viewport: { width: 1440, height: 900 }, locale: "pl-PL" });
      const page = await ctx.newPage();
      try {
        await page.goto("/campaigns");
        await page.getByLabel("Login", { exact: true }).fill(info.admin_user);
        await page.getByLabel("Hasło", { exact: true }).fill(info.admin_password);
        await page.getByRole("button", { name: "Zaloguj się" }).click();
        await expect(page.getByRole("main").getByText("Inwestycja PKO Bonus").first()).toBeVisible({ timeout: 30_000 });
        await page.screenshot({ path: shot(SC, "campaigns-after-restart") });
      } finally {
        await ctx.close();
      }
    });
  } finally {
    await req.dispose();
  }
  s.finish();
});
