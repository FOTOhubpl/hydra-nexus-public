/**
 * Flat split layout of the login and forced-password screens (outside AppShell). Left (>= 900 px, about 57 %): one
 * rounded card filled with the generated scene carousel (LoginArtwork.tsx). Right: a warm off-white (light) or
 * near-black (dark) column, ~380 px wide, with the wordmark, the screen content and a small footer. Below 900 px the
 * scene card becomes a short banner on top. The form comes first in the DOM, so keyboard and screen-reader order
 * start at the form; CSS order puts the visual on the left / top.
 */
import { useEffect, useState, type ReactNode } from "react";
import { useHealth } from "../../api/client";
import { Icon } from "../../components/Icon";
import { cx } from "../../lib/cx";
import { toggleTheme, useTheme } from "../../lib/theme";
import { SceneCarousel } from "./LoginArtwork";

export const TAGLINE = "Łączymy ślady kampanii oszustw w jeden obraz";

/** Plain input style shared with the account pages (kept for AccountPages.tsx). */
export const inputClass =
  "h-10 w-full rounded-lg border border-line-strong bg-panel px-3 text-base text-fg placeholder:text-subtle transition-colors hover:border-subtle focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-accent aria-[invalid=true]:border-err";

/** Flat auth input: 1 px light border, 12 px radius, leading icon space, plain placeholder. */
export const flatInputClass =
  "login-input h-12 w-full rounded-xl border pl-11 pr-3 text-[16px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-1 min-[900px]:text-[15px]";

/** Small inline glyphs used only on the auth screens (Icon.tsx is shared; these stay local). */
const GLYPH: Record<string, string> = {
  caps: "M10 3.5l6 6.5h-3.2v3.5H7.2V10H4zM7.2 16.5h5.6",
  loop: "M6 7a4 4 0 100 6h8a4 4 0 100-6H6z",
  timer: "M10 11m-6 0a6 6 0 1012 0a6 6 0 10-12 0M10 8v3l2 1.5M8 2.5h4",
};

export function Glyph({ name, size = 18, className }: { name: keyof typeof GLYPH; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className}>
      <path d={GLYPH[name]} />
    </svg>
  );
}

/** Soft grey notice card with the icon in a red circle: expired session, errors, lockout countdown. */
export function NoticeCard({ icon, title, children, role, id, testId }: {
  icon: ReactNode;
  title?: string;
  children: ReactNode;
  role?: "alert" | "status";
  id?: string;
  testId?: string;
}) {
  return (
    <div id={id} role={role} className="login-notice flex items-start gap-3 rounded-2xl px-4 py-3.5" data-testid={testId}>
      <span aria-hidden="true" className="login-notice-icon mt-px flex size-8 shrink-0 items-center justify-center rounded-full">{icon}</span>
      <span className="min-w-0 pt-1 text-[14px] leading-snug">
        {title && <span className="block font-semibold">{title}</span>}
        <span className={cx("block tabular-nums", title ? "login-muted mt-0.5" : "font-medium")}>{children}</span>
      </span>
    </div>
  );
}

// No "deepfake'i": HYDRA has manipulation-suspicion signals only, not validated deepfake detection (KNOWN_LIMITATIONS L32).
const WORDS = ["fałszywe reklamy", "kampanie oszustw", "manipulacje", "operatorów"];

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** "Śledź [word]. / Z dowodem." with a typewriter word and a hand-drawn underline; static under reduced motion. */
export function TypedHeadline() {
  const [reduced] = useState(prefersReducedMotion);
  const [word, setWord] = useState(0);
  const [len, setLen] = useState(reduced ? WORDS[1]!.length : 0);
  const [deleting, setDeleting] = useState(false);
  const current = reduced ? WORDS[1]! : WORDS[word]!;
  const complete = len >= current.length;
  useEffect(() => {
    if (reduced) return;
    const delay = deleting ? 32 : complete ? 2200 : 70;
    const t = setTimeout(() => {
      if (!deleting && complete) setDeleting(true);
      else if (deleting && len === 0) { setDeleting(false); setWord((w) => (w + 1) % WORDS.length); }
      else setLen((n) => n + (deleting ? -1 : 1));
    }, delay);
    return () => clearTimeout(t);
  }, [reduced, deleting, complete, len]);
  return (
    <p className="login-headline text-[clamp(1.6rem,7.4vw,2.05rem)] leading-[1.18] font-normal tracking-[-0.025em] min-[900px]:text-[2.05rem]" data-testid="login-headline">
      <span className="sr-only">Śledź fałszywe reklamy, kampanie oszustw, manipulacje i operatorów. Z dowodem.</span>
      <span aria-hidden="true" className="block whitespace-nowrap">
        Śledź{" "}
        <span className="relative inline-block">
          {current.slice(0, len)}
          {!reduced && <span className="login-caret ml-px inline-block h-[0.9em] w-[2px] translate-y-[0.1em] bg-current" />}
          <svg viewBox="0 0 300 14" preserveAspectRatio="none" className={cx("login-underline pointer-events-none absolute -bottom-1.5 left-0 h-3 w-full", complete && !deleting && "login-underline-on")}>
            <path d="M3 9C50 4 95 3 150 6S238 11 297 4" fill="none" stroke="var(--lg-red)" strokeWidth="3.2" strokeLinecap="round" vectorEffect="non-scaling-stroke" pathLength={1} />
          </svg>
        </span>
        .
      </span>
      <span aria-hidden="true" className="block">Z dowodem.</span>
    </p>
  );
}

function isLoopbackHost(host: string): boolean {
  return host === "127.0.0.1" || host === "localhost" || host === "::1" || host === "[::1]" || host.endsWith(".localhost");
}

/** Version from GET /health (pre-login body: status + version), reachability only when down, and the transport. */
function AuthFooter() {
  const health = useHealth(15000);
  const host = typeof window === "undefined" ? "" : window.location.hostname;
  const h = health.data;
  return (
    <div className="login-muted mt-10 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs" data-testid="auth-footer">
      {h?.version && <span className="tabular-nums">HYDRA wersja {h.version}</span>}
      {h?.version && <span aria-hidden="true">·</span>}
      <span>{isLoopbackHost(host) ? "Połączenie lokalne (loopback)" : `Połączenie z ${host}`}</span>
      {health.isError && !h && (
        <>
          <span aria-hidden="true">·</span>
          <span className="inline-flex items-center gap-1.5" data-testid="auth-server-status"><span aria-hidden="true" className="size-1.5 rounded-full bg-err" />Brak połączenia z serwerem</span>
        </>
      )}
    </div>
  );
}

function ThemePill() {
  const theme = useTheme();
  const label = theme === "dark" ? "Tryb jasny" : "Tryb ciemny";
  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={label}
      className="login-pill inline-flex h-8 shrink-0 items-center gap-1.5 rounded-full px-3 text-xs font-medium focus-visible:outline-2 focus-visible:outline-offset-2 min-[900px]:absolute min-[900px]:top-6 min-[900px]:right-6"
    >
      <Icon name={theme === "dark" ? "sun" : "moon"} size={14} />
      {label}
    </button>
  );
}

export function AuthLayout({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <div className="login-root flex min-h-full flex-col min-[900px]:grid min-[900px]:h-full min-[900px]:grid-cols-[minmax(0,1.32fr)_minmax(0,1fr)]" data-testid={testId}>
      <div className="relative order-2 flex flex-1 flex-col px-5 pt-6 pb-8 min-[480px]:px-8 min-[900px]:col-start-2 min-[900px]:row-start-1 min-[900px]:justify-center min-[900px]:overflow-y-auto min-[900px]:py-14">
        <main className="mx-auto w-full max-w-[380px]">
          <div className="flex items-center justify-between gap-3">
            <p className="text-[19px] font-extrabold tracking-[-0.035em]">HYDRA NEXUS</p>
            <ThemePill />
          </div>
          <div data-testid="login-card" className="mt-8 min-[900px]:mt-12">
            {children}
          </div>
          <AuthFooter />
        </main>
      </div>
      <SceneCarousel className="order-1 m-3 mb-0 h-44 shrink-0 rounded-[20px] min-[600px]:h-56 min-[900px]:col-start-1 min-[900px]:row-start-1 min-[900px]:mr-0 min-[900px]:mb-3 min-[900px]:h-auto min-[900px]:rounded-3xl" />
    </div>
  );
}

/** Screen heading (the page h1) with an optional one-line subtitle. */
export function AuthTitle({ title, large, children }: { title: string; large?: boolean; children?: ReactNode }) {
  return (
    <div className="mb-6">
      <h1 className={cx(large ? "text-[2rem] leading-[1.15] font-normal tracking-[-0.025em]" : "text-[15px] font-semibold")}>{title}</h1>
      {children && <p className="login-muted mt-1.5 text-[14px] leading-relaxed">{children}</p>}
    </div>
  );
}

/** m:ss for a countdown in seconds. */
export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
