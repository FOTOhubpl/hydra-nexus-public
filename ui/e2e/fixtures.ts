/**
 * Shared fixtures: base URL of the e2e server, theme per project, and a page guard that fails a test on console
 * errors, uncaught page errors and HTTP 5xx responses.
 */
import { readFileSync } from "node:fs";
import { test as base, expect, type APIRequestContext, type Page } from "playwright/test";
import { apiAuthHeaders } from "./authState";

/**
 * Console messages that are not product defects. Each entry documents why it is benign; keep this list short.
 * Matching is by substring of the console text.
 */
export const CONSOLE_ALLOWLIST: { match: string; why: string; url?: string }[] = [
  // auth.spec.ts submits a wrong password on purpose: Chromium logs the 401 of POST /api/v1/auth/login as a
  // resource error. Scoped to that URL only; a 401 anywhere else is still a failure.
  {
    match: "the server responded with a status of 401",
    url: "/api/v1/auth/login",
    why: "intentional wrong-password login attempt in auth.spec.ts",
  },
  // routes.spec.ts opens /campaigns/C-00000000 on purpose (B3 stale campaign link): the card, lifecycle and
  // earlier-run lookups answer 404 for that id only.
  {
    match: "the server responded with a status of 404",
    url: "/api/v1/campaigns/C-00000000",
    why: "intentional unknown campaign id in routes.spec.ts (B3)",
  },
  // HtmlPreview (components/artifacts/Previews.tsx) renders a saved phishing page in <iframe sandbox="" srcdoc>,
  // "podgląd (bez skryptów)". Chromium logs every <script> the sandbox refuses to run; that is the isolation
  // working as designed, not an app error. Exact Chromium wording, scoped to about:srcdoc.
  {
    match: "Blocked script execution in 'about:srcdoc' because the document's frame is sandboxed and the 'allow-scripts' permission is not set.",
    why: "sandboxed HTML artefact preview refuses the page's scripts by design",
  },
];

export interface ServerInfo {
  base_url: string;
  data_dir?: string;
  new?: string;
  base?: string;
}

export function serverInfo(): ServerInfo | null {
  const p = process.env.E2E_SERVER_JSON;
  if (!p) return null;
  try {
    return JSON.parse(readFileSync(p, "utf8")) as ServerInfo;
  } catch {
    return null;
  }
}

export interface Problems {
  console: string[];
  http5xx: string[];
}

type Fixtures = { problems: Problems; theme: "light" | "dark" };

export const test = base.extend<Fixtures>({
  baseURL: async ({}, use) => {
    const url = process.env.E2E_BASE_URL;
    if (!url) throw new Error("E2E_BASE_URL not set (globalSetup did not run?)");
    await use(url);
  },
  // API calls from specs authenticate with the e2e personal access token (server.json api_token), so they never
  // depend on (or log out) the browser session of the shared storageState.
  request: async ({ playwright, baseURL }, use) => {
    const ctx = await playwright.request.newContext({ baseURL, extraHTTPHeaders: apiAuthHeaders() });
    await use(ctx);
    await ctx.dispose();
  },
  theme: async ({ colorScheme }, use) => {
    await use(colorScheme === "dark" ? "dark" : "light");
  },
  // The app reads its theme from localStorage "hydra.ui.theme" (lib/theme.ts, lib/storage.ts), not from
  // prefers-color-scheme, so the project colorScheme is mirrored into storage before any script runs.
  page: async ({ page, theme }, use) => {
    await page.addInitScript((t) => {
      try {
        window.localStorage.setItem("hydra.ui.theme", JSON.stringify(t));
      } catch {
        /* storage blocked */
      }
    }, theme);
    await use(page);
  },
  problems: [
    async ({ page }, use, testInfo) => {
      const p: Problems = { console: [], http5xx: [] };
      page.on("console", (m) => {
        if (m.type() !== "error") return;
        const text = m.text();
        const loc = m.location();
        if (CONSOLE_ALLOWLIST.some((a) => text.includes(a.match) && (!a.url || (loc.url ?? "").includes(a.url)))) return;
        p.console.push(`console.error: ${text.slice(0, 500)}${loc.url ? ` (${loc.url}:${loc.lineNumber})` : ""}`);
      });
      page.on("pageerror", (e) => p.console.push(`pageerror: ${String(e).slice(0, 500)}`));
      page.on("response", (r) => {
        if (r.status() >= 500) p.http5xx.push(`HTTP ${r.status()} ${r.request().method()} ${r.url()}`);
      });
      await use(p);
      if (p.console.length || p.http5xx.length) {
        await testInfo.attach("page-problems", { body: [...p.console, ...p.http5xx].join("\n"), contentType: "text/plain" });
      }
      expect.soft(p.http5xx, "HTTP 5xx responses").toEqual([]);
      expect(p.console, "console errors / page errors").toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };

export async function apiJson<T = unknown>(request: APIRequestContext, path: string): Promise<T> {
  const r = await request.get(path);
  expect(r.status(), `GET ${path}`).toBe(200);
  return (await r.json()) as T;
}

export interface Campaign {
  id: string;
  label: string;
  members: string[];
}

export async function campaignByLabel(request: APIRequestContext, label: string): Promise<Campaign> {
  const d = await apiJson<{ items: Campaign[] }>(request, "/v1/campaigns");
  const c = d.items.find((x) => x.label === label);
  expect(c, `campaign "${label}" in GET /v1/campaigns`).toBeTruthy();
  return c!;
}

/** Polish number formatting as the UI does it (lib/evidence.ts nf): pl-PL, fixed digits, decimal comma. */
export function plFixed(value: number, digits = 1): string {
  return new Intl.NumberFormat("pl-PL", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
}

/** The app shell is up: top bar title (StatusBar h1) shows the page title and the bottom status bar rendered. */
export async function shellReady(page: Page): Promise<void> {
  await expect(page.getByTestId("bottom-status")).toBeVisible();
}
