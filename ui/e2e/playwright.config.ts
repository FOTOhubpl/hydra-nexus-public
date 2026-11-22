/**
 * Playwright UI end-to-end suite (headless chromium). Run through ui/e2e/run.sh, which resolves Playwright from
 * ui/node_modules or the npx cache without adding a dependency.
 *
 * globalSetup starts tests/e2e/server.py (own temp state, own free loopback port, synthetic corpus analysed, built
 * UI served at /) unless E2E_BASE_URL points at an already-running e2e server. Never point it at the demo APIs.
 *
 * Projects: {desktop 1440x900, mobile 390x844} x {light, dark}. Tests tagged @mutating (GUI upload) change the server
 * state, so they run last in their own project that depends on the four read-only projects.
 */
import { defineConfig } from "playwright/test";
import { STORAGE_STATE } from "./authState";

/** No cookies: the auth specs start signed out. */
const EMPTY_STATE = { cookies: [], origins: [] };

const OUT = process.env.E2E_OUT_DIR ?? "/tmp/hydra-e2e-ui-results";
const desktop = { viewport: { width: 1440, height: 900 } };
const mobile = { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 };
const READ_ONLY = ["desktop-light", "desktop-dark", "mobile-light", "mobile-dark"];

export default defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.ts$/,
  // G4 scenario specs need their own server (ui/e2e/g4.config.ts, g4-run.sh); keep them out of the default run.
  testIgnore: /g4-.*\.spec\.ts$/,
  outputDir: `${OUT}/artifacts`,
  globalSetup: "./global-setup.ts",
  fullyParallel: true,
  workers: Number(process.env.E2E_WORKERS ?? 3),
  retries: 0,
  forbidOnly: true,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["json", { outputFile: `${OUT}/report.json` }]],
  use: {
    browserName: "chromium",
    headless: true,
    locale: "pl-PL",
    timezoneId: "Europe/Warsaw",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    // Read-only projects share one admin session (globalSetup logs in once, authState.ts). auth.spec.ts logs in
    // and out in its own fresh contexts, so it never ends the shared session.
    { name: "desktop-light", use: { ...desktop, colorScheme: "light", storageState: STORAGE_STATE }, grepInvert: /@mutating/, testIgnore: /(auth|g4-.*)\.spec\.ts$/ },
    { name: "desktop-dark", use: { ...desktop, colorScheme: "dark", storageState: STORAGE_STATE }, grepInvert: /@mutating/, testIgnore: /(auth|g4-.*)\.spec\.ts$/ },
    { name: "mobile-light", use: { ...mobile, colorScheme: "light", storageState: STORAGE_STATE }, testMatch: /routes\.spec\.ts$/ },
    { name: "mobile-dark", use: { ...mobile, colorScheme: "dark", storageState: STORAGE_STATE }, testMatch: /routes\.spec\.ts$/ },
    { name: "auth-desktop", use: { ...desktop, colorScheme: "light", storageState: EMPTY_STATE }, testMatch: /auth\.spec\.ts$/ },
    { name: "auth-mobile", use: { ...mobile, colorScheme: "dark", storageState: EMPTY_STATE }, testMatch: /auth\.spec\.ts$/ },
    {
      name: "desktop-light-mutating",
      use: { ...desktop, colorScheme: "light", storageState: STORAGE_STATE },
      grep: /@mutating/,
      testIgnore: /(auth|g4-.*)\.spec\.ts$/,
      dependencies: READ_ONLY,
    },
  ],
});
