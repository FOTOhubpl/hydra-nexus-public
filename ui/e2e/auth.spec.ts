/**
 * Panel login (AUTH-LOGIN), run by the auth-desktop / auth-mobile projects with an empty storageState: every test
 * signs in inside its own browser context, so logging out never ends the shared session of the other projects.
 * Wrong-password attempts stay at one per project (lockout: 5 failures / 5 min per username).
 */
import { expect, shellReady, test } from "./fixtures";
import { authInfo } from "./authState";
import type { Page } from "playwright/test";

const info = authInfo();
const ADMIN = info.admin_user ?? "admin";
const PASSWORD = info.admin_password ?? "";

test.skip(!PASSWORD, "e2e server runs without auth (no admin_password in server.json)");

async function signIn(page: Page, username: string, password: string): Promise<void> {
  await page.getByLabel("Login", { exact: true }).fill(username);
  await page.getByLabel("Hasło", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Zaloguj się" }).click();
}

test.describe("login", () => {
  test("protected page redirects to login; wrong password shows the Polish error; success lands on next", async ({ page }) => {
    await page.goto("/campaigns?view=list");
    await expect(page).toHaveURL(/\/login\?next=%2Fcampaigns%3Fview%3Dlist$/);
    await expect(page.getByRole("heading", { name: "Zaloguj się" })).toBeVisible();
    await expect(page.getByTestId("login-card")).toBeVisible();
    await expect(page.getByText("HYDRA NEXUS").first()).toBeVisible();

    // credentials hint from GET /v1/auth/session
    await page.getByRole("button", { name: "Jak znaleźć dane administratora?" }).click();
    await expect(page.getByTestId("credentials-popover")).toContainText("HYDRA_ADMIN_PASSWORD");
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("credentials-popover")).toBeHidden();

    // show / hide
    const pw = page.getByLabel("Hasło", { exact: true });
    await expect(pw).toHaveAttribute("type", "password");
    await page.getByRole("button", { name: "Pokaż hasło" }).click();
    await expect(pw).toHaveAttribute("type", "text");
    await page.getByRole("button", { name: "Ukryj hasło" }).click();

    await signIn(page, ADMIN, `${PASSWORD}-wrong`);
    await expect(page.getByTestId("login-error")).toContainText("Nieprawidłowy login lub hasło");
    await expect(page).toHaveURL(/\/login\?next=/);

    await signIn(page, ADMIN, PASSWORD);
    await expect(page).toHaveURL(/\/campaigns\?view=list$/);
    await shellReady(page);
    await expect(page.getByTestId("user-menu")).toBeVisible();
  });

  test("user menu shows the role; Wyloguj ends the session and protected pages redirect to login", async ({ page }) => {
    await page.goto("/login");
    await signIn(page, ADMIN, PASSWORD);
    await expect(page).toHaveURL(/\/$/);
    await shellReady(page);
    await page.getByTestId("user-menu").getByRole("button", { name: "Konto użytkownika" }).click();
    const panel = page.getByRole("dialog", { name: "Konto użytkownika" });
    await expect(panel).toContainText(ADMIN);
    await expect(panel.getByTestId("role-pill")).toHaveText("Administrator");
    await expect(panel.getByRole("link", { name: "Użytkownicy" })).toBeVisible();
    await panel.getByTestId("logout").click();
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByRole("heading", { name: "Zaloguj się" })).toBeVisible();
    await page.goto("/pairs");
    await expect(page).toHaveURL(/\/login\?next=%2Fpairs$/);
  });
});

test.describe("forced password change", () => {
  test("a new user logs in with the generated password, must set a new one, then reaches the app", async ({ page, request }, testInfo) => {
    const username = `e2e-${testInfo.project.name}-${Date.now().toString(36)}`;
    const created = await request.post("/v1/auth/users", { data: { username, role: "analyst" } });
    expect(created.status(), await created.text()).toBe(201);
    const { password } = (await created.json()) as { password: string };

    await page.goto("/queue");
    await expect(page).toHaveURL(/\/login\?next=%2Fqueue$/);
    await signIn(page, username, password);
    await expect(page).toHaveURL(/\/login\/change-password\?next=%2Fqueue$/);
    await expect(page.getByRole("heading", { name: "Ustaw nowe hasło" })).toBeVisible();

    const next = `Zielony-Kot-${Date.now().toString(36)}#Wisla-2026`;  // no common word: the meter must say Silne
    await page.getByLabel("Obecne hasło", { exact: true }).fill(password);
    await page.getByLabel("Nowe hasło", { exact: true }).fill(next);
    await expect(page.getByTestId("strength-meter")).toContainText(/Siła hasła: (Silne|Bardzo silne)/);
    await page.getByLabel("Powtórz nowe hasło", { exact: true }).fill(next);
    await page.getByRole("button", { name: "Ustaw hasło i przejdź dalej" }).click();
    await expect(page).toHaveURL(/\/queue$/);
    await shellReady(page);
  });
});
