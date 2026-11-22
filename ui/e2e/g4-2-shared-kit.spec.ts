/**
 * G4 scenario 2, shared kit / different campaign: an unrelated lure ("Lotto Premium", own host, phone, scheme) built
 * with the same page kit as campaigns A/B (same HTML skeleton and lead form). Expected: kept as its own campaign,
 * the shared tool/kit relation is shown, no common operator is suggested. Also roles, bad ZIP, refresh mid-flow,
 * signed export checks.
 */
import { readdirSync } from "node:fs";
import path from "node:path";
import { apiJson, expect, shellReady, test } from "./fixtures";
import { type Assignment, Steps, csrfUpload, exportAndVerify, g4Info, rolesCheck, shot, trackRequests, uploadViaUI, waitJob } from "./g4-helpers";

const SC = "g4-2-shared-kit";

interface PairScore { score: { hypothesis: string; operator_bits: number; kit_bits: number; campaign_bits: number }; same_campaign_in_run: boolean }

test("G4-2 shared kit: separate campaign, kit relation shown, no common operator", async ({ browser, baseURL, request }) => {
  test.setTimeout(600_000);
  const s = new Steps(SC);
  const info = g4Info();
  const failed: string[] = [];
  const files = readdirSync(info.g4.kit).sort().map((f) => path.join(info.g4.kit, f));
  const analyst = await rolesCheck(browser, baseURL!, request, s, SC, files[0]!);
  const page = analyst.page;
  trackRequests(page, failed);
  let asg: Assignment | null = null;
  let jobId = "";
  try {
    await s.step("invalid input: truncated ZIP is accepted without a crash and ends in a justified abstain", async () => {
      const r = await csrfUpload(page, info.g4.bad_zip);
      s.note("bad zip upload", `HTTP ${r.status()} ${(await r.text()).slice(0, 200)}`);
      expect([202, 400, 415, 422]).toContain(r.status());
      if (r.status() === 202) {
        const done = await waitJob(request, ((await r.json()) as { job_id: string }).job_id);
        s.note("bad zip result", `${done.job.status} ${done.assignment?.decision}: ${done.assignment?.reason_pl ?? done.job.error}`);
        expect(done.job.status === "failed" || done.assignment?.decision === "abstain").toBe(true);
      }
    });

    await s.step("analyst imports the kit-sharing page through the GUI; refresh mid-flow", async () => {
      const up = await uploadViaUI(page, files, { refreshMidFlow: true });
      expect(up.status).toBe(202);
      s.note("refresh mid-flow", `job reopened from the session job list after reload: ${up.resumed}`);
      expect(up.resumed, "job reachable after the reload").toBe(true);
      jobId = up.jobId;
      await shellReady(page);
      const done = await waitJob(request, jobId);
      expect(done.job.status).toBe("done");
      asg = done.assignment;
      s.note("assignment", `${asg?.incident_id} ${asg?.decision} ${asg?.campaign_id}: ${asg?.reason_pl}`);
    });

    await s.step("kept separate: not assigned to any existing campaign", async () => {
      expect(asg?.decision).not.toBe("assign");
      expect(asg?.campaign_id ?? null).toBeNull();
      const camps = await apiJson<{ items: { id: string; members: string[] }[] }>(request, "/v1/campaigns");
      const holder = camps.items.find((c) => c.members.includes(asg!.incident_id) && c.members.length > 1);
      expect(holder, "no multi-member campaign holds the new incident").toBeFalsy();
    });

    await s.step("shared tool/kit relation is stated in the reason and on the pair page", async () => {
      expect(asg!.reason_pl).toMatch(/Wspólny zestaw narzędzi \(kit\)/);
      const best = asg!.candidates[0]!.best_pair;
      await page.goto(`/pair/${best.a}/${best.b}`);
      await shellReady(page);
      const levels = page.locator("section, div").filter({ has: page.getByText("Poziomy dowodu", { exact: true }) }).last();
      await expect(levels).toContainText("Szablon / kit");
      await expect(page.getByRole("main").getByText(/wspóln(y|ego) (szablon|kit|zestaw)/i).first()).toBeVisible();
      await page.screenshot({ path: shot(SC, "pair-kit-levels"), fullPage: true });
    });

    await s.step("no common operator suggested: best pair has 0 operator bits and hypothesis is not same_campaign", async () => {
      const best = asg!.candidates[0]!.best_pair;
      const p = await apiJson<PairScore>(request, `/v1/pairs/${best.a}/${best.b}`);
      s.note("best pair", `${best.a}/${best.b} hypothesis=${p.score.hypothesis} operator_bits=${p.score.operator_bits} kit_bits=${p.score.kit_bits} campaign_bits=${p.score.campaign_bits}`);
      expect(p.score.operator_bits).toBe(0);
      expect(p.score.hypothesis).not.toBe("same_campaign");
      expect(p.score.kit_bits).toBeGreaterThan(0);
      expect(p.same_campaign_in_run).toBe(false);
    });

    await s.step("analyse page result card shows the decision and the kit sentence", async () => {
      await page.goto("/analyse");
      await shellReady(page);
      await page.screenshot({ path: shot(SC, "analyse-page") });
    });

    await exportAndVerify(page, request, s, SC);
  } finally {
    s.note("failed requests (>=400) seen by the analyst page", failed.join(" ; ") || "none");
    await analyst.close();
  }
  s.finish();
});
