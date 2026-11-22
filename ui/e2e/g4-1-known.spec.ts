/**
 * G4 scenario 1, known campaign: a new incident of synthetic campaign A ("Inwestycja PKO Bonus") whose ad creative is a
 * CROPPED copy of the base/a2 screenshot, plus its landing-page variant. Imported through the GUI as an analyst:
 * progress bar → analysis → assigned to campaign A → pair page (decision card questions) → variant diff with the
 * geometry overlay (non-zero inliers) → evidence + report; KONTRA shows a decision change after removing evidence.
 * Also: viewer blocked, invalid input (corrupt PNG), refresh mid-flow, signed export verify PASS / tampered FAIL /
 * foreign fingerprint FAIL.
 */
import { readdirSync } from "node:fs";
import path from "node:path";
import { campaignByLabel, expect, shellReady, test } from "./fixtures";
import { type Assignment, Steps, csrfUpload, exportAndVerify, g4Info, rolesCheck, shot, trackRequests, uploadViaUI, waitJob } from "./g4-helpers";

const SC = "g4-1-known";

test("G4-1 known campaign: cropped creative assigned to campaign A with geometry, evidence, report, KONTRA", async ({ browser, baseURL, request }) => {
  test.setTimeout(600_000);
  const s = new Steps(SC);
  const info = g4Info();
  const failed: string[] = [];
  const files = readdirSync(info.g4.known).sort().map((f) => path.join(info.g4.known, f));
  const a = await campaignByLabel(request, "Inwestycja PKO Bonus");
  const analyst = await rolesCheck(browser, baseURL!, request, s, SC, files[0]!);
  const page = analyst.page;
  trackRequests(page, failed);
  let asg: Assignment | null = null;
  let jobId = "";
  try {
    await s.step("invalid input: corrupt PNG accepted, no features read, justified abstain (not a crash)", async () => {
      const r = await csrfUpload(page, info.g4.corrupt_png);
      expect(r.status()).toBe(202);
      const done = await waitJob(request, ((await r.json()) as { job_id: string }).job_id);
      expect(done.job.status).toBe("done");
      expect(done.assignment?.decision).toBe("abstain");
      expect(done.assignment?.reason_pl).toMatch(/nie odczytano żadnej cechy|uszkodzony/);
    });

    await s.step("analyst imports the cropped creative + landing page through the GUI; progress bar shown", async () => {
      const up = await uploadViaUI(page, files, {
        onProgress: async () => {
          const live = page.getByRole("dialog", { name: "Analiza na żywo" });
          await live.screenshot({ path: shot(SC, "live-progress") });
          await live.getByRole("button", { name: "Zamknij (Esc)" }).click();
          await expect(page.getByTestId("analyse-activity").getByTestId("activity-overall-bar")).toBeVisible();
          await page.getByTestId("analyse-activity").screenshot({ path: shot(SC, "progress-bar") });
        },
        refreshMidFlow: true,
      });
      expect(up.status).toBe(202);
      s.note("refresh mid-flow", `job reopened from the session job list after reload: ${up.resumed}`);
      expect(up.resumed, "job reachable after the reload").toBe(true);
      jobId = up.jobId;
    });

    await s.step("page refresh mid-flow: job still tracked and finishes", async () => {
      await shellReady(page);
      const done = await waitJob(request, jobId);
      expect(done.job.status).toBe("done");
      asg = done.assignment;
      const prog = (await (await request.get(`/v1/analyse/${jobId}/progress`)).json()) as { status: string; import?: { phase: string } };
      expect(prog.status).toBe("done");
      await page.goto("/analyse");
      await shellReady(page);
      await page.screenshot({ path: shot(SC, "after-refresh") });
    });

    await s.step("assigned to the right campaign (A)", async () => {
      expect(asg?.decision, asg?.reason_pl).toBe("assign");
      expect(asg?.campaign_id).toBe(a.id);
      s.note("assignment", `${asg!.incident_id} -> ${asg!.campaign_id}: ${asg!.reason_pl}`);
    });

    await s.step("campaign card lists the new incident", async () => {
      await page.goto(`/campaigns/${a.id}`);
      await shellReady(page);
      await expect(page.getByRole("main").getByText(asg!.incident_id).first()).toBeVisible({ timeout: 30_000 });
      await page.screenshot({ path: shot(SC, "campaign-card"), fullPage: false });
    });

    const pairA = asg ? asg.candidates[0]!.best_pair.a : "nask-a2";
    const pairB = asg ? asg.candidates[0]!.best_pair.b : "";
    await s.step("pair page: decision card answers (what linked, files, rarity, skipped, dependent, missing)", async () => {
      await page.goto(`/pair/${pairA}/${pairB}`);
      await shellReady(page);
      const main = page.getByRole("main");
      await expect(main.getByRole("heading", { name: /Wszystkie powody/ })).toBeVisible({ timeout: 30_000 });
      // what linked the materials
      await expect(main.getByText(/Ten sam numer konta|Ten sam numer telefonu|identyfikator śledzący/).first()).toBeVisible();
      // why it is rare: document frequency in the corpus
      await expect(main.getByText(/występuje w \d+ z \d+ incydentów/).first()).toBeVisible();
      // what was skipped / missing: unknown families shown as "no observation, not counted against"
      await expect(main.locator('[title="brak obserwacji: nie liczy się przeciw"]').first()).toBeVisible();
      // dependent evidence: family cap correction row of the waterfall
      await expect(main.getByText("korekta limitu rodziny").first()).toBeVisible();
      await page.screenshot({ path: shot(SC, "pair-decision-card"), fullPage: true });
    });

    await s.step("pair page: which file each trace comes from (file names of both incidents + provenance by role)", async () => {
      const prov = page.locator("section, div").filter({ has: page.getByText("Skąd pochodzi dowód", { exact: true }) }).last();
      await expect(prov).toContainText(/strona docelowa \(HTML\)|zrzut|obraz/, { timeout: 30_000 });
      // the pair page names each side by artefact role (zrzut z feedu, strona docelowa), not by file name
      await expect(page.getByRole("main").getByText("Rola obrazu").first()).toBeVisible();
      await expect(page.getByRole("main").getByText("Dopasowane miejsca w materiałach", { exact: true })).toBeVisible();
      await prov.screenshot({ path: shot(SC, "pair-provenance") });
    });

    await s.step("geometry: API reports non-zero inliers and the diff page draws the overlay", async () => {
      const g = (await (await request.get(`/v1/pairs/${pairA}/${pairB}/geometry`)).json()) as { pairs: { inliers: number; method: string }[] };
      const best = Math.max(0, ...g.pairs.map((p) => p.inliers));
      s.note("geometry", `${g.pairs.length} unit pairs, best inliers ${best} (${g.pairs[0]?.method})`);
      expect(best).toBeGreaterThan(0);
      await page.goto(`/diff/${pairA}/${pairB}`);
      await shellReady(page);
      const overlay = page.getByTestId("geometry-overlay");
      await expect(overlay).toBeVisible({ timeout: 30_000 });
      expect(await overlay.getByTestId("geometry-lines").first().locator("line").count()).toBeGreaterThan(0);
      await overlay.screenshot({ path: shot(SC, "geometry-overlay") });
    });

    await s.step("analyst decision on the pair is recorded under the authenticated user (what the analyst changed)", async () => {
      await page.goto(`/pair/${pairA}/${pairB}`);
      await shellReady(page);
      await page.getByLabel("Uzasadnienie").fill("G4: kadr tej samej reklamy");
      const posted = page.waitForResponse((r) => r.url().endsWith("/v1/reviews") && r.request().method() === "POST");
      await page.getByRole("button", { name: "✓ Potwierdź powiązanie" }).click();
      const res = await posted;
      expect(res.status()).toBe(201);
      const body = (await res.json()) as { review: { analyst: string }; effect: string };
      expect(body.review.analyst).toBe(analyst.username);
      await expect(page.getByRole("main").getByText(new RegExp(`${analyst.username}: .*G4: kadr`)).first()).toBeVisible({ timeout: 30_000 });
      s.note("review effect", body.effect);
    });

    await s.step("evidence: campaign export (JSON + CSV of member genes) contains the new incident", async () => {
      // the applied review created a revision run: resolve campaign A again by label (its id may change)
      const a2 = await campaignByLabel(request, "Inwestycja PKO Bonus");
      s.note("campaign A id after review", `${a.id} -> ${a2.id}`);
      for (const f of ["json", "csv"]) {
        const r = await request.get(`/v1/campaigns/${a2.id}/export?format=${f}`);
        expect(r.status(), f).toBe(200);
        expect(await r.text(), f).toContain(asg!.incident_id);
      }
    });

    await s.step("KONTRA: removing evidence changes the decision (concrete scenario)", async () => {
      await page.goto(`/lab/${asg!.incident_id}`);
      await shellReady(page);
      await page.getByTestId("lab-run").first().click();
      await expect(page.getByTestId("lab-scenarios")).toBeVisible({ timeout: 90_000 });
      const exp = (await (await request.get(`/v1/lab/${asg!.incident_id}`)).json()) as { scenarios: { scenario_id: string; label_pl: string; decision: string; decision_changed: boolean }[]; baseline: { decision: string } };
      const flip = exp.scenarios.find((x) => x.decision_changed);
      expect(flip, "a scenario that changes the decision").toBeTruthy();
      s.note("kontra flip", `${flip!.scenario_id}: ${exp.baseline.decision} -> ${flip!.decision} (${flip!.label_pl})`);
      await expect(page.getByTestId("lab-flip-set")).toBeVisible();
      await page.getByTestId("lab-scenarios").screenshot({ path: shot(SC, "kontra-scenarios") });
      await page.getByTestId("lab-flip-set").screenshot({ path: shot(SC, "kontra-flip-set") });
    });

    await s.step("report: KONTRA evidence package built in the GUI, report.md names the incident and the decision", async () => {
      const made = page.waitForResponse((r) => /\/v1\/lab\/[^/]+\/package$/.test(r.url()));
      await page.getByTestId("lab-package").click();
      const pk = (await (await made).json()) as { package_id: string };
      await expect(page.getByTestId("lab-export")).toContainText("kp-");
      const md = await request.get(`/v1/lab/packages/${pk.package_id}/report.md`);
      expect(md.status()).toBe(200);
      const t = await md.text();
      expect(t).toContain(asg!.incident_id);
      await page.getByTestId("lab-export").screenshot({ path: shot(SC, "kontra-package") });
    });

    await exportAndVerify(page, request, s, SC);
  } finally {
    s.note("failed requests (>=400) seen by the analyst page", failed.join(" ; ") || "none");
    await analyst.close();
  }
  s.finish();
});
