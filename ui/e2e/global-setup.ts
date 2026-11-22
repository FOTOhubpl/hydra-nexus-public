/**
 * Starts the throwaway HYDRA API of tests/e2e/server.py (synthetic corpus, own temp state, own free port, built UI
 * at /) and exposes it to the workers through E2E_BASE_URL / E2E_SERVER_JSON. The returned function is the global
 * teardown: SIGTERM to the server process group, then to the API pid as a fallback, then the temp root is removed.
 *
 * E2E_BASE_URL set by the caller: reuse that server (E2E_SERVER_JSON optional, needed only by the upload test).
 */
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { request as pwRequest } from "playwright/test";
import { STORAGE_STATE, authInfo } from "./authState";

/**
 * Log in once as the e2e admin and save the cookies (hydra_session, hydra_csrf) as the shared storageState. Without
 * credentials in server.json (auth off) an empty state is written so the projects still start.
 */
async function saveLoginState(baseURL: string): Promise<void> {
  mkdirSync(path.dirname(STORAGE_STATE), { recursive: true });
  const { admin_user, admin_password } = authInfo();
  const ctx = await pwRequest.newContext({ baseURL });
  try {
    if (admin_user && admin_password) {
      const r = await ctx.post("/api/v1/auth/login", { data: { username: admin_user, password: admin_password, remember: true } });
      if (!r.ok()) throw new Error(`[e2e] admin login failed: HTTP ${r.status()} ${await r.text()}`);
      console.log(`[e2e] logged in as ${admin_user}; storageState ${STORAGE_STATE}`);
    }
    await ctx.storageState({ path: STORAGE_STATE });
  } finally {
    await ctx.dispose();
  }
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WORKTREE = path.resolve(HERE, "..", "..");
const FORBIDDEN_PORTS = new Set(["8000", "8100", "5173"]);
const READY_TIMEOUT_MS = 5 * 60_000;

function assertSafe(url: string): void {
  const u = new URL(url);
  if (!["127.0.0.1", "localhost", "::1", "[::1]"].includes(u.hostname)) throw new Error(`E2E_BASE_URL must be loopback: ${url}`);
  if (FORBIDDEN_PORTS.has(u.port)) throw new Error(`E2E_BASE_URL must not be a demo/dev port (8000/8100/5173): ${url}`);
}

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function waitDead(pid: number, ms: number): Promise<boolean> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (!alive(pid)) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return !alive(pid);
}

export default async function globalSetup(): Promise<(() => Promise<void>) | void> {
  if (process.env.E2E_BASE_URL) {
    assertSafe(process.env.E2E_BASE_URL);
    console.log(`[e2e] reusing server ${process.env.E2E_BASE_URL}`);
    await saveLoginState(process.env.E2E_BASE_URL);
    return;
  }
  const root = path.join("/tmp", `hydra-e2e-ui-${Date.now()}-${process.pid}`);
  const ready = path.join(root, "server.json");
  const dist = path.join(WORKTREE, "ui", "dist");
  if (!existsSync(path.join(dist, "index.html"))) throw new Error(`ui/dist missing: run 'cd ui && pnpm build' (or ui/e2e/run.sh)`);
  mkdirSync(root, { recursive: true });

  const t0 = Date.now();
  const child: ChildProcess = spawn(
    "uv",
    ["run", "python", "-m", "tests.e2e.server", "--root", path.join(root, "root"), "--ready", ready, "--ui-dist", dist],
    { cwd: WORKTREE, detached: true, stdio: ["ignore", "ignore", "inherit"], env: { ...process.env } },
  );
  let exited: number | null = null;
  child.on("exit", (code) => {
    exited = code ?? -1;
  });

  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (!existsSync(ready)) {
    if (exited !== null) throw new Error(`tests.e2e.server exited with ${exited} before it was ready (see stderr above)`);
    if (Date.now() > deadline) {
      if (child.pid) process.kill(-child.pid, "SIGTERM");
      throw new Error("tests.e2e.server not ready within 5 min");
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  await new Promise((r) => setTimeout(r, 200)); // the ready file is written in one call; let it land
  const info = JSON.parse(readFileSync(ready, "utf8")) as { base_url: string; pid: number };
  assertSafe(info.base_url);
  process.env.E2E_BASE_URL = info.base_url;
  process.env.E2E_SERVER_JSON = ready;
  console.log(`[e2e] server ${info.base_url} ready in ${Math.round((Date.now() - t0) / 1000)} s (root ${root})`);
  try {
    await saveLoginState(info.base_url);
  } catch (err) {
    if (child.pid) process.kill(-child.pid, "SIGTERM");
    throw err;
  }

  return async () => {
    if (child.pid && alive(child.pid)) {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        /* group already gone */
      }
      await waitDead(child.pid, 25_000);
    }
    if (alive(info.pid)) {
      process.kill(info.pid, "SIGTERM");
      if (!(await waitDead(info.pid, 10_000))) process.kill(info.pid, "SIGKILL");
    }
    if (process.env.E2E_KEEP !== "1") rmSync(root, { recursive: true, force: true });
    else console.log(`[e2e] kept ${root}`);
  };
}
