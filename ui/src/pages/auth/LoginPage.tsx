/**
 * /login (flat design, UI-LOGIN v3): username + password with leading icons and plain placeholders (labels stay for
 * assistive tech), show/hide toggle, "Zapamiętaj mnie" (longer idle timeout only), Caps Lock pill, Polish messages in
 * a grey notice card (wrong credentials, lockout and rate limit with a live countdown, expired session after a 401
 * redirect: `?reason=expired` from AuthGuard), and a popover that tells where the generated admin password lives
 * (`credentials_hint` from GET /v1/auth/session). On success it goes to `next`, or to the forced password change when
 * the account has a temporary password. No token or SSO sign-in: /v1/auth/login takes username + password only.
 */
import { useEffect, useId, useRef, useState, type FormEvent, type InputHTMLAttributes, type KeyboardEvent, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { isApiError } from "../../api/client";
import { apiErrorCode, apiRetryAfter, login, safeNext, sessionKey, useSession, type CredentialsHint, type Session } from "../../api/auth";
import { Icon } from "../../components/Icon";
import { CopyButton } from "../integrations/console/CodeBlock";
import { cx } from "../../lib/cx";
import { AuthLayout, flatInputClass, formatCountdown, Glyph, NoticeCard, TypedHeadline } from "./AuthLayout";

type LoginError =
  | { kind: "invalid"; message: string }
  | { kind: "locked"; until: number; total: number }
  | { kind: "rate"; until: number; total: number }
  | { kind: "other"; message: string };

export const MSG_INVALID = "Nieprawidłowy login lub hasło.";

/** Seconds left until `until` (ms), ticking every second while positive. */
function useCountdown(until: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (until == null) return;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [until]);
  return until == null ? 0 : Math.max(0, Math.ceil((until - now) / 1000));
}

function toLoginError(err: unknown): LoginError {
  const code = apiErrorCode(err);
  const retry = apiRetryAfter(err);
  if (isApiError(err)) {
    if (err.status === 423 || code === "locked") { const t = retry ?? 300; return { kind: "locked", until: Date.now() + t * 1000, total: t }; }
    if (err.status === 429 || code === "rate_limited") { const t = retry ?? 60; return { kind: "rate", until: Date.now() + t * 1000, total: t }; }
    if (err.status === 401 || code === "invalid_credentials") return { kind: "invalid", message: MSG_INVALID };
    if (err.isOffline) return { kind: "other", message: "Brak połączenia z serwerem HYDRA. Sprawdź, czy usługa działa, i spróbuj ponownie." };
    return { kind: "other", message: err.message || "Logowanie nie powiodło się." };
  }
  return { kind: "other", message: "Logowanie nie powiodło się." };
}

/** "Jak znaleźć dane administratora?": file path, Docker command and env variable from the session endpoint. */
export function CredentialsHelp({ hint }: { hint: CredentialsHint | undefined }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const root = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); setOpen(false); btn.current?.focus(); }
    };
    const onDown = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onDown);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("pointerdown", onDown); };
  }, [open]);
  const rows: { label: string; value: string }[] = [];
  if (hint?.file) rows.push({ label: "Plik z hasłem (pierwsze uruchomienie)", value: hint.file });
  if (hint?.docker) rows.push({ label: "W kontenerze Docker", value: hint.docker });
  if (hint?.cli) rows.push({ label: "Reset hasła z wiersza poleceń", value: hint.cli });
  return (
    <div ref={root} className="relative">
      <button
        ref={btn}
        type="button"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-haspopup="dialog"
        onClick={() => setOpen((v) => !v)}
        className="login-link inline-flex items-center gap-1 rounded-md text-[13px] font-medium underline decoration-1 underline-offset-[3px] hover:decoration-2 focus-visible:outline-2 focus-visible:outline-offset-2"
        data-testid="credentials-help"
      >
        Jak znaleźć dane administratora?
      </button>
      {open && (
        <div
          id={id}
          role="dialog"
          aria-label="Dane administratora"
          className="absolute top-[calc(100%+10px)] right-0 z-30 w-[min(24rem,calc(100vw-2.5rem))] rounded-2xl border border-line bg-panel p-4 text-left shadow-float motion-safe:animate-[pop-in_160ms_var(--ease-out-soft)]"
          data-testid="credentials-popover"
        >
          <p className="text-sm font-semibold text-fg">Dane administratora</p>
          <p className="mt-1 text-xs text-muted">
            Przy pierwszym uruchomieniu HYDRA tworzy konto administratora z losowym hasłem i zapisuje je na serwerze. Po pierwszym logowaniu system poprosi o zmianę hasła.
          </p>
          {rows.length === 0 ? (
            <p className="mt-3 text-xs text-subtle">Serwer nie podał ścieżki do pliku. Zapytaj osobę, która uruchomiła HYDRA.</p>
          ) : (
            <dl className="mt-3 flex flex-col gap-2.5">
              {rows.map((r) => (
                <div key={r.label}>
                  <dt className="text-2xs font-medium text-subtle">{r.label}</dt>
                  <dd className="mt-1 flex items-start gap-2">
                    <code className="min-w-0 flex-1 rounded-md border border-line bg-raised px-2 py-1 font-mono text-xs break-all text-fg">{r.value}</code>
                    <CopyButton text={r.value} />
                  </dd>
                </div>
              ))}
            </dl>
          )}
          <p className="mt-3 text-xs text-muted">
            Hasło można też ustawić przed pierwszym startem zmienną środowiskową <code className="font-mono text-fg">{hint?.env ?? "HYDRA_ADMIN_PASSWORD"}</code>.
          </p>
        </div>
      )}
    </div>
  );
}

/** Flat field: visually hidden label (placeholder shows the same text), leading icon, optional trailing button. */
export function TextField({ id, label, icon, value, onChange, invalid, inputProps, trailing, testId, showLabel }: {
  id: string;
  label: string;
  /** Visible label above the field (forms with several password fields); otherwise sr-only + placeholder. */
  showLabel?: boolean;
  icon: ReactNode;
  value: string;
  onChange: (v: string) => void;
  invalid?: boolean;
  inputProps?: InputHTMLAttributes<HTMLInputElement>;
  trailing?: ReactNode;
  testId?: string;
}) {
  return (
    <div>
      <label htmlFor={id} className={showLabel ? "mb-1.5 block text-[13px] font-medium" : "sr-only"}>{label}</label>
      <div className="relative">
        <span aria-hidden="true" className="login-muted pointer-events-none absolute top-1/2 left-3.5 -translate-y-1/2">{icon}</span>
        <input
          id={id}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={showLabel ? undefined : label}
          aria-invalid={invalid ? true : undefined}
          className={cx(flatInputClass, trailing ? "pr-12" : undefined)}
          data-testid={testId}
          {...inputProps}
        />
        {trailing}
      </div>
    </div>
  );
}

export function PasswordField({ id, label, value, onChange, autoComplete, invalid, describedBy, autoFocus, onCaps, testId, showLabel }: {
  id: string;
  showLabel?: boolean;
  label: string;
  value: string;
  onChange: (v: string) => void;
  autoComplete: string;
  invalid?: boolean;
  describedBy?: string;
  autoFocus?: boolean;
  onCaps?: (on: boolean) => void;
  testId?: string;
}) {
  const [show, setShow] = useState(false);
  const caps = (e: KeyboardEvent<HTMLInputElement>) => onCaps?.(e.getModifierState?.("CapsLock") ?? false);
  return (
    <TextField
      id={id}
      label={label}
      icon={<Icon name="lock" size={17} />}
      value={value}
      onChange={onChange}
      invalid={invalid}
      testId={testId}
      showLabel={showLabel}
      inputProps={{
        type: show ? "text" : "password",
        onKeyDown: caps,
        onKeyUp: caps,
        onBlur: () => onCaps?.(false),
        autoComplete,
        autoCapitalize: "off",
        autoCorrect: "off",
        spellCheck: false,
        required: true,
        autoFocus,
        "aria-describedby": describedBy,
      }}
      trailing={
        <button
          type="button"
          onClick={() => setShow((v) => !v)}
          aria-pressed={show}
          aria-label={show ? "Ukryj hasło" : "Pokaż hasło"}
          aria-controls={id}
          title={show ? "Ukryj hasło" : "Pokaż hasło"}
          className="login-icon-btn absolute top-1/2 right-1.5 flex size-9 -translate-y-1/2 items-center justify-center rounded-lg focus-visible:outline-2 focus-visible:outline-offset-0"
        >
          <Icon name={show ? "eyeOff" : "eye"} size={17} />
        </button>
      }
    />
  );
}

/** Primary pill button (black in light, white in dark): spinner while busy, check after success. */
export function SubmitButton({ busy, done, disabled, label, busyLabel, doneLabel, testId }: {
  busy: boolean;
  done?: boolean;
  disabled?: boolean;
  label: string;
  busyLabel: string;
  doneLabel?: string;
  testId: string;
}) {
  return (
    <button
      type="submit"
      disabled={disabled || busy || done}
      aria-busy={busy}
      className={cx("login-primary inline-flex h-12 w-full items-center justify-center gap-2 rounded-full px-5 text-[15px] font-semibold focus-visible:outline-2 focus-visible:outline-offset-3 disabled:cursor-not-allowed", done && "login-primary-done")}
      data-testid={testId}
    >
      {busy && <span aria-hidden="true" className="size-4 rounded-full border-2 border-current border-r-transparent motion-safe:animate-spin" />}
      {done && (
        <svg aria-hidden="true" width="18" height="18" viewBox="0 0 20 20" fill="none">
          <path d="M5 10.5l3.2 3.2L15 6.8" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )}
      <span>{done ? doneLabel ?? label : busy ? busyLabel : label}</span>
    </button>
  );
}

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function LoginForm({ session, onSuccess, expired = false }: { session: Session | undefined; onSuccess: (s: Session) => void; expired?: boolean }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [capsLock, setCapsLock] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<LoginError | null>(null);
  const [tried, setTried] = useState(false);
  const until = error && (error.kind === "locked" || error.kind === "rate") ? error.until : null;
  const left = useCountdown(until);
  const blocked = until != null && left > 0;
  const passwordRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (until != null && left === 0) setError(null);
  }, [until, left]);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || blocked || done) return;
    setBusy(true);
    setTried(true);
    setError(null);
    try {
      const r = await login({ username: username.trim(), password, remember });
      const fresh: Session = { ...(session ?? { auth_enabled: true }), auth_enabled: true, authenticated: true, user: r.user, csrf_token: r.csrf_token, expires_at: r.expires_at, idle_expires_at: r.idle_expires_at };
      setBusy(false);
      setDone(true);
      timer.current = setTimeout(() => onSuccess(fresh), prefersReducedMotion() ? 0 : 300);
    } catch (err) {
      setError(toLoginError(err));
      setPassword("");
      passwordRef.current?.querySelector("input")?.focus();
      setBusy(false);
    }
  };

  let message: string | null = null;
  if (error?.kind === "invalid" || error?.kind === "other") message = error.message;
  else if (error?.kind === "locked") message = `Konto tymczasowo zablokowane. Spróbuj ponownie za ${formatCountdown(left)}`;
  else if (error?.kind === "rate") message = `Zbyt wiele prób logowania z tego adresu. Spróbuj ponownie za ${formatCountdown(left)}`;
  const noticeIcon = error?.kind === "locked" ? <Icon name="lock" size={15} /> : error?.kind === "rate" ? <Glyph name="timer" size={16} /> : <Icon name="alert" size={15} />;

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-3" aria-describedby={message ? "login-error" : undefined} data-testid="login-form">
      {expired && !tried && !message && (
        <div className="mb-2">
          <NoticeCard role="status" icon={<Glyph name="timer" size={16} />} title="Sesja wygasła — zaloguj się ponownie" testId="session-expired">
            Po zalogowaniu wrócisz do ostatnio otwartej strony.
          </NoticeCard>
        </div>
      )}
      <div aria-live="assertive" className="empty:hidden">
        {message && (
          <div className="mb-2">
            <NoticeCard role="alert" id="login-error" icon={noticeIcon} testId="login-error">{message}</NoticeCard>
          </div>
        )}
      </div>
      <TextField
        id="login-username"
        label="Login"
        icon={<Icon name="user" size={17} />}
        value={username}
        onChange={setUsername}
        invalid={error?.kind === "invalid"}
        testId="login-username"
        inputProps={{ name: "username", autoComplete: "username", autoCapitalize: "off", autoCorrect: "off", spellCheck: false, required: true, autoFocus: true }}
      />
      <div ref={passwordRef}>
        <PasswordField
          id="login-password"
          label="Hasło"
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
          invalid={error?.kind === "invalid"}
          describedBy={capsLock ? "login-caps" : undefined}
          onCaps={setCapsLock}
          testId="login-password"
        />
        {capsLock && (
          <p id="login-caps" role="status" className="login-caps mt-2 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold" data-testid="caps-lock">
            <Glyph name="caps" size={14} /> Włączony Caps Lock
          </p>
        )}
      </div>
      <div className="mt-1 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <label className="inline-flex cursor-pointer items-center gap-2 text-[13px]" title="Wydłuża tylko czas bezczynności przed wylogowaniem. Nie zapisuje hasła.">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} aria-describedby="login-remember-hint" className="login-check size-4 shrink-0 rounded" data-testid="login-remember" />
          Zapamiętaj mnie
          <span id="login-remember-hint" className="sr-only">Wydłuża tylko czas bezczynności przed wylogowaniem. Nie zapisuje hasła.</span>
        </label>
        <CredentialsHelp hint={session?.credentials_hint} />
      </div>
      <div className="mt-3">
        <SubmitButton busy={busy} done={done} disabled={blocked} label="Zaloguj się" busyLabel="Logowanie…" doneLabel="Zalogowano" testId="login-submit" />
      </div>
    </form>
  );
}

export function LoginPage() {
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));
  const expired = params.get("reason") === "expired";
  const session = useSession();
  const client = useQueryClient();
  const navigate = useNavigate();
  const s = session.data;
  if (s && !s.auth_enabled && session.isFetched) return <Navigate to={next} replace />;
  if (s?.authenticated && s.user) {
    if (s.user.must_change_password) return <Navigate to={`/login/change-password?next=${encodeURIComponent(next)}`} replace />;
    return <Navigate to={next} replace />;
  }
  const onSuccess = (fresh: Session) => {
    client.setQueryData(sessionKey, fresh);
    void client.invalidateQueries({ queryKey: sessionKey });
    if (fresh.user?.must_change_password) navigate(`/login/change-password?next=${encodeURIComponent(next)}`, { replace: true });
    else navigate(next, { replace: true });
  };
  return (
    <AuthLayout testId="login-page">
      <TypedHeadline />
      <h1 className="mt-8 mb-4 text-[15px] font-semibold">Zaloguj się do panelu</h1>
      <LoginForm session={s} onSuccess={onSuccess} expired={expired} />
    </AuthLayout>
  );
}
