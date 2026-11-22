/**
 * Change password (forced after a temporary password, and /account/password): current, new, repeat, a segmented
 * local strength meter (lib/passwordStrength.ts, colour + text) and a requirement checklist that ticks live, mirroring
 * the server policy (>= 12 characters, not containing the username, >= 5 distinct characters, different from the
 * current one) plus the matching repeat. The server answer decides; its Polish message is shown as is.
 */
import { useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams, Navigate } from "react-router";
import { apiErrorCode, changePassword, safeNext, sessionKey, useSession, type Session, type User } from "../../api/auth";
import { isApiError } from "../../api/client";
import { Icon } from "../../components/Icon";
import { cx } from "../../lib/cx";
import { estimateStrength, MIN_PASSWORD_LENGTH } from "../../lib/passwordStrength";
import { AuthLayout, AuthTitle, NoticeCard } from "./AuthLayout";
import { PasswordField, SubmitButton } from "./LoginPage";

const SEG = ["bg-err", "bg-err", "bg-warn", "bg-ok", "bg-ok"];
const TEXT = ["text-err", "text-err", "text-warn", "text-ok", "text-ok"];
const SEGMENTS = 4;

export function StrengthMeter({ password, username }: { password: string; username: string }) {
  const s = estimateStrength(password, username);
  const empty = password.length === 0;
  const lit = empty ? 0 : Math.max(1, s.score);
  return (
    <div className="mt-2.5 px-0.5" data-testid="strength-meter" data-score={empty ? "" : s.score}>
      <div className="flex gap-1.5" aria-hidden="true">
        {Array.from({ length: SEGMENTS }, (_, i) => (
          <span key={i} className={cx("h-1 flex-1 rounded-full transition-colors", i < lit ? SEG[s.score] : "login-seg-off")} />
        ))}
      </div>
      <p className="mt-1.5 flex flex-wrap items-baseline justify-between gap-x-3 text-xs" aria-live="polite">
        <span className={cx("font-semibold", empty ? "text-subtle" : TEXT[s.score])}>
          {empty ? "Siła hasła" : <>Siła hasła: {s.label}</>}
        </span>
        {!empty && s.hints[0] && s.score < 3 && <span className="text-muted">{s.hints[0]}</span>}
      </p>
    </div>
  );
}

export interface Requirement { id: string; label: string; met: boolean }

/** Live mirror of the server policy; `met` is false while the relevant field is still empty. */
export function passwordRequirements(current: string, next: string, repeat: string, username: string): Requirement[] {
  const n = next.normalize("NFC");
  const user = username.trim().toLowerCase();
  const hasUser = user.length >= 3 && n.toLowerCase().includes(user);
  return [
    { id: "length", label: `Co najmniej ${MIN_PASSWORD_LENGTH} znaków`, met: [...n].length >= MIN_PASSWORD_LENGTH },
    { id: "username", label: "Nie zawiera loginu", met: n.length > 0 && !hasUser },
    { id: "distinct", label: "Co najmniej 5 różnych znaków", met: new Set([...n]).size >= 5 },
    { id: "current", label: "Inne niż obecne hasło", met: n.length > 0 && n !== current.normalize("NFC") },
    { id: "repeat", label: "Powtórzenie zgodne", met: repeat.length > 0 && repeat.normalize("NFC") === n },
  ];
}

export function RequirementList({ items }: { items: Requirement[] }) {
  return (
    <ul className="flex flex-col gap-1.5 py-1" aria-label="Wymagania dla nowego hasła" data-testid="pw-requirements">
      {items.map((r) => (
        <li key={r.id} className={cx("flex items-center gap-2.5 text-[13px]", r.met ? "" : "login-muted")} data-met={r.met} data-testid={`pw-req-${r.id}`}>
          <svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none" className={cx("shrink-0", r.met ? "text-ok" : "login-faint")}>
            {r.met
              ? <path d="M3 8.4l3.1 3.1L13 4.6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              : <circle cx="8" cy="8" r="2.2" fill="currentColor" />}
          </svg>
          <span>{r.label}<span className="sr-only">{r.met ? " — spełnione" : " — niespełnione"}</span></span>
        </li>
      ))}
    </ul>
  );
}

export function PasswordChangeForm({ user, onDone, submitLabel = "Zmień hasło" }: { user: User | null; onDone: (u: User) => void; submitLabel?: string }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const username = user?.username ?? "";
  const strength = estimateStrength(next, username);

  const local: string | null =
    next.length > 0 && next.length < MIN_PASSWORD_LENGTH ? `Nowe hasło musi mieć co najmniej ${MIN_PASSWORD_LENGTH} znaków.`
    : next && !strength.meetsPolicy ? "Nowe hasło nie może zawierać nazwy użytkownika."
    : next && next === current ? "Nowe hasło musi się różnić od obecnego."
    : repeat && repeat !== next ? "Powtórzone hasło jest inne niż nowe."
    : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (busy) return;
    if (!current || !next || !repeat) { setError("Wypełnij wszystkie trzy pola."); return; }
    if (local) { setError(local); return; }
    setBusy(true);
    setError(null);
    try {
      const r = await changePassword({ current_password: current, new_password: next });
      onDone(r.user);
    } catch (err) {
      const code = apiErrorCode(err);
      if (code === "invalid_credentials" || (isApiError(err) && err.status === 401)) setError("Obecne hasło jest nieprawidłowe.");
      else setError(isApiError(err) ? err.message : "Nie udało się zmienić hasła.");
    } finally {
      setBusy(false);
    }
  };

  const shown = error ?? (touched ? local : null) ?? (repeat && repeat !== next ? "Powtórzone hasło jest inne niż nowe." : null);

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-3" data-testid="password-form">
      {/* hidden username so password managers attach the new password to the right account */}
      <input type="text" name="username" autoComplete="username" value={username} readOnly hidden />
      <PasswordField id="pw-current" label="Obecne hasło" value={current} onChange={setCurrent} autoComplete="current-password" autoFocus testId="pw-current" showLabel />
      <div>
        <PasswordField id="pw-new" label="Nowe hasło" value={next} onChange={setNext} autoComplete="new-password" describedBy="pw-policy" testId="pw-new" showLabel />
        <span id="pw-policy" className="sr-only">Co najmniej {MIN_PASSWORD_LENGTH} znaków, bez nazwy użytkownika, inne niż obecne.</span>
        <StrengthMeter password={next} username={username} />
      </div>
      <PasswordField id="pw-repeat" label="Powtórz nowe hasło" value={repeat} onChange={setRepeat} autoComplete="new-password" invalid={Boolean(repeat && repeat !== next)} testId="pw-repeat" showLabel />
      <RequirementList items={passwordRequirements(current, next, repeat, username)} />
      <div aria-live="assertive" className="empty:hidden">
        {shown && (
          <NoticeCard role="alert" icon={<Icon name="alert" size={15} />} testId="password-error">{shown}</NoticeCard>
        )}
      </div>
      <SubmitButton busy={busy} label={submitLabel} busyLabel="Zapisywanie…" testId="password-submit" />
    </form>
  );
}

/** /login/change-password: the forced change after a temporary password; continues to `next`. */
export function ForcedPasswordPage() {
  const [params] = useSearchParams();
  const target = safeNext(params.get("next"));
  const session = useSession();
  const client = useQueryClient();
  const navigate = useNavigate();
  const s = session.data;
  if (session.isPending) return <AuthLayout testId="password-page"><p role="status" className="flex items-center gap-2 text-sm text-muted"><span aria-hidden="true" className="size-4 rounded-full border-2 border-accent border-r-transparent motion-safe:animate-spin" />Sprawdzanie sesji…</p></AuthLayout>;
  if (!s?.auth_enabled) return <Navigate to={target} replace />;
  if (!s.authenticated || !s.user) return <Navigate to={`/login${target === "/" ? "" : `?next=${encodeURIComponent(target)}`}`} replace />;
  const done = (user: User) => {
    client.setQueryData<Session>(sessionKey, (old) => ({ ...(old ?? s), user }));
    void client.invalidateQueries({ queryKey: sessionKey });
    navigate(target, { replace: true });
  };
  return (
    <AuthLayout testId="password-page">
      <AuthTitle large title="Ustaw nowe hasło">
        {s.user.must_change_password
          ? <>Konto <span className="font-semibold text-fg">{s.user.username}</span> ma hasło tymczasowe. Przed rozpoczęciem pracy ustaw własne hasło.</>
          : <>Zmiana hasła dla konta <span className="font-semibold text-fg">{s.user.username}</span>.</>}
      </AuthTitle>
      <PasswordChangeForm user={s.user} onDone={done} submitLabel="Ustaw hasło i przejdź dalej" />
    </AuthLayout>
  );
}
