import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { UNAUTHORIZED_EVENT } from "../../api/auth";
import { AuthGuard } from "./AuthGuard";
import { buildHelix, FAMILIES, SCENES } from "./LoginArtwork";
import { LoginPage } from "./LoginPage";
import { ForcedPasswordPage, passwordRequirements } from "./PasswordChangeForm";

const HINT = { file: "/data/state/admin_password.txt", docker: "docker compose exec hydra cat /data/state/admin_password.txt", env: "HYDRA_ADMIN_PASSWORD" };
const USER = { id: 1, username: "admin", role: "admin", must_change_password: false, disabled: false, created_at: "2026-11-22T01:00:00Z", last_login: null, locked_until: null, failed_attempts: 0 };

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{`${loc.pathname}${loc.search}`}</div>;
}

function routes() {
  return (
    <Routes>
      <Route path="/login" element={<><LoginPage /><Where /></>} />
      <Route path="/login/change-password" element={<><ForcedPasswordPage /><Where /></>} />
      <Route path="*" element={<AuthGuard><div data-testid="app">APP</div><Where /></AuthGuard>} />
    </Routes>
  );
}

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function server(session: object, extra: Handler = () => jsonResponse({}, { status: 404 })) {
  return mockFetch(async (input, init) => {
    const url = String(input);
    if (url.includes("/v1/auth/session")) return jsonResponse(session);
    return extra(url, init);
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("route guard", () => {
  it("unauthenticated: redirects to /login with next = current path and search", async () => {
    server({ auth_enabled: true, authenticated: false, credentials_hint: HINT });
    renderWithProviders(routes(), { route: "/campaigns?tab=2" });
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/login?next=%2Fcampaigns%3Ftab%3D2"));
    expect(screen.getByRole("heading", { name: "Zaloguj się do panelu" })).toBeInTheDocument();
    expect(screen.queryByTestId("app")).toBeNull();
  });

  it("auth disabled: renders the app", async () => {
    server({ auth_enabled: false, authenticated: false });
    renderWithProviders(routes(), { route: "/pairs" });
    expect(await screen.findByTestId("app")).toBeInTheDocument();
  });

  it("no session endpoint (older backend / tests): renders the app", async () => {
    mockFetch(async () => jsonResponse({ detail: "Not Found" }, { status: 404 }));
    renderWithProviders(routes(), { route: "/pairs" });
    expect(await screen.findByTestId("app")).toBeInTheDocument();
  });

  it("temporary password: sends the user to the change screen", async () => {
    server({ auth_enabled: true, authenticated: true, user: { ...USER, must_change_password: true } });
    renderWithProviders(routes(), { route: "/queue" });
    expect(await screen.findByRole("heading", { name: "Ustaw nowe hasło" })).toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent("/login/change-password?next=%2Fqueue");
  });
});

describe("expired session", () => {
  it("a 401 while signed in redirects with reason=expired and shows the notice", async () => {
    let signedIn = true;
    mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/v1/auth/session")) return jsonResponse(signedIn ? { auth_enabled: true, authenticated: true, user: USER } : { auth_enabled: true, authenticated: false });
      return jsonResponse({}, { status: 404 });
    });
    renderWithProviders(routes(), { route: "/pairs" });
    expect(await screen.findByTestId("app")).toBeInTheDocument();
    signedIn = false;
    act(() => { window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT, { detail: { path: "/v1/pairs" } })); });
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/login?next=%2Fpairs&reason=expired"));
    expect(await screen.findByTestId("session-expired")).toHaveTextContent("Sesja wygasła — zaloguj się ponownie");
  });
});

describe("login page", () => {
  it("renders labelled fields with autocomplete attributes and the credentials hint", async () => {
    server({ auth_enabled: true, authenticated: false, credentials_hint: HINT });
    renderWithProviders(routes(), { route: "/login" });
    const user = await screen.findByLabelText("Login");
    expect(user).toHaveAttribute("autocomplete", "username");
    expect(screen.getByLabelText("Hasło")).toHaveAttribute("autocomplete", "current-password");
    expect(screen.getByText("HYDRA NEXUS")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Jak znaleźć dane administratora/ }));
    await waitFor(() => expect(screen.getByTestId("credentials-popover")).toHaveTextContent(HINT.file));
    expect(screen.getByTestId("credentials-popover")).toHaveTextContent(HINT.docker);
  });

  it("show/hide password toggles the input type and aria-pressed", async () => {
    server({ auth_enabled: true, authenticated: false });
    renderWithProviders(routes(), { route: "/login" });
    const input = await screen.findByLabelText("Hasło");
    expect(input).toHaveAttribute("type", "password");
    const toggle = screen.getByRole("button", { name: "Pokaż hasło" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(input).toHaveAttribute("type", "text");
    expect(screen.getByRole("button", { name: "Ukryj hasło" })).toHaveAttribute("aria-pressed", "true");
  });

  it("warns about Caps Lock from getModifierState", async () => {
    server({ auth_enabled: true, authenticated: false });
    renderWithProviders(routes(), { route: "/login" });
    const input = await screen.findByLabelText("Hasło");
    const ev = new KeyboardEvent("keydown", { key: "A", bubbles: true });
    Object.defineProperty(ev, "getModifierState", { value: (k: string) => k === "CapsLock" });
    act(() => { input.dispatchEvent(ev); });
    expect(await screen.findByText("Włączony Caps Lock")).toBeInTheDocument();
  });

  it("shows the Polish error on 401", async () => {
    server({ auth_enabled: true, authenticated: false }, (url) =>
      url.includes("/v1/auth/login") ? jsonResponse({ detail: { code: "invalid_credentials", message: "Nieprawidłowy login lub hasło." } }, { status: 401 }) : jsonResponse({}, { status: 404 }));
    renderWithProviders(routes(), { route: "/login" });
    fireEvent.change(await screen.findByLabelText("Login"), { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("Hasło"), { target: { value: "zle-haslo" } });
    fireEvent.click(screen.getByRole("button", { name: "Zaloguj się" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Nieprawidłowy login lub hasło");
    expect(screen.getByLabelText("Hasło")).toHaveValue("");
  });

  it("lockout: live countdown and a disabled submit", async () => {
    server({ auth_enabled: true, authenticated: false }, (url) =>
      url.includes("/v1/auth/login") ? jsonResponse({ detail: { code: "locked", message: "Konto zablokowane.", retry_after_s: 300 } }, { status: 423 }) : jsonResponse({}, { status: 404 }));
    renderWithProviders(routes(), { route: "/login" });
    fireEvent.change(await screen.findByLabelText("Login"), { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("Hasło"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Zaloguj się" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Konto tymczasowo zablokowane. Spróbuj ponownie za 5:00");
    expect(screen.getByTestId("login-submit")).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/za 4:5\d$/), { timeout: 3000 });
  });

  it("rate limit: message with countdown", async () => {
    server({ auth_enabled: true, authenticated: false }, (url) =>
      url.includes("/v1/auth/login") ? jsonResponse({ detail: { code: "rate_limited", message: "Za dużo prób.", retry_after_s: 42 } }, { status: 429 }) : jsonResponse({}, { status: 404 }));
    renderWithProviders(routes(), { route: "/login" });
    fireEvent.change(await screen.findByLabelText("Login"), { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("Hasło"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Zaloguj się" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Zbyt wiele prób logowania z tego adresu. Spróbuj ponownie za 0:42");
  });

  it("success goes to next; login POST carries remember", async () => {
    let signedIn = false;
    const fetchMock = mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/v1/auth/session")) return jsonResponse(signedIn ? { auth_enabled: true, authenticated: true, user: USER } : { auth_enabled: true, authenticated: false });
      if (url.includes("/v1/auth/login")) {
        signedIn = true;
        return jsonResponse({ user: USER, csrf_token: "c", expires_at: "2026-11-23T00:00:00Z", idle_expires_at: "2026-11-22T05:00:00Z" });
      }
      return jsonResponse({}, { status: 404 });
    });
    renderWithProviders(routes(), { route: "/login?next=%2Fincidents" });
    fireEvent.change(await screen.findByLabelText("Login"), { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("Hasło"), { target: { value: "dobre-haslo-2026" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Zapamiętaj mnie/ }));
    fireEvent.click(screen.getByRole("button", { name: "Zaloguj się" }));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/incidents"));
    const call = fetchMock.mock.calls.find(([u]) => String(u).includes("/v1/auth/login"))!;
    expect(JSON.parse(String(call[1]?.body))).toEqual({ username: "admin", password: "dobre-haslo-2026", remember: true });
  });
});

describe("login artwork", () => {
  it("DNA scenes are deterministic; helix base pairs carry gene families", () => {
    const opts = { cx: 400, y0: 0, y1: 900, amp: 100, period: 300, seed: 11 };
    expect(buildHelix(opts)).toEqual(buildHelix(opts));
    const h = buildHelix(opts);
    expect(h.rungs.length).toBeGreaterThan(20);
    for (const r of h.rungs) {
      expect(r.fam).toBeGreaterThanOrEqual(0);
      expect(r.fam).toBeLessThan(FAMILIES.length);
      expect(r.pair).not.toBe(r.fam);
    }
    expect(FAMILIES).toEqual(["VIS", "TXT", "INF", "ID", "NAR", "META"]);
    expect(SCENES.map((s) => s.caption)).toEqual(["Genom incydentu", "Rzadki gen", "Ta sama kampania", "Węzeł DNA", "Sekwencja dowodów"]);
  });

  it("carousel shows the first scene and can be paused", async () => {
    server({ auth_enabled: true, authenticated: false });
    renderWithProviders(routes(), { route: "/login" });
    const art = await screen.findByTestId("login-artwork");
    expect(art).toHaveAttribute("data-scene", "genome");
    expect(art).toHaveTextContent("ilustracja");
    const pause = screen.getByTestId("scene-pause");
    expect(pause).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(pause);
    expect(pause).toHaveAttribute("aria-pressed", "true");
  });
});

describe("password requirements", () => {
  it("tick live: length, username, distinct characters, different from current, matching repeat", () => {
    const met = (c: string, n: string, r: string) => Object.fromEntries(passwordRequirements(c, n, r, "admin").map((x) => [x.id, x.met]));
    expect(met("", "", "")).toEqual({ length: false, username: false, distinct: false, current: false, repeat: false });
    expect(met("stare-haslo", "admin-123456789", "")).toMatchObject({ length: true, username: false, repeat: false });
    expect(met("Nowe-Haslo#Wisla", "Nowe-Haslo#Wisla", "Nowe-Haslo#Wisla")).toMatchObject({ current: false, repeat: true });
    expect(met("stare", "Zażółć-gęślą-jaźń", "Zażółć-gęślą-jaźń".normalize("NFD"))).toEqual({ length: true, username: true, distinct: true, current: true, repeat: true });
    expect(met("x", "aaaaaaaaaaaa", "aaaaaaaaaaaa").distinct).toBe(false);
  });
});

describe("forced change screen", () => {
  it("shows the checklist and the segmented meter", async () => {
    server({ auth_enabled: true, authenticated: true, user: { ...USER, must_change_password: true } });
    renderWithProviders(routes(), { route: "/login/change-password" });
    expect(await screen.findByRole("heading", { name: "Ustaw nowe hasło" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Nowe hasło"), { target: { value: "Nowe-Haslo#Wisla2026" } });
    expect(screen.getByTestId("pw-req-length")).toHaveAttribute("data-met", "true");
    expect(screen.getByTestId("pw-req-repeat")).toHaveAttribute("data-met", "false");
    fireEvent.change(screen.getByLabelText("Powtórz nowe hasło"), { target: { value: "Nowe-Haslo#Wisla2026" } });
    expect(screen.getByTestId("pw-req-repeat")).toHaveAttribute("data-met", "true");
    expect(screen.getByTestId("strength-meter")).toHaveTextContent(/Siła hasła: (Silne|Bardzo silne)/);
  });
});
