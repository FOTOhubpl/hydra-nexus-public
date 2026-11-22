import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "./client";
import { authHeaders, getSession, loginPath, parseSession, readCookie, safeNext, UNAUTHORIZED_EVENT, PASSWORD_CHANGE_EVENT } from "./auth";
import { jsonResponse, mockFetch } from "../test/utils";

function setCookie(v: string) {
  document.cookie = v;
}

afterEach(() => {
  document.cookie = "hydra_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  vi.unstubAllGlobals();
});

describe("CSRF header", () => {
  it("authHeaders reads the hydra_csrf cookie for unsafe methods only", () => {
    setCookie("other=1; path=/");
    setCookie("hydra_csrf=tok%2Babc; path=/");
    expect(readCookie("hydra_csrf")).toBe("tok+abc");
    expect(authHeaders("POST")).toEqual({ "X-CSRF-Token": "tok+abc" });
    expect(authHeaders("delete")).toEqual({ "X-CSRF-Token": "tok+abc" });
    expect(authHeaders("GET")).toEqual({});
    expect(authHeaders()).toEqual({});
  });

  it("apiFetch sends X-CSRF-Token on POST/PUT/PATCH/DELETE and not on GET", async () => {
    setCookie("hydra_csrf=csrf-123; path=/");
    const fetchMock = mockFetch(async () => jsonResponse({ ok: true }));
    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      await apiFetch("/v1/x", { method, json: {} });
    }
    await apiFetch("/v1/x");
    const headers = fetchMock.mock.calls.map(([, init]) => new Headers(init?.headers).get("X-CSRF-Token"));
    expect(headers).toEqual(["csrf-123", "csrf-123", "csrf-123", "csrf-123", null]);
  });

  it("no cookie: no header (auth disabled servers ignore it)", async () => {
    const fetchMock = mockFetch(async () => jsonResponse({}));
    await apiFetch("/v1/x", { method: "POST" });
    expect(new Headers(fetchMock.mock.calls[0]![1]?.headers).has("X-CSRF-Token")).toBe(false);
  });
});

describe("401 / password change signals", () => {
  it("a 401 from a data endpoint fires the auth-required event; a 401 from login does not", async () => {
    const seen: string[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent<{ path: string }>).detail.path);
    window.addEventListener(UNAUTHORIZED_EVENT, on);
    mockFetch(async () => jsonResponse({ detail: { code: "auth_required", message: "Wymagane logowanie." } }, { status: 401 }));
    await expect(apiFetch("/v1/campaigns")).rejects.toThrow("Wymagane logowanie.");
    await expect(apiFetch("/v1/auth/login", { method: "POST", json: {} })).rejects.toThrow();
    window.removeEventListener(UNAUTHORIZED_EVENT, on);
    expect(seen).toEqual(["/v1/campaigns"]);
  });

  it("403 password_change_required fires the password-change event", async () => {
    const on = vi.fn();
    window.addEventListener(PASSWORD_CHANGE_EVENT, on);
    mockFetch(async () => jsonResponse({ detail: { code: "password_change_required", message: "Zmień hasło." } }, { status: 403 }));
    await expect(apiFetch("/v1/status")).rejects.toThrow();
    window.removeEventListener(PASSWORD_CHANGE_EVENT, on);
    expect(on).toHaveBeenCalledTimes(1);
  });
});

describe("session parsing", () => {
  it("non-conforming or failed answers mean auth disabled", async () => {
    expect(parseSession({ status: "ok" }).auth_enabled).toBe(false);
    expect(parseSession(null).auth_enabled).toBe(false);
    mockFetch(async () => jsonResponse({ detail: "Not Found" }, { status: 404 }));
    expect((await getSession()).auth_enabled).toBe(false);
    mockFetch(async () => { throw new TypeError("Failed to fetch"); });
    expect((await getSession()).auth_enabled).toBe(false);
  });

  it("authenticated only with a user", () => {
    expect(parseSession({ auth_enabled: true, authenticated: true }).authenticated).toBe(false);
    const s = parseSession({ auth_enabled: true, authenticated: true, user: { username: "anna", role: "analyst" } });
    expect(s.authenticated).toBe(true);
    expect(s.user?.username).toBe("anna");
  });
});

describe("next parameter", () => {
  it("keeps same-origin paths only and never loops back to /login", () => {
    expect(safeNext("/campaigns?x=1")).toBe("/campaigns?x=1");
    expect(safeNext("//evil.example/")).toBe("/");
    expect(safeNext("https://evil.example/")).toBe("/");
    expect(safeNext("/login?next=/a")).toBe("/");
    expect(safeNext(null)).toBe("/");
    expect(loginPath("/pairs?tab=2")).toBe("/login?next=%2Fpairs%3Ftab%3D2");
    expect(loginPath("/")).toBe("/login");
  });
});
