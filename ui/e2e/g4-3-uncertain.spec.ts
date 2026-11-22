/**
 * G4 scenario 3, uncertain / foreign material: an English page + photo with no shared indicator. Expected: a new
 * campaign or a justified abstain; then the analyst works the review queue in the GUI (decision recorded under the
 * real authenticated user), the decision is applied (re-computation: a new revision run) and shows up in the history.
 * Also roles, corrupt input, refresh mid-flow, signed export checks.
 */
import { readdirSync } from "node:fs";
import path from "node:path";
import { apiJson, expect, shellReady, test } from "./fixtures";
import { type Assignment, Steps, csrfUpload, exportAndVerify, g4Info, rolesCheck, shot, trackRequests, uploadViaUI, waitJob } from "./g4-helpers";

const SC = "g4-3-uncertain";

interface QueueItem { key: string; kinds: string[]; a: string | null; b: string | null; campaigns: string[] }

test("G4-3 uncertain material: new campaign / abstain, queue decision by the real analyst, recomputation, history", async ({ browser, baseURL, request }) => {
  test.setTimeout(600_000);
  const s = new Steps(SC);
  const info = g4Info();
  const failed: string[] = [];
  const files = readdirSync(info.g4.foreign).sort().map((f) => path.join(info.g4.foreign, f));
  const analyst = await rolesCheck(browser, baseURL!, request, s, SC, files[0]!);
  const page = analyst.page;
  trackRequests(page, failed);
  let asg: Assignment | null = null;
  try {
    await s.step("invalid input: corrupt PNG gives a justified abstain that lands in the queue", async () => {
      const r = await csrfUpload(page, info.g4.corrupt_png);
      expect(r.status()).toBe(202);
      const done = await waitJob(request, ((await r.json()) as { job_id: string }).job_id);
      expect(done.assignment?.decision).toBe("abstain");
      const q = await apiJson<{ items: QueueItem[] }>(request, "/v1/review/queue");
      expect(q.items.some((i) => i.key === `inc:${done.assignment!.incident_id}`)).toBe(true);
    });

    await s.step("analyst imports the foreign material through the GUI; refresh mid-flow", async () => {
      const up = await uploadViaUI(page, files, { refreshMidFlow: true });
      expect(up.status).toBe(202);
      s.note("refresh mid-flow", `job reopened from the session job list after reload: ${up.resumed}`);
      expect(up.resumed, "job reachable after the reload").toBe(true);
      await shellReady(page);
      const done = await waitJob(request, up.jobId);
      expect(done.job.status).toBe("done");
      asg = done.assignment;
      s.note("assignment", `${asg?.incident_id} ${asg?.decision}: ${asg?.reason_pl}`);
    });

    await s.step("result: new campaign or justified abstain (reason states the evidence vs threshold)", async () => {
      expect(["new_campaign", "abstain"]).toContain(asg!.decision);
      expect(asg!.campaign_id ?? null).toBeNull();
      expect(asg!.reason_pl).toMatch(/próg|Wstrzymano|Nowy wzorzec|Niejednoznaczne/);
      await page.goto("/analyse");
      await shellReady(page);
      await page.screenshot({ path: shot(SC, "analyse-result") });
    });

    let decided: QueueItem | undefined;
    let runBefore = "";
    await s.step("queue: analyst rejects a cross-campaign abstain pair in the GUI", async () => {
      const q = await apiJson<{ run_id: string; items: QueueItem[] }>(request, "/v1/review/queue");
      runBefore = q.run_id;
      decided = q.items.find((i) => i.a && i.b);
      expect(decided, "a pair item in the queue").toBeTruthy();
      await page.goto("/queue");
      await shellReady(page);
      const list = page.getByTestId("queue-list");
      await list.getByRole("button").filter({ hasText: decided!.a! }).filter({ hasText: decided!.b! }).first().click();
      await page.screenshot({ path: shot(SC, "queue-before-decision") });
      const posted = page.waitForResponse((r) => r.url().endsWith("/v1/reviews") && r.request().method() === "POST");
      await page.getByRole("button", { name: "✕ Odrzuć (R)" }).click();
      const res = await posted;
      expect(res.status()).toBe(201);
      const body = (await res.json()) as { review: { analyst: string; action: string }; effect: string; applied?: unknown };
      s.note("queue decision", `${decided!.key} reject_link by ${body.review.analyst}, effect ${body.effect}`);
      expect(body.review.analyst, "real author recorded").toBe(analyst.username);
      expect(body.effect).toBe("applied");
      await page.screenshot({ path: shot(SC, "queue-after-decision") });
    });

    await s.step("re-computation: a new revision run is the default and the decided pair left the queue", async () => {
      const q = await apiJson<{ run_id: string; items: QueueItem[] }>(request, "/v1/review/queue");
      s.note("runs", `before ${runBefore} after ${q.run_id}`);
      expect(q.run_id).not.toBe(runBefore);
      expect(q.items.some((i) => i.key === decided!.key)).toBe(false);
    });

    await s.step("history: reviews list and the pair page show the analyst's decision", async () => {
      const r = await apiJson<{ items: { action: string; analyst: string; target: { a?: string; b?: string } }[] }>(request, "/v1/reviews");
      expect(r.items.some((x) => x.action === "reject_link" && x.analyst === analyst.username)).toBe(true);
      await page.goto(`/pair/${decided!.a}/${decided!.b}`);
      await shellReady(page);
      await expect(page.getByRole("main").getByText(new RegExp(`${analyst.username}: `)).first()).toBeVisible({ timeout: 30_000 });
      await page.screenshot({ path: shot(SC, "pair-history"), fullPage: true });
      const audit = await request.get("/v1/audit/verify");
      s.note("audit chain", `${audit.status()} ${(await audit.text()).slice(0, 200)}`);
    });

    await exportAndVerify(page, request, s, SC);
  } finally {
    s.note("failed requests (>=400) seen by the analyst page", failed.join(" ; ") || "none");
    await analyst.close();
  }
  s.finish();
});
