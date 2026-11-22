/**
 * G4 scenarios (internal G4 scenario notes, not published): one desktop-light project, serial, against our own already-running
 * tests/e2e/g4_server.py (E2E_BASE_URL + E2E_SERVER_JSON are required; globalSetup only logs in). Run from ui/e2e:
 *   E2E_BASE_URL=http://127.0.0.1:PORT E2E_SERVER_JSON=ROOT/server.json G4_OUT_DIR=~/HydraNexus/qa/g4 \
 *   E2E_OUT_DIR=$G4_OUT_DIR G4_PYTHON=<python with Pillow> node <playwright>/cli.js test -c g4.config.ts
 * The restart spec (g4-9) stops our own launcher (pid in server.json) and starts it again with --restart.
 */
import { defineConfig } from "playwright/test";
import { STORAGE_STATE } from "./authState";

const OUT = process.env.G4_OUT_DIR ?? "/tmp/hydra-g4";

export default defineConfig({
  testDir: ".",
  testMatch: /g4-.*\.spec\.ts$/,
  outputDir: `${OUT}/artifacts`,
  globalSetup: "./global-setup.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 600_000,
  expect: { timeout: 20_000 },
  reporter: [["list"], ["json", { outputFile: `${OUT}/g4-report.json` }]],
  use: {
    browserName: "chromium",
    headless: true,
    locale: "pl-PL",
    timezoneId: "Europe/Warsaw",
    trace: "off",
    screenshot: "only-on-failure",
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
    viewport: { width: 1440, height: 900 },
    colorScheme: "light",
    storageState: STORAGE_STATE,
  },
});
