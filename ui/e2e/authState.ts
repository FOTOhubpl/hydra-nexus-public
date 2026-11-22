/**
 * Shared login state for the e2e projects (AUTH-LOGIN). globalSetup logs in once as the e2e admin
 * (server.json: admin_user / admin_password) and writes a Playwright storageState file with the session and CSRF
 * cookies; every read-only project uses it. Auth specs that log out or change passwords use their own empty context.
 */
import { readFileSync } from "node:fs";

const OUT = process.env.E2E_OUT_DIR ?? "/tmp/hydra-e2e-ui-results";
export const STORAGE_STATE = process.env.E2E_STORAGE_STATE ?? `${OUT}/storage-state.json`;

export interface AuthInfo {
  admin_user?: string;
  admin_password?: string;
  api_token?: string;
}

export function authInfo(): AuthInfo {
  const p = process.env.E2E_SERVER_JSON;
  if (!p) return {};
  try {
    return JSON.parse(readFileSync(p, "utf8")) as AuthInfo;
  } catch {
    return {};
  }
}

/** Bearer header for API calls made by specs outside the browser (empty when the server runs without auth). */
export function apiAuthHeaders(): Record<string, string> {
  const t = authInfo().api_token;
  return t ? { Authorization: `Bearer ${t}` } : {};
}
