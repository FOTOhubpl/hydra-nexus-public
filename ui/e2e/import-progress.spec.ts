/**
 * IMPORT-PROGRESS: GUI upload of a synthetic multi-file incident ZIP; the activity panel on /analyse shows the
 * two-phase stepper (Wysyłanie → Import → ... → Gotowe) and its bars reach 100 % (role=progressbar, aria-valuenow).
 * The ZIP is written by the system python3 (stdlib zipfile) into a private temp dir under E2E_OUT_DIR.
 */
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { expect, test } from "./fixtures";

const FILES = 40;

function makeZip(): { dir: string; zip: string } {
  const base = process.env.E2E_OUT_DIR ?? os.tmpdir();
  mkdirSync(base, { recursive: true });
  const dir = mkdtempSync(path.join(base, "import-progress-"));
  const zip = path.join(dir, "incydent-postep.zip");
  const script = [
    "import sys, zipfile, struct, zlib",
    "def png(c):",
    "    raw = b''.join(b'\\x00' + bytes([c, 40, 90]) * 16 for _ in range(12))",
    "    chunk = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d))",
    "    return b'\\x89PNG\\r\\n\\x1a\\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 16, 12, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')",
    "n = int(sys.argv[2])",
    "with zipfile.ZipFile(sys.argv[1], 'w', zipfile.ZIP_DEFLATED) as z:",
    "    z.writestr('reklama.png', png(211))",
    "    z.writestr('strona.html', '<html><head><title>Postep importu</title></head><body>oferta testowa postep-importu.invalid</body></html>')",
    "    for k in range(n - 2):",
    "        z.writestr(f'notatki/notatka_{k:03d}.txt', f'notatka {k} postep importu https://postep-importu.invalid/{k}\\n')",
  ].join("\n");
  execFileSync("python3", ["-c", script, zip, String(FILES)]);
  return { dir, zip };
}

test.describe("import progress @mutating", () => {
  test.describe.configure({ timeout: 180_000 });
  test.beforeEach(({}, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-light-mutating", "runs in desktop-light-mutating only");
  });

  test("synthetic multi-file ZIP: upload and import bars reach 100 %", async ({ page }) => {
    const { dir, zip } = makeZip();
    try {
      await page.goto("/analyse");
      await page.getByRole("tab", { name: "Plik lub ZIP incydentu" }).click();
      await page.getByTestId("analyse-file-input").setInputFiles(zip);
      await expect(page.getByText(/wybrano 1 plik\(ów\)/)).toBeVisible();
      const posted = page.waitForResponse((r) => r.url().endsWith("/api/v1/analyse") && r.request().method() === "POST");
      await page.getByRole("button", { name: "Analizuj", exact: true }).click();
      expect((await posted).status()).toBe(202);
      const live = page.getByRole("dialog", { name: "Analiza na żywo" });
      await expect(live).toBeVisible();
      await live.getByRole("button", { name: "Zamknij (Esc)" }).click();

      const panel = page.getByTestId("analyse-activity");
      await expect(panel).toBeVisible();
      await expect(panel.getByRole("listitem").filter({ hasText: "Wysyłanie" })).toContainText("(zakończony)");
      await expect(panel.getByRole("listitem").filter({ hasText: "Import" }).first()).toContainText("(zakończony)", { timeout: 120_000 });
      const overall = panel.getByTestId("activity-overall-bar");
      await expect(overall).toHaveAttribute("role", "progressbar");
      await expect(overall).toHaveAttribute("aria-valuenow", "100", { timeout: 120_000 });
      await expect(panel.getByTestId("activity-stage-bar")).toHaveAttribute("aria-valuenow", "100");
      await expect(panel.getByTestId("activity-done")).toContainText(`Import zakończony: ${FILES} plików`);
      await expect(panel.getByRole("listitem").filter({ hasText: "Gotowe" })).toContainText("(zakończony)");
      await panel.screenshot({ path: path.join(process.env.E2E_OUT_DIR ?? os.tmpdir(), "import-progress-done.png") });
      const snap = await page.request.get(`/v1/analyse/${await jobIdOf(page)}/progress`);
      const body = (await snap.json()) as { import: { files_total: number; files_done: number; percent: number; phase: string } };
      expect(body.import).toMatchObject({ files_total: FILES, files_done: FILES, percent: 100, phase: "done" });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

async function jobIdOf(page: import("playwright/test").Page): Promise<string> {
  const text = await page.getByTestId("analyse-result").locator("span.font-mono").first().innerText();
  const m = /zadanie (\w+)/.exec(text);
  if (!m) throw new Error(`no job id in "${text}"`);
  return m[1]!;
}
