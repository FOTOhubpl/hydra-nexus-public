/**
 * User menu in the floating top bar: name, role pill, "Zmień hasło", "Tokeny API", "Użytkownicy" (admin only),
 * "Wyloguj". Rendered only when the panel login is enabled and somebody is signed in.
 */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router";
import { logout, ROLE_LABEL, sessionKey, useCurrentUser, type Role, type Session } from "../api/auth";
import { cx } from "../lib/cx";
import { Icon } from "./Icon";
import { Popover, PopoverHeader } from "./Popover";

const ROLE_PILL: Record<Role, string> = {
  admin: "border-accent/40 bg-accent-soft text-accent",
  analyst: "border-ok/40 bg-ok/10 text-ok",
  viewer: "border-line-strong bg-hover text-muted",
};

export function RolePill({ role, className }: { role: Role; className?: string }) {
  return (
    <span className={cx("inline-flex h-5 items-center rounded-full border px-2 text-2xs font-medium whitespace-nowrap", ROLE_PILL[role] ?? ROLE_PILL.viewer, className)} data-testid="role-pill">
      {ROLE_LABEL[role] ?? role}
    </span>
  );
}

function initials(name: string): string {
  const parts = name.split(/[\s._-]+/).filter(Boolean);
  const s = parts.length >= 2 ? `${parts[0]![0]}${parts[1]![0]}` : name.slice(0, 2);
  return s.toUpperCase();
}

const itemClass = "flex min-h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-left text-sm text-fg transition-colors hover:bg-hover";

export function UserMenu() {
  const user = useCurrentUser();
  const client = useQueryClient();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  if (!user) return null;
  const signOut = async () => {
    setBusy(true);
    try {
      await logout();
    } catch {
      /* the session may already be gone; the cookies are cleared by the server when it answers */
    }
    client.setQueryData<Session>(sessionKey, (old) => ({ ...(old ?? { auth_enabled: true }), authenticated: false, user: undefined, csrf_token: undefined }));
    navigate("/login", { replace: true });
    // drop cached data of the signed-out user (keep the session entry the login page reads)
    client.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
    setBusy(false);
  };
  return (
    <Popover
      label="Konto użytkownika"
      tip={`${user.username} · ${ROLE_LABEL[user.role] ?? user.role}`}
      testId="user-menu"
      trigger={
        <span aria-hidden="true" className="flex size-7 items-center justify-center rounded-full bg-fg text-2xs font-semibold text-canvas">{initials(user.username)}</span>
      }
      panelClassName="w-[min(17rem,calc(100vw-1.5rem))]"
    >
      {(close) => (
        <>
          <PopoverHeader
            title={user.username}
            sub={<span className="mt-1 flex items-center gap-2"><RolePill role={user.role} /><span>zalogowano</span></span>}
          />
          <ul className="flex flex-col gap-0.5 p-1.5" aria-label="Konto">
            <li>
              <Link to="/account/password" onClick={close} className={itemClass}>
                <Icon name="lock" size={16} className="text-muted" /> Zmień hasło
              </Link>
            </li>
            <li>
              <Link to="/account/tokens" onClick={close} className={itemClass}>
                <Icon name="key" size={16} className="text-muted" /> Tokeny API
              </Link>
            </li>
            {user.role === "admin" && (
              <li>
                <Link to="/admin/users" onClick={close} className={itemClass}>
                  <Icon name="users" size={16} className="text-muted" /> Użytkownicy
                </Link>
              </li>
            )}
          </ul>
          <div className="border-t border-line p-1.5">
            <button type="button" onClick={() => void signOut()} disabled={busy} className={cx(itemClass, "text-err hover:bg-err/10")} data-testid="logout">
              <Icon name="logout" size={16} /> {busy ? "Wylogowywanie…" : "Wyloguj"}
            </button>
          </div>
        </>
      )}
    </Popover>
  );
}
