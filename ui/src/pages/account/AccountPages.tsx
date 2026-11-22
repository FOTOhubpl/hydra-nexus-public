/**
 * Account pages inside AppShell: /account/password (change password), /account/tokens (personal API tokens),
 * /admin/users (user management, admin only). Secrets (new token, generated password) are shown once, in a copyable
 * box, and never written to the query cache or localStorage.
 */
import { useState, type FormEvent, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  apiErrorCode,
  ROLE_LABEL,
  ROLES,
  sessionKey,
  useCreateToken,
  useCreateUser,
  useCurrentUser,
  useResetPassword,
  useRevokeToken,
  useSession,
  useTokens,
  useUpdateUser,
  useUsers,
  type ApiToken,
  type Role,
  type Session,
  type User,
} from "../../api/auth";
import { isApiError } from "../../api/client";
import { ApiErrorState } from "../../components/ApiErrorState";
import { Button } from "../../components/Button";
import { EmptyState } from "../../components/EmptyState";
import { Icon } from "../../components/Icon";
import { Panel } from "../../components/Panel";
import { Skeleton } from "../../components/Skeleton";
import { formatDatePl } from "../../lib/evidence";
import { CopyButton } from "../integrations/console/CodeBlock";
import { PasswordChangeForm } from "../auth/PasswordChangeForm";
import { inputClass } from "../auth/AuthLayout";

const fieldClass = inputClass.replace("h-10", "h-9");

function errText(err: unknown, fallback: string): string {
  const code = apiErrorCode(err);
  if (code === "exists") return "Użytkownik o tej nazwie już istnieje.";
  if (code === "last_admin") return "Nie można odebrać uprawnień ani wyłączyć ostatniego administratora.";
  if (code === "self_disable") return "Nie możesz wyłączyć własnego konta.";
  if (code === "forbidden") return "Brak uprawnień do tej operacji.";
  return isApiError(err) ? err.message : fallback;
}

function when(v: string | null | undefined, empty = "nigdy"): string {
  return v ? formatDatePl(v) : empty;
}

/** Secret shown once: copy box + warning; closing it drops the value from memory. */
export function SecretOnce({ title, secret, note, onClose, testId }: { title: string; secret: string; note: ReactNode; onClose: () => void; testId?: string }) {
  return (
    <div role="status" className="rounded-xl border border-warn/40 bg-warn/10 p-3 sm:p-4" data-testid={testId}>
      <p className="flex items-center gap-2 text-sm font-semibold text-fg"><Icon name="key" size={16} className="text-warn" />{title}</p>
      <p className="mt-1 text-xs text-muted">{note}</p>
      <div className="mt-2 flex items-start gap-2">
        <code className="min-w-0 flex-1 rounded-lg border border-line bg-panel px-2.5 py-1.5 font-mono text-sm break-all text-fg select-all" data-testid="secret-value">{secret}</code>
        <CopyButton text={secret} className="mt-1" />
      </div>
      <div className="mt-2 flex justify-end">
        <Button size="sm" onClick={onClose}>Zapisałem, ukryj</Button>
      </div>
    </div>
  );
}

function PageWrap({ children }: { children: ReactNode }) {
  return <div className="mx-auto flex w-full max-w-5xl flex-col gap-3 p-3 sm:p-4">{children}</div>;
}

function NoAuth() {
  return (
    <PageWrap>
      <EmptyState title="Logowanie jest wyłączone" description="Ten serwer działa bez logowania (HYDRA_AUTH=0). Konta, hasła i tokeny API są dostępne po włączeniu logowania." />
    </PageWrap>
  );
}

// ---------------------------------------------------------------------------
// /account/password
// ---------------------------------------------------------------------------

export function AccountPasswordPage() {
  const session = useSession();
  const user = useCurrentUser();
  const client = useQueryClient();
  const [done, setDone] = useState(false);
  if (session.isPending) return <PageWrap><Skeleton label="Ładowanie…" lines={4} /></PageWrap>;
  if (!user) return <NoAuth />;
  return (
    <PageWrap>
      <Panel title="Zmień hasło" className="max-w-xl">
        {done && (
          <p role="status" className="mb-4 flex items-center gap-2 rounded-xl border border-ok/40 bg-ok/10 px-3 py-2 text-sm text-ok" data-testid="password-done">
            <Icon name="check" size={16} /> Hasło zostało zmienione. Inne sesje tego konta mogą wymagać ponownego logowania.
          </p>
        )}
        <PasswordChangeForm
          key={done ? "done" : "form"}
          user={user}
          onDone={(u) => {
            client.setQueryData<Session>(sessionKey, (old) => (old ? { ...old, user: u } : old));
            setDone(true);
          }}
        />
      </Panel>
    </PageWrap>
  );
}

// ---------------------------------------------------------------------------
// /account/tokens
// ---------------------------------------------------------------------------

function tokenState(t: ApiToken): { label: string; cls: string } {
  if (t.revoked_at) return { label: "unieważniony", cls: "text-subtle" };
  if (t.expires_at && Date.parse(t.expires_at) < Date.now()) return { label: "wygasł", cls: "text-warn" };
  return { label: "aktywny", cls: "text-ok" };
}

export function TokensPage() {
  const session = useSession();
  const user = useCurrentUser();
  const isAdmin = user?.role === "admin";
  const [all, setAll] = useState(false);
  const tokens = useTokens(isAdmin && all);
  const create = useCreateToken();
  const revoke = useRevokeToken();
  const [name, setName] = useState("");
  const [days, setDays] = useState("90");
  const [secret, setSecret] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<number | null>(null);
  if (session.isPending) return <PageWrap><Skeleton label="Ładowanie…" lines={4} /></PageWrap>;
  if (!user) return <NoAuth />;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    const d = days.trim() === "" ? null : Number(days);
    create.mutate(
      { name: name.trim(), expires_days: d && Number.isFinite(d) && d > 0 ? Math.round(d) : null },
      { onSuccess: (r) => { setSecret(r.token); setName(""); } },
    );
  };
  const items = tokens.data?.items ?? [];

  return (
    <PageWrap>
      <Panel title="Nowy token API">
        <p className="mb-3 text-xs text-muted">
          Token zastępuje sesję przeglądarki w skryptach i integracjach: nagłówek <code className="font-mono text-fg">Authorization: Bearer &lt;token&gt;</code>. Ma te same uprawnienia co Twoje konto ({ROLE_LABEL[user.role]}).
        </p>
        <form onSubmit={submit} className="flex flex-col gap-3 sm:flex-row sm:items-end" data-testid="token-form">
          <div className="min-w-0 flex-1">
            <label htmlFor="token-name" className="mb-1 block text-xs font-medium text-muted">Nazwa (do czego służy)</label>
            <input id="token-name" value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} placeholder="np. skrypt eksportu SOC" className={fieldClass} autoComplete="off" />
          </div>
          <div className="sm:w-40">
            <label htmlFor="token-days" className="mb-1 block text-xs font-medium text-muted">Ważność (dni, puste = bez limitu)</label>
            <input id="token-days" type="number" min={1} max={3650} value={days} onChange={(e) => setDays(e.target.value)} className={fieldClass} />
          </div>
          <Button type="submit" variant="primary" disabled={create.isPending || !name.trim()} className="h-9 px-4">
            {create.isPending ? "Tworzenie…" : "Utwórz token"}
          </Button>
        </form>
        {create.isError && <p role="alert" className="mt-2 text-sm text-err">{errText(create.error, "Nie udało się utworzyć tokenu.")}</p>}
        {secret && (
          <div className="mt-3">
            <SecretOnce
              title="Nowy token API"
              secret={secret}
              testId="token-secret"
              note="Skopiuj token teraz. Serwer przechowuje tylko jego skrót, więc po zamknięciu tego okna nie da się go ponownie wyświetlić."
              onClose={() => setSecret(null)}
            />
          </div>
        )}
      </Panel>
      <Panel
        title={isAdmin && all ? "Wszystkie tokeny" : "Moje tokeny"}
        flush
        actions={isAdmin ? (
          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} className="size-4 accent-[var(--color-accent)]" />
            pokaż tokeny wszystkich użytkowników
          </label>
        ) : undefined}
      >
        {tokens.isPending ? <Skeleton label="Ładowanie tokenów…" lines={3} className="p-4" />
          : tokens.isError ? <div className="p-4"><ApiErrorState error={tokens.error} resource="tokeny" endpoint="GET /v1/auth/tokens" compact /></div>
          : items.length === 0 ? <EmptyState compact title="Brak tokenów" description="Utwórz token, aby wywoływać API ze skryptów bez logowania w przeglądarce." />
          : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[40rem] text-sm" data-testid="tokens-table">
                <thead className="text-left text-xs text-subtle">
                  <tr className="border-b border-line">
                    <th scope="col" className="px-4 py-2 font-medium">Nazwa</th>
                    <th scope="col" className="px-2 py-2 font-medium">Prefiks</th>
                    {isAdmin && all && <th scope="col" className="px-2 py-2 font-medium">Właściciel</th>}
                    <th scope="col" className="px-2 py-2 font-medium">Utworzony</th>
                    <th scope="col" className="px-2 py-2 font-medium">Ostatnie użycie</th>
                    <th scope="col" className="px-2 py-2 font-medium">Wygasa</th>
                    <th scope="col" className="px-2 py-2 font-medium">Stan</th>
                    <th scope="col" className="px-4 py-2"><span className="sr-only">Akcje</span></th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((t) => {
                    const st = tokenState(t);
                    return (
                      <tr key={t.id} className="border-b border-line/70 last:border-b-0">
                        <td className="px-4 py-2 text-fg">{t.name}</td>
                        <td className="px-2 py-2 font-mono text-xs text-muted">{t.prefix}…</td>
                        {isAdmin && all && <td className="px-2 py-2 text-muted">{t.username}</td>}
                        <td className="px-2 py-2 text-xs text-muted">{when(t.created_at)}</td>
                        <td className="px-2 py-2 text-xs text-muted">{when(t.last_used_at)}</td>
                        <td className="px-2 py-2 text-xs text-muted">{when(t.expires_at, "bez limitu")}</td>
                        <td className={`px-2 py-2 text-xs font-medium ${st.cls}`}>{st.label}</td>
                        <td className="px-4 py-2 text-right whitespace-nowrap">
                          {!t.revoked_at && (confirm === t.id ? (
                            <span className="inline-flex items-center gap-1.5">
                              <span className="text-xs text-muted">Unieważnić?</span>
                              <Button size="sm" variant="danger" disabled={revoke.isPending} onClick={() => revoke.mutate(t.id, { onSettled: () => setConfirm(null) })}>Tak, unieważnij</Button>
                              <Button size="sm" variant="ghost" onClick={() => setConfirm(null)}>Anuluj</Button>
                            </span>
                          ) : (
                            <Button size="sm" variant="ghost" onClick={() => setConfirm(t.id)} aria-label={`Unieważnij token ${t.name}`}>Unieważnij</Button>
                          ))}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        {revoke.isError && <p role="alert" className="px-4 pb-3 text-sm text-err">{errText(revoke.error, "Nie udało się unieważnić tokenu.")}</p>}
      </Panel>
    </PageWrap>
  );
}

// ---------------------------------------------------------------------------
// /admin/users
// ---------------------------------------------------------------------------

function userStatus(u: User): { label: string; cls: string } {
  if (u.disabled) return { label: "wyłączony", cls: "text-subtle" };
  if (u.locked_until && Date.parse(u.locked_until) > Date.now()) return { label: `zablokowany do ${formatDatePl(u.locked_until)}`, cls: "text-err" };
  if (u.must_change_password) return { label: "hasło tymczasowe", cls: "text-warn" };
  return { label: "aktywny", cls: "text-ok" };
}

export function UsersPage() {
  const session = useSession();
  const me = useCurrentUser();
  const isAdmin = me?.role === "admin";
  const users = useUsers(isAdmin);
  const createUser = useCreateUser();
  const update = useUpdateUser();
  const reset = useResetPassword();
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<Role>("analyst");
  const [secret, setSecret] = useState<{ username: string; password: string; kind: "new" | "reset" } | null>(null);
  const [confirmReset, setConfirmReset] = useState<number | null>(null);
  if (session.isPending) return <PageWrap><Skeleton label="Ładowanie…" lines={4} /></PageWrap>;
  if (!me) return <NoAuth />;
  if (!isAdmin) {
    return <PageWrap><EmptyState tone="error" title="Brak uprawnień" description="Zarządzanie użytkownikami jest dostępne tylko dla roli Administrator." /></PageWrap>;
  }

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!username.trim()) return;
    createUser.mutate({ username: username.trim(), role }, {
      onSuccess: (r) => { setSecret({ username: r.user.username, password: r.password, kind: "new" }); setUsername(""); },
    });
  };
  const items = users.data?.items ?? [];
  const actionError = update.error ?? reset.error;

  return (
    <PageWrap>
      <Panel title="Nowy użytkownik">
        <form onSubmit={submit} className="flex flex-col gap-3 sm:flex-row sm:items-end" data-testid="user-form">
          <div className="min-w-0 flex-1">
            <label htmlFor="new-username" className="mb-1 block text-xs font-medium text-muted">Login (3–64 znaki: litery, cyfry, . _ -)</label>
            <input id="new-username" value={username} onChange={(e) => setUsername(e.target.value)} required minLength={3} maxLength={64} pattern="[A-Za-z0-9._\-]+" autoComplete="off" spellCheck={false} className={fieldClass} data-testid="new-username" />
          </div>
          <div className="sm:w-44">
            <label htmlFor="new-role" className="mb-1 block text-xs font-medium text-muted">Rola</label>
            <select id="new-role" value={role} onChange={(e) => setRole(e.target.value as Role)} className={fieldClass} data-testid="new-role">
              {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
            </select>
          </div>
          <Button type="submit" variant="primary" disabled={createUser.isPending || !username.trim()} className="h-9 px-4" data-testid="create-user">
            {createUser.isPending ? "Tworzenie…" : "Utwórz konto"}
          </Button>
        </form>
        <p className="mt-2 text-xs text-subtle">Serwer generuje hasło tymczasowe. Przy pierwszym logowaniu użytkownik musi ustawić własne. Analityk: praca z dowodami i decyzje. Obserwator: tylko odczyt.</p>
        {createUser.isError && <p role="alert" className="mt-2 text-sm text-err">{errText(createUser.error, "Nie udało się utworzyć konta.")}</p>}
        {secret && (
          <div className="mt-3">
            <SecretOnce
              title={secret.kind === "new" ? `Hasło tymczasowe dla ${secret.username}` : `Nowe hasło tymczasowe dla ${secret.username}`}
              secret={secret.password}
              testId="user-secret"
              note="Przekaż hasło użytkownikowi bezpiecznym kanałem. Jest wyświetlane tylko raz; przy logowaniu system wymusi jego zmianę."
              onClose={() => setSecret(null)}
            />
          </div>
        )}
      </Panel>
      <Panel title="Użytkownicy" flush actions={users.data ? <span className="text-xs text-subtle">kont: {items.length}</span> : undefined}>
        {users.isPending ? <Skeleton label="Ładowanie użytkowników…" lines={4} className="p-4" />
          : users.isError ? <div className="p-4"><ApiErrorState error={users.error} resource="użytkownicy" endpoint="GET /v1/auth/users" compact /></div>
          : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[46rem] text-sm" data-testid="users-table">
                <thead className="text-left text-xs text-subtle">
                  <tr className="border-b border-line">
                    <th scope="col" className="px-4 py-2 font-medium">Login</th>
                    <th scope="col" className="px-2 py-2 font-medium">Rola</th>
                    <th scope="col" className="px-2 py-2 font-medium">Ostatnie logowanie</th>
                    <th scope="col" className="px-2 py-2 font-medium">Stan</th>
                    <th scope="col" className="px-4 py-2 text-right font-medium">Akcje</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((u) => {
                    const st = userStatus(u);
                    const self = u.id === me.id;
                    return (
                      <tr key={u.id} className="border-b border-line/70 last:border-b-0" data-testid={`user-row-${u.username}`}>
                        <td className="px-4 py-2">
                          <span className="font-medium text-fg">{u.username}</span>
                          {self && <span className="ml-2 text-xs text-subtle">(Ty)</span>}
                          {u.failed_attempts > 0 && <span className="block text-2xs text-subtle">nieudane próby: {u.failed_attempts}</span>}
                        </td>
                        <td className="px-2 py-2">
                          <label className="sr-only" htmlFor={`role-${u.id}`}>Rola użytkownika {u.username}</label>
                          <span className="flex items-center gap-2">
                            <select id={`role-${u.id}`} value={u.role} disabled={update.isPending} onChange={(e) => update.mutate({ id: u.id, role: e.target.value as Role })}
                              className="h-8 rounded-lg border border-line-strong bg-panel px-2 text-sm text-fg">
                              {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
                            </select>
                          </span>
                        </td>
                        <td className="px-2 py-2 text-xs text-muted">{when(u.last_login)}</td>
                        <td className={`px-2 py-2 text-xs font-medium ${st.cls}`}>{st.label}</td>
                        <td className="px-4 py-2 text-right whitespace-nowrap">
                          <span className="inline-flex items-center gap-1.5">
                            {confirmReset === u.id ? (
                              <>
                                <span className="text-xs text-muted">Nowe hasło?</span>
                                <Button size="sm" variant="danger" disabled={reset.isPending} onClick={() => reset.mutate(u.id, { onSuccess: (r) => setSecret({ username: r.user.username, password: r.password, kind: "reset" }), onSettled: () => setConfirmReset(null) })}>Tak, resetuj</Button>
                                <Button size="sm" variant="ghost" onClick={() => setConfirmReset(null)}>Anuluj</Button>
                              </>
                            ) : (
                              <Button size="sm" variant="ghost" onClick={() => setConfirmReset(u.id)} aria-label={`Resetuj hasło użytkownika ${u.username}`}>Resetuj hasło</Button>
                            )}
                            {!self && (
                              <Button size="sm" variant={u.disabled ? "secondary" : "ghost"} disabled={update.isPending} onClick={() => update.mutate({ id: u.id, disabled: !u.disabled })}
                                aria-label={`${u.disabled ? "Włącz" : "Wyłącz"} konto ${u.username}`}>
                                {u.disabled ? "Włącz" : "Wyłącz"}
                              </Button>
                            )}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        {actionError && <p role="alert" className="px-4 pb-3 text-sm text-err">{errText(actionError, "Operacja nie powiodła się.")}</p>}
      </Panel>
    </PageWrap>
  );
}
