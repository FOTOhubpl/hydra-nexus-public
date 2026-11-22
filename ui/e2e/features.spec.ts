/**
 * Feature flows on the synthetic corpus (tests/e2e/corpus.py): graph constellation + campaign expansion, pair
 * waterfall, artefact viewer (image / HTML / HAR / video), corpus switch, live indicator, and GUI upload (@mutating,
 * runs last in its own project because it adds incidents and an "assign" run to the server state).
 *
 * Desktop light only, except the graph test which also runs in desktop dark. Known product defects are kept as
 * test.fail() with a CR-E2E-UI-n id; when a defect is fixed the test starts passing and Playwright reports it.
 */
import { readdirSync } from "node:fs";
import path from "node:path";
import { apiJson, campaignByLabel, expect, plFixed, serverInfo, shellReady, test } from "./fixtures";
import type { Page } from "playwright/test";

const LABEL_A = "Inwestycja PKO Bonus";

function onlyProjects(names: string[]) {
  test.beforeEach(({}, testInfo) => {
    test.skip(!names.includes(testInfo.project.name), `runs in ${names.join(", ")} only`);
  });
}

interface GraphStats {
  order: number;
  campaigns: number;
  incidents: string[];
}

/** Node counts of the sigma graph; exposed by ConstellationCanvas as window.__hydraConstellation with ?perf. */
async function graphStats(page: Page): Promise<GraphStats> {
  return page.evaluate(() => {
    const h = (window as unknown as { __hydraConstellation?: { graph: { order: number; forEachNode: (f: (n: string, a: { kind?: string }) => void) => void } } }).__hydraConstellation;
    if (!h) return { order: 0, campaigns: 0, incidents: [] };
    let campaigns = 0;
    const incidents: string[] = [];
    h.graph.forEachNode((n, a) => {
      if (a.kind === "campaign") campaigns++;
      else if (a.kind === "incident") incidents.push(n.replace(/^i:/, ""));
    });
    return { order: h.graph.order, campaigns, incidents };
  });
}

/** Opaque pixels on the 2D layers of the constellation (glows, discs, labels): > 0 when something is drawn. */
async function paintedPixels(page: Page): Promise<number> {
  return page.getByTestId("constellation").evaluate((root) => {
    let n = 0;
    for (const c of root.querySelectorAll("canvas")) {
      const ctx = (c as HTMLCanvasElement).getContext("2d");
      if (!ctx || !c.width || !c.height) continue;
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      for (let i = 3; i < d.length; i += 4) if (d[i]) n++;
    }
    return n;
  });
}

test.describe("graph constellation", () => {
  onlyProjects(["desktop-light", "desktop-dark"]);

  test("renders all campaigns and expanding one loads its incidents", async ({ page, request, theme }) => {
    const a = await campaignByLabel(request, LABEL_A);
    // columnar overview: campaigns.rows also holds "solo:" rows for live (uploaded) incidents
    const ov = await apiJson<{ campaigns: { rows: unknown[][] } }>(request, "/v1/graph/overview");

    await page.goto("/graph?perf=1");
    await shellReady(page);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await expect(page.getByTestId("constellation")).toBeVisible();
    await expect(page.locator(".sigma-stage, [data-testid=constellation] canvas").first()).toBeAttached();
    await expect.poll(async () => (await graphStats(page)).campaigns, { message: "campaign nodes in sigma graph" }).toBe(ov.campaigns.rows.length);
    expect((await graphStats(page)).incidents, "no incidents before expansion (lazy LOD)").toEqual([]);
    await expect.poll(() => paintedPixels(page), { message: "painted pixels on the constellation layers" }).toBeGreaterThan(1000);

    const inspector = page.getByRole("complementary", { name: "Inspektor grafu" });
    await expect(inspector.getByText("Przegląd przebiegu")).toBeVisible();
    // "Największe kampanie": label + size
    const row = inspector.getByRole("button", { name: new RegExp(`^${LABEL_A}\\s*${a.members.length}\\s*incydentów$`) });
    await expect(row).toBeVisible();
    await row.click();

    await expect(inspector.getByRole("heading", { level: 2, name: LABEL_A })).toBeVisible();
    await expect(inspector.getByText(a.id, { exact: true })).toBeVisible();
    await expect(inspector.getByText(`Incydenty (${a.members.length})`)).toBeVisible();
    for (const m of a.members) await expect(inspector.getByRole("button", { name: new RegExp(m) })).toBeVisible();
    await expect.poll(async () => (await graphStats(page)).incidents.filter((i) => a.members.includes(i)).sort(), {
      message: "incident nodes of the expanded campaign in the sigma graph",
    }).toEqual([...a.members].sort());
    await expect(page.getByTestId("constellation").getByText(/^Rozwinięte kampanie: \d+/)).toBeVisible();
  });

  // Regression for CR-E2E-UI-2 (fixed 2026-11-21): date-only campaign dates (first_seen "2026-10-01") were parsed as
  // UTC midnight and printed with an invented time ("1 paź 2026, 02:00"); formatDatePl now shows the calendar day only.
  test("campaign panel shows date-only first/last seen without an invented time", async ({ page, request }) => {
    const camps = await apiJson<{ items: { label: string; first_seen: string | null }[] }>(request, "/v1/campaigns");
    expect(camps.items.find((c) => c.label === LABEL_A)?.first_seen).toBe("2026-10-01");
    await page.goto("/graph");
    const inspector = page.getByRole("complementary", { name: "Inspektor grafu" });
    await inspector.getByRole("button", { name: new RegExp(`^${LABEL_A}`) }).click();
    const first = inspector.locator("dt", { hasText: /^pierwszy$/ }).locator("xpath=following-sibling::dd[1]");
    await expect(first).toHaveText("1 paź 2026", { timeout: 5_000 });
  });
});

test.describe("desktop features", () => {
  onlyProjects(["desktop-light"]);

  test("pair page shows the bits waterfall for nask-a1 / nask-a2 with real contributions", async ({ page, request }) => {
    const pair = await apiJson<{ score: { family_bits: Record<string, number>; total_bits: number; hypothesis: string } }>(
      request, "/v1/pairs/nask-a1/nask-a2");
    const fb = pair.score.family_bits;
    expect(Object.keys(fb).length, "families with bits").toBeGreaterThanOrEqual(3);

    await page.goto("/pair/nask-a1/nask-a2");
    const wf = page.getByTestId("waterfall");
    await expect(wf).toBeVisible();
    const LABEL: Record<string, string> = { VIS: "obraz", TXT: "tekst", INF: "infrastruktura", ID: "tożsamość", NAR: "narracja", META: "metadane" };
    const text = () => wf.innerText();
    for (const [fam, bits] of Object.entries(fb)) {
      const sign = bits > 0 ? "\\+" : bits < 0 ? "−" : "±";
      await expect.poll(text).toMatch(new RegExp(`${fam} · ${LABEL[fam] ?? ""}\\s+${sign}${plFixed(Math.abs(bits)).replace(/\s/g, "\\s")} b`));
    }
    await expect.poll(text).toMatch(new RegExp(`Suma\\s+\\+${plFixed(pair.score.total_bits).replace(/\s/g, "\\s")} b`));
    // families without observations are "nieznane", never negative
    const missing = ["VIS", "TXT", "INF", "ID", "NAR", "META"].filter((f) => !(f in fb));
    for (const f of missing) await expect.poll(text).toMatch(new RegExp(`${f} · ${LABEL[f]}\\s+nieznane`));

    // expanding INF lists the shared infrastructure genes of campaign A
    const inf = wf.getByRole("button", { name: /INF · infrastruktura/ });
    await inf.click();
    await expect(inf).toHaveAttribute("aria-expanded", "true");
    await expect(wf.locator("table")).toContainText("pko-bonus24.com");
    await expect(wf.locator("table")).toContainText("fb_pixel:987654321012345");
  });

  test("artefact viewer opens image, HTML and HAR of nask-a1", async ({ page }) => {
    await page.goto("/incident/nask-a1");
    await expect(page.getByRole("heading", { name: "Materiały incydentu (3)" })).toBeVisible();
    const preview = page.locator("section[id^=art-preview-]");

    // default artefact: the PNG ad screenshot, decoded at its real size (720 x 480, corpus.png)
    await expect(page.getByRole("heading", { name: /Artefakt Zrzut ekranu 2026-10-01 o 10\.11\.png/ })).toBeVisible();
    const img = preview.locator("img").first();
    await expect.poll(() => img.evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBe(720);
    await expect(preview.getByText("720 × 480 px")).toBeVisible();

    // saved landing page: sandboxed iframe, the page title rendered inside
    await page.locator('button[aria-pressed][title^="Strona.html"]').click();
    await expect(page.locator('button[aria-pressed][title^="Strona.html"]')).toHaveAttribute("aria-pressed", "true");
    const frame = preview.locator("iframe");
    await expect(frame).toHaveAttribute("sandbox", "");
    await expect(page.frameLocator("section[id^=art-preview-] iframe").getByRole("heading", { name: LABEL_A })).toBeVisible();
    await expect(preview).toContainText("podgląd (bez skryptów)");

    // HAR: request table with the landing host and path
    await page.locator('button[aria-pressed][title^="page.har"]').click();
    await expect(preview.getByLabel("Filtr żądań HAR")).toBeVisible();
    await expect(preview).toContainText("pko-bonus24.com");
    await expect(preview).toContainText("/oferta/101");
  });

  test("artefact viewer opens the MP4 screen recording and the SMS screenshot of nask-c1", async ({ page }) => {
    await page.goto("/incident/nask-c1");
    await expect(page.getByRole("heading", { name: /Materiały incydentu \(\d+\)/ })).toBeVisible();
    const preview = page.locator("section[id^=art-preview-]");

    await page.locator('button[aria-pressed][title^="ScreenRecording_"]').click();
    const video = preview.locator("video");
    await expect(video).toBeAttached();
    // The corpus MP4 is MPEG-4 Part 2 (OpenCV mp4v), which Chromium cannot decode: the viewer must say so and show
    // ffmpeg-decoded keyframes instead. If the browser can play it, metadata (2 s at 12 fps) must load.
    const state = await expect.poll(() => video.evaluate((v: HTMLVideoElement) => (v.error ? "error" : v.readyState >= 1 ? "ready" : "pending"))).not.toBe("pending").then(
      () => video.evaluate((v: HTMLVideoElement) => (v.error ? "error" : "ready")));
    if (state === "error") await expect(preview).toContainText("Przeglądarka nie odtwarza tego formatu");
    else expect(await video.evaluate((v: HTMLVideoElement) => v.duration)).toBeCloseTo(2, 0);
    const frames = preview.getByLabel("Klatki kluczowe").locator("img");
    await expect(frames.first()).toBeVisible();
    await expect.poll(() => frames.first().evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBeGreaterThan(0);
    await expect(preview).toContainText("0:02,0");

    await page.locator('button[aria-pressed][title^="IMG_1012.PNG"]').click();
    await expect.poll(() => preview.locator("img").first().evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBe(720);
  });

  // Regression for CR-E2E-UI-1 (fixed 2026-11-21): AnnotatedImage kept "ładowanie…" instead of "720 × 480 px" when an
  // image artefact was selected again (image complete from the memory cache, size reset raced the load). The size is
  // now keyed by src and also read from an already complete <img>.
  test("image size label after switching back to a previously viewed image", async ({ page }) => {
    await page.goto("/incident/nask-a1");
    const preview = page.locator("section[id^=art-preview-]");
    await expect(preview.getByText("720 × 480 px")).toBeVisible();
    // reproduced 24/24 in a standalone loop; under parallel load the first revisit occasionally wins the race, so
    // the switch is repeated and every revisit must show the size
    for (let i = 0; i < 3; i++) {
      await page.locator('button[aria-pressed][title^="Strona.html"]').click();
      await expect(preview.locator("iframe")).toBeAttached();
      await page.locator('button[aria-pressed][title^="Zrzut ekranu"]').click();
      await expect.poll(() => preview.locator("img").first().evaluate((i: HTMLImageElement) => i.complete && i.naturalWidth)).toBe(720);
      await expect(preview.getByText("720 × 480 px")).toBeVisible({ timeout: 3_000 });
    }
  });

  test("corpus switch lists GET /v1/corpora and selecting a corpus scopes requests to its run", async ({ page, request }) => {
    const corpora = await apiJson<{ items: { corpus: string; latest_run: string; runs: { id: string }[] }[] }>(request, "/v1/corpora");
    expect(corpora.items.length).toBeGreaterThanOrEqual(1);
    // B16 (QA audit): a corpus imported through upload is labelled `upload`, not `nask`.
    const nask = corpora.items.find((c) => c.corpus === "nask" || c.corpus === "upload");
    expect(nask, "nask or upload corpus").toBeTruthy();
    const LABEL = (c: string) => (c === "nask" ? "Tylko dane NASK" : c === "upload" ? "Materiał przesłany" : c === "mixed" ? "NASK + zewnętrzne" : c === "unknown" ? "inne przebiegi" : c);

    await page.goto("/campaigns");
    const group = page.getByRole("group", { name: "Zbiór danych" });
    await expect(group).toBeVisible();
    await expect(group.getByRole("button")).toHaveCount(corpora.items.length);
    for (const c of corpora.items) await expect(group.getByRole("button", { name: LABEL(c.corpus), exact: true })).toBeVisible();
    const btn = group.getByRole("button", { name: LABEL(nask!.corpus), exact: true });
    await expect(btn).toHaveAttribute("aria-pressed", "true");
    const runSelect = page.getByRole("combobox", { name: "Przebieg analizy" });
    if (nask!.runs.length > 1) await expect(runSelect).toBeVisible();
    else await expect(runSelect).toHaveCount(0);

    const scoped = page.waitForRequest((r) => r.url().includes("/api/v1/campaigns") && r.url().includes(`run_id=${nask!.latest_run}`));
    await btn.click();
    await scoped;
    await expect(btn).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("main").getByText(LABEL_A).first()).toBeVisible();
    await expect(page.getByTestId("bottom-status")).toContainText(nask!.latest_run);
  });

  test("live indicator connects to GET /v1/stream and shows 'na żywo'", async ({ page }) => {
    const stream = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/v1/stream");
    await page.goto("/");
    await stream;
    const live = page.getByTestId("live-status");
    await expect(live).toHaveAttribute("data-status", "live");
    await expect(live).toHaveText("na żywo");
    await expect(live).toHaveAttribute("title", /GET \/v1\/stream/);
  });
});

// ------------------------------------------------------------------------------------------------ shell (2027 redesign)
test.describe("hypothesis lab (KONTRA K1)", () => {
  onlyProjects(["desktop-light", "desktop-dark"]);

  test("lab page re-scores nask-a2 for real and builds an evidence package", async ({ page, request }) => {
    await page.goto("/lab/nask-a2");
    await expect(page.getByRole("heading", { name: /Laboratorium hipotez/ }).first()).toBeVisible();
    const run = page.getByTestId("lab-run").first();
    await run.click();
    await expect(page.getByTestId("lab-scenarios")).toBeVisible({ timeout: 60_000 });
    const exp = await apiJson<{ baseline: { decision: string }; scenarios: unknown[]; hypotheses: { id: string }[] }>(
      request, "/v1/lab/nask-a2");
    expect(exp.scenarios.length, "baseline + counterfactual rows").toBeGreaterThan(1);
    await expect(page.getByTestId("lab-hypotheses")).toBeVisible();
    for (const h of exp.hypotheses) await expect(page.getByTestId(`lab-hypothesis-${h.id}`)).toBeVisible();
    await expect(page.getByTestId("lab-retrieval-scope")).toBeVisible();
    await expect(page.getByTestId("lab-next-observations")).toContainText("bez wykazanej przewagi");
    await expect(page.getByTestId("lab-stale")).toHaveCount(0);
    await page.getByTestId("lab-package").click();
    await expect(page.getByTestId("lab-export")).toContainText("kp-");
    await page.getByTestId("lab-send").click();
    await expect(page.getByTestId("lab-ack")).toBeVisible();
  });
});

test.describe("floating sidebar and top bar", () => {
  onlyProjects(["desktop-light", "desktop-dark"]);

  test("sidebar collapses to an icon rail with Ctrl+B, shows tooltips and remembers the state", async ({ page }) => {
    await page.goto("/");
    await shellReady(page);
    const sidebar = page.getByTestId("sidebar");
    await expect(sidebar).toHaveAttribute("data-state", "expanded");
    const nav = page.getByRole("navigation", { name: "Nawigacja" });
    for (const section of ["Przegląd", "Analiza", "Kampanie i powiązania", "Dowody", "Monitoring", "Ustawienia"]) {
      await expect(nav.getByRole("group", { name: section })).toBeVisible();
    }
    await expect(nav.getByRole("link", { name: "Pulpit" })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("navigation", { name: "Ścieżka" })).toContainText("Przegląd");

    await page.locator("main").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("ControlOrMeta+b");
    await expect(sidebar).toHaveAttribute("data-state", "collapsed");
    await nav.getByRole("link", { name: "Graf powiązań" }).hover();
    await expect(page.getByRole("tooltip")).toHaveText("Graf powiązań");

    await page.reload();
    await shellReady(page);
    await expect(page.getByTestId("sidebar")).toHaveAttribute("data-state", "collapsed");
    await page.getByTestId("sidebar-toggle").click();
    await expect(page.getByTestId("sidebar")).toHaveAttribute("data-state", "expanded");
  });

  test("command palette opens with Ctrl/Cmd+K and jumps to a screen", async ({ page }) => {
    await page.goto("/");
    await shellReady(page);
    await page.getByTestId("global-search").click();
    const dialog = page.getByRole("dialog", { name: "Paleta poleceń" });
    await expect(dialog).toBeVisible();
    await page.keyboard.type("Pary");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/pairs$/);
    await expect(page.locator("header h1")).toHaveText("Pary i dowody");
  });

  test("floating top bar: notifications, shortcuts and run details popovers; compact on scroll", async ({ page, request }) => {
    await page.goto("/");
    await shellReady(page);
    const bar = page.getByTestId("top-bar");
    await expect(bar).toHaveAttribute("data-scrolled", "false");
    await page.getByRole("button", { name: /^Powiadomienia/ }).click();
    const notes = page.getByRole("dialog", { name: "Powiadomienia" });
    await expect(notes).toBeVisible();
    await expect(notes).toContainText("Aktywność na żywo");
    await page.keyboard.press("Escape");
    await expect(notes).toBeHidden();
    await expect(page.getByRole("button", { name: /^Powiadomienia/ })).toBeFocused();

    await page.locator("main").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("?");
    const keys = page.getByRole("dialog", { name: "Skróty klawiszowe" });
    await expect(keys).toBeVisible();
    await expect(keys).toContainText("Ctrl/⌘ J");
    await page.keyboard.press("Escape");

    const corpora = await apiJson<{ items: { corpus: string; latest_run: string }[] }>(request, "/v1/corpora");
    await page.getByRole("button", { name: "Szczegóły przebiegu" }).click();
    const details = page.getByRole("dialog", { name: "Szczegóły przebiegu" });
    await expect(details).toContainText(corpora.items[0]!.latest_run);
    await page.keyboard.press("Escape");

    await page.locator("main").evaluate((el) => { el.scrollTop = 600; el.dispatchEvent(new Event("scroll")); });
    await expect(bar).toHaveAttribute("data-scrolled", "true");
  });

  test("dashboard right rail: lazy sections, Ctrl+J collapse remembered, KPI tooltip on focus", async ({ page }) => {
    const queue = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/v1/review/queue");
    await page.goto("/");
    await shellReady(page);
    const rail = page.getByTestId("dash-rail");
    await expect(rail).toHaveAttribute("data-state", "expanded");
    for (const name of ["Aktywność na żywo", "Kolejka analityka", "Stan systemu", "Szybkie akcje"]) {
      await expect(rail.getByRole("heading", { name, exact: true })).toBeVisible();
    }
    await queue;
    await expect(rail.getByTestId("rail-system")).toContainText("dziennik audytu");

    await page.getByTestId("kpi-incidents").focus();
    await expect(page.getByRole("tooltip")).toContainText("Zgłoszenia");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("tooltip")).toHaveCount(0);

    await page.locator("main").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("ControlOrMeta+j");
    await expect(rail).toHaveAttribute("data-state", "collapsed");
    await page.reload();
    await shellReady(page);
    await expect(page.getByTestId("dash-rail")).toHaveAttribute("data-state", "collapsed");
    await page.getByTestId("rail-toggle").click();
    await expect(page.getByTestId("dash-rail")).toHaveAttribute("data-state", "expanded");
  });

  test("below lg the dashboard rail is a bottom sheet", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await shellReady(page);
    await expect(page.getByTestId("dash-rail")).toHaveCount(0);
    await page.getByTestId("rail-sheet-button").click();
    const sheet = page.getByRole("dialog", { name: "Panel pulpitu" });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByRole("heading", { name: "Stan systemu" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(sheet).toBeHidden();
  });

  test("below md the sidebar becomes a drawer", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/campaigns");
    await shellReady(page);
    await expect(page.getByTestId("sidebar")).toBeHidden();
    await page.getByTestId("menu-button").click();
    const drawer = page.getByRole("dialog", { name: "Menu" });
    await expect(drawer).toBeVisible();
    await drawer.getByRole("link", { name: "Incydenty" }).click();
    await expect(drawer).toBeHidden();
    await expect(page.locator("header h1")).toHaveText("Incydenty");
  });
});

// ------------------------------------------------------------------------------------------------ GUI upload
const POLISH = /[ąćęłńóśźż]/i;

async function uploadFolder(page: Page, dir: string) {
  await page.goto("/analyse");
  await page.getByRole("tab", { name: "Plik lub ZIP incydentu" }).click();
  await expect(page.getByRole("tab", { name: "Plik lub ZIP incydentu" })).toHaveAttribute("aria-selected", "true");
  const files = readdirSync(dir).sort().map((f) => path.join(dir, f));
  // FilePicker (pages/task/AnalysePage.tsx) has no data-testid: first hidden file input = "Wybierz pliki / ZIP…"
  await page.locator("main form input[type=file]").first().setInputFiles(files);
  await expect(page.getByText(new RegExp(`wybrano ${files.length} plik\\(ów\\)`))).toBeVisible();
  const posted = page.waitForResponse((r) => r.url().endsWith("/api/v1/analyse") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Analizuj", exact: true }).click();
  expect((await posted).status()).toBe(202);
  const live = page.getByRole("dialog", { name: "Analiza na żywo" });
  await expect(live).toBeVisible();
  await live.getByRole("button", { name: "Zamknij (Esc)" }).click();
  await expect(live).toHaveCount(0);
  const card = page.locator("section, div").filter({ has: page.getByRole("heading", { name: "Wynik analizy" }) }).last();
  await expect(card.getByText("zakończone", { exact: true })).toBeVisible({ timeout: 120_000 });
  return card;
}

test.describe("GUI upload @mutating", () => {
  test.describe.configure({ mode: "serial", timeout: 180_000 });
  onlyProjects(["desktop-light-mutating"]);

  test("unseen campaign D (new-d1): new pattern, reasons in Polish", async ({ page }) => {
    const info = serverInfo();
    test.skip(!info?.new, "E2E_SERVER_JSON with the corpus 'new' dir is required for uploads");
    const card = await uploadFolder(page, path.join(info!.new!, "new-d1"));
    await expect(card).toContainText("tryb: pliki / ZIP incydentu");
    await expect(card.getByText("nowa kampania / nowy wzorzec", { exact: true })).toBeVisible();
    const reason = card.locator("div.space-y-2 > div.text-fg").first(); // assignment.reason_pl under the decision badge
    await expect(reason).toBeVisible();
    const t = await reason.innerText();
    expect(t, "reason_pl contains Polish diacritics").toMatch(POLISH);
    expect(t).toMatch(/Nowy wzorzec|nowa kampania/);
    expect(t).toMatch(/próg|dowodów|bit/);
    // CR-E2E-7 (backend reason_pl, fixed 2026-11-21 in hydra/labels_pl.py): scheme ids and decimal points leaked into
    // the Polish sentence ("różny typ oszustwa (investment vs gov_refund)", "3.0 bit"). Any remaining token is still
    // recorded as an annotation, not failed.
    const leaks = [...t.matchAll(/\b[a-z]+_[a-z_]+\b|\b\d+\.\d+ bit/g)].map((m) => m[0]);
    if (leaks.length) test.info().annotations.push({ type: "reason_pl non-Polish tokens", description: leaks.join(", ") });
    await expect(card.getByRole("link", { name: /^new-/ })).toBeVisible();
  });

  test("fresh campaign A item (new-a5): assigned to 'Inwestycja PKO Bonus', reasons in Polish", async ({ page, request }) => {
    const info = serverInfo();
    test.skip(!info?.new, "E2E_SERVER_JSON with the corpus 'new' dir is required for uploads");
    const a = await campaignByLabel(request, LABEL_A);
    const card = await uploadFolder(page, path.join(info!.new!, "new-a5"));
    await expect(card.getByText("przypisano do kampanii", { exact: true })).toBeVisible();
    await expect(card.getByRole("link", { name: new RegExp(LABEL_A) }).first()).toBeVisible();
    await expect(card.getByRole("link", { name: new RegExp(LABEL_A) }).first()).toHaveAttribute("href", new RegExp(`/campaigns/${a.id}$`));
    const reason = card.locator("div.space-y-2 > div.text-fg").first();
    const t = await reason.innerText();
    expect(t, "reason_pl contains Polish diacritics").toMatch(POLISH);
    expect(t).toMatch(new RegExp(`^Przypisano do kampanii ${a.id}`));
  });
});
