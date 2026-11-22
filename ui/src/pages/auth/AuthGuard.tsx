/**
 * Route guard around AppShell. Reads GET /v1/auth/session once (react-query cache):
 * - auth disabled (or a backend/test without the endpoint): render the app as before;
 * - not signed in: /login?next=<current path + search>;
 * - temporary password: /login/change-password?next=...;
 * and re-checks the session whenever any fetch reports 401 or `password_change_required` (api/auth.ts events), so an
 * expired session lands on the login page with the page preserved. A 401 seen while this guard held a signed-in
 * session adds `reason=expired`, so the login page can say "Sesja wygasła" (a deliberate logout sends no 401).
 */
import { useEffect, useRef, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Navigate, useLocation } from "react-router";
import { DISABLED_SESSION, loginPath, PASSWORD_CHANGE_EVENT, sessionKey, UNAUTHORIZED_EVENT, useSession } from "../../api/auth";

export function AuthGuard({ children }: { children: ReactNode }) {
  const session = useSession();
  const client = useQueryClient();
  const { pathname, search } = useLocation();
  const signedIn = useRef(false);
  const expired = useRef(false);
  signedIn.current = Boolean(session.data?.authenticated);

  useEffect(() => {
    const recheck = () => void client.invalidateQueries({ queryKey: sessionKey }, { cancelRefetch: false });
    const unauthorized = () => {
      if (signedIn.current) expired.current = true;
      recheck();
    };
    window.addEventListener(UNAUTHORIZED_EVENT, unauthorized);
    window.addEventListener(PASSWORD_CHANGE_EVENT, recheck);
    return () => {
      window.removeEventListener(UNAUTHORIZED_EVENT, unauthorized);
      window.removeEventListener(PASSWORD_CHANGE_EVENT, recheck);
    };
  }, [client]);

  if (session.isPending) {
    return (
      <div role="status" aria-live="polite" className="flex h-full items-center justify-center bg-canvas text-sm text-muted" data-testid="auth-checking">
        <span aria-hidden="true" className="mr-2 size-4 rounded-full border-2 border-accent border-r-transparent motion-safe:animate-spin" />
        Sprawdzanie sesji…
      </div>
    );
  }
  const s = session.data ?? DISABLED_SESSION;
  if (!s.auth_enabled) return <>{children}</>;
  const here = `${pathname}${search}`;
  if (!s.authenticated || !s.user) {
    const to = loginPath(here);
    return <Navigate to={expired.current ? `${to}${to.includes("?") ? "&" : "?"}reason=expired` : to} replace />;
  }
  if (s.user.must_change_password) {
    return <Navigate to={`/login/change-password${here === "/" ? "" : `?next=${encodeURIComponent(here)}`}`} replace />;
  }
  return <>{children}</>;
}
