import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router";
import { useLiveStream } from "../api/stream";
import { cx } from "../lib/cx";
import { useSelectedRun } from "../lib/runSelection";
import { readJson, writeJson } from "../lib/storage";
import { CommandPalette } from "./CommandPalette";
import { ErrorBoundary } from "./ErrorBoundary";
import { GeneExplainer } from "./GeneExplainer";
import { Icon } from "./Icon";
import { InspectorDrawer } from "./InspectorDrawer";
import { BottomStatusBar, StatusBar, type Crumb } from "./StatusBar";

export interface NavItem {
  to: string;
  label: string;
  /** Icon name (components/Icon.tsx); no icon font so nothing loads from the network. */
  glyph: string;
  end?: boolean;
  /** Extra path prefixes that should also mark this item active. */
  match?: string[];
}

export interface NavGroup {
  title: string;
  items: NavItem[];
}

/** Plain Polish names (docs/ui/UI_LOG.md, 2026-11-21 redesign: the rename table). */
export const NAV_GROUPS: NavGroup[] = [
  {
    title: "Przegląd",
    items: [
      { to: "/", label: "Pulpit", glyph: "dashboard", end: true },
      { to: "/how", label: "Jak to działa", glyph: "help" },
      { to: "/simulation", label: "Symulacja potoku", glyph: "flow" },
    ],
  },
  {
    title: "Analiza",
    items: [
      { to: "/analyse", label: "Nowa analiza", glyph: "plus" },
      { to: "/incidents", label: "Incydenty", glyph: "incidents", match: ["/incident/", "/diff/"] },
      { to: "/search", label: "Wyszukiwanie", glyph: "search" },
      { to: "/hunt", label: "Poszukiwanie wariantów", glyph: "layers" },
    ],
  },
  {
    title: "Kampanie i powiązania",
    items: [
      { to: "/campaigns", label: "Kampanie", glyph: "campaigns", end: false },
      { to: "/graph", label: "Graf powiązań", glyph: "graph" },
    ],
  },
  {
    title: "Dowody",
    items: [
      { to: "/pairs", label: "Pary i dowody", glyph: "pairs", match: ["/pair/"] },
      { to: "/queue", label: "Kolejka analityka", glyph: "check" },
      { to: "/methodology", label: "Metodyka i wyniki", glyph: "chart" },
    ],
  },
  {
    title: "Monitoring",
    items: [
      { to: "/watch", label: "Monitoring domen", glyph: "eye" },
      { to: "/scheduler", label: "Harmonogram", glyph: "clock" },
      { to: "/integrations", label: "Integracje & API", glyph: "chain" },
    ],
  },
  {
    title: "Ustawienia",
    items: [
      { to: "/data", label: "Import danych", glyph: "upload", match: ["/item/"] },
      { to: "/settings", label: "Ustawienia", glyph: "settings" },
    ],
  },
];

export const NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

/** Detail routes: title plus the list page they belong to (breadcrumb parent). */
const DETAIL: { prefix: string; title: string | ((rest: string) => string); parent: string }[] = [
  { prefix: "/pair/", title: "Dowód pary", parent: "/pairs" },
  { prefix: "/lab/", title: (rest) => `Laboratorium hipotez · ${rest}`, parent: "/incidents" },
  { prefix: "/diff/", title: "Co się zmieniło", parent: "/incidents" },
  { prefix: "/incident/", title: "Incydent", parent: "/incidents" },
  { prefix: "/campaigns/", title: "Karta kampanii", parent: "/campaigns" },
  { prefix: "/item/", title: "Artefakt", parent: "/data" },
];

/** Account pages reached from the user menu (not in the sidebar). */
const ACCOUNT_TITLES: Record<string, string> = {
  "/account/password": "Zmień hasło",
  "/account/tokens": "Tokeny API",
  "/admin/users": "Użytkownicy",
};

function navItemFor(pathname: string): NavItem | undefined {
  return NAV_ITEMS.find((nav) => (nav.end ? pathname === nav.to : pathname === nav.to || pathname.startsWith(`${nav.to}/`) || (nav.to !== "/" && pathname.startsWith(nav.to))));
}

function groupOf(to: string): string | undefined {
  return NAV_GROUPS.find((g) => g.items.some((i) => i.to === to))?.title;
}

export function titleForPath(pathname: string): string {
  if (pathname === "/") return "Pulpit";
  if (ACCOUNT_TITLES[pathname]) return ACCOUNT_TITLES[pathname];
  const detail = DETAIL.find((d) => pathname.startsWith(d.prefix));
  if (detail) {
    if (typeof detail.title === "string") return detail.title;
    let rest = pathname.slice(detail.prefix.length).split("/")[0] ?? "";
    try { rest = decodeURIComponent(rest); } catch { /* keep the raw segment */ }
    return detail.title(rest);
  }
  return navItemFor(pathname)?.label ?? "Nie znaleziono";
}

/** Breadcrumb above the page title: section, then the list page for detail routes (the title itself is the h1). */
export function crumbsForPath(pathname: string): Crumb[] {
  if (ACCOUNT_TITLES[pathname]) return [{ label: pathname.startsWith("/admin/") ? "Administracja" : "Konto" }];
  const detail = DETAIL.find((d) => pathname.startsWith(d.prefix));
  if (detail) {
    const parent = NAV_ITEMS.find((n) => n.to === detail.parent)!;
    return [{ label: groupOf(parent.to) ?? "" }, { label: parent.label, to: parent.to }];
  }
  const item = pathname === "/" ? NAV_ITEMS[0] : navItemFor(pathname);
  const group = item ? groupOf(item.to) : undefined;
  return group ? [{ label: group }] : [];
}

const isTyping = (t: EventTarget | null) => t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));

/** One fixed tooltip for the icon rail: the rail scrolls, so per-item absolute tooltips would be clipped. */
interface Tip { label: string; top: number; left: number }

function NavList({ rail, onNavigate, onTip }: { rail: boolean; onNavigate?: () => void; onTip?: (t: Tip | null) => void }) {
  const { pathname } = useLocation();
  const show = (label: string) => (e: React.SyntheticEvent<HTMLElement>) => {
    if (!rail || !onTip) return;
    const r = e.currentTarget.getBoundingClientRect();
    onTip({ label, top: r.top + r.height / 2, left: r.right + 10 });
  };
  const hide = () => onTip?.(null);
  return (
    <div className={cx("flex flex-1 flex-col overflow-y-auto overscroll-contain py-2", rail ? "gap-2 px-2" : "gap-4 px-3")} onScroll={hide}>
      {NAV_GROUPS.map((group) => (
        <div key={group.title} role="group" aria-label={group.title}>
          {rail ? <div aria-hidden="true" className="mx-2 mb-2 border-t border-line first:hidden" /> : (
            <div className="px-2.5 pb-1 text-xs font-medium text-subtle">{group.title}</div>
          )}
          <ul className="flex flex-col gap-0.5">
            {group.items.map((item) => {
              const extra = item.match?.some((prefix) => pathname.startsWith(prefix)) ?? false;
              return (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    end={item.end}
                    onClick={onNavigate}
                    aria-label={item.label}
                    onMouseEnter={show(item.label)}
                    onMouseLeave={hide}
                    onFocus={show(item.label)}
                    onBlur={hide}
                    className={({ isActive }) =>
                      cx(
                        "group relative flex h-9 items-center gap-3 rounded-lg text-sm transition-colors duration-150",
                        rail ? "justify-center px-0" : "px-2.5",
                        isActive || extra ? "bg-accent-soft font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg",
                      )
                    }
                  >
                    {({ isActive }) => (
                      <>
                        {(isActive || extra) && <span aria-hidden="true" className="absolute top-1.5 bottom-1.5 left-0 w-[3px] rounded-full bg-accent" />}
                        <Icon name={item.glyph} className={cx("shrink-0", isActive || extra ? "text-accent" : "")} />
                        {!rail && <span className="truncate">{item.label}</span>}
                      </>
                    )}
                  </NavLink>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}

function Brand({ rail }: { rail: boolean }) {
  return (
    <div className={cx("flex min-w-0 items-center gap-2.5", rail && "justify-center")}>
      <span aria-hidden="true" className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-fg font-mono text-sm font-bold text-canvas">H</span>
      {!rail && (
        <span className="min-w-0 leading-tight">
          <span className="block truncate text-sm font-semibold tracking-tight text-fg">HYDRA NEXUS</span>
          <span className="block truncate text-2xs text-subtle">NASK · Kolekcjoner fejków</span>
        </span>
      )}
    </div>
  );
}

/** Floating inset sidebar (md and up): rounded card off the viewport edge, collapses to an icon rail. */
function SideNav({ expanded, onToggle }: { expanded: boolean; onToggle: () => void }) {
  const rail = !expanded;
  const [tip, setTip] = useState<Tip | null>(null);
  useEffect(() => setTip(null), [expanded]);
  return (
    <div className="hidden shrink-0 py-3 pl-3 md:flex" data-testid="sidebar" data-state={expanded ? "expanded" : "collapsed"}>
      <nav
        aria-label="Nawigacja"
        className={cx(
          "flex flex-col overflow-hidden rounded-2xl border border-line bg-panel shadow-float",
          expanded ? "w-60" : "w-16",
        )}
      >
        <div className={cx("flex h-14 shrink-0 items-center", rail ? "justify-center px-2" : "px-4")}>
          <Brand rail={rail} />
        </div>
        <NavList rail={rail} onTip={setTip} />
        <div className={cx("flex shrink-0 items-center gap-2 border-t border-line p-2", rail ? "justify-center" : "justify-between pl-4")}>
          {!rail && <span className="truncate text-2xs text-subtle">Lokalnie, bez chmury</span>}
          <button
            type="button"
            onClick={onToggle}
            aria-label={expanded ? "Zwiń menu" : "Rozwiń menu"}
            aria-expanded={expanded}
            aria-keyshortcuts="Control+B Meta+B"
            title={`${expanded ? "Zwiń menu" : "Rozwiń menu"} (Ctrl/⌘ B)`}
            data-testid="sidebar-toggle"
            className="flex size-9 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg"
          >
            <Icon name="panel" />
          </button>
        </div>
      </nav>
      {tip && (
        <div role="tooltip" className="pointer-events-none fixed z-50 -translate-y-1/2 rounded-md bg-fg px-2 py-1 text-xs font-medium whitespace-nowrap text-canvas shadow-float" style={{ top: tip.top, left: tip.left }}>
          {tip.label}
        </div>
      )}
    </div>
  );
}

/** Phones and narrow windows: the same navigation as a modal drawer. */
function NavDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    panel.current?.querySelector<HTMLElement>("a,button")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && panel.current) {
        const f = [...panel.current.querySelectorAll<HTMLElement>("a,button")];
        if (f.length === 0) return;
        const first = f[0]!, last = f[f.length - 1]!;
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); prev?.focus?.(); };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 md:hidden" role="presentation">
      <div className="absolute inset-0 bg-fg/30 motion-safe:animate-[fade-in_150ms_ease-out]" onClick={onClose} aria-hidden="true" />
      <div ref={panel} role="dialog" aria-modal="true" aria-label="Menu" className="absolute inset-y-2 left-2 flex w-[min(18rem,calc(100vw-3rem))] flex-col overflow-hidden rounded-2xl border border-line bg-panel shadow-float motion-safe:animate-[drawer-in_200ms_var(--ease-out-soft)]">
        <div className="flex h-14 shrink-0 items-center justify-between px-4">
          <Brand rail={false} />
          <button type="button" onClick={onClose} aria-label="Zamknij menu" className="flex size-9 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg">
            <Icon name="close" />
          </button>
        </div>
        <nav aria-label="Nawigacja" className="flex min-h-0 flex-1 flex-col">
          <NavList rail={false} onNavigate={onClose} />
        </nav>
      </div>
    </div>
  );
}

export function AppShell() {
  const { pathname } = useLocation();
  // Run-scoped query hashes include the selected run; remounting the views on a switch re-subscribes them.
  const runKey = useSelectedRun() ?? "latest";
  // one live stream per tab (DATAFLOW): patches dashboard / campaigns / overview caches in place
  useLiveStream();
  const [expanded, setExpanded] = useState<boolean>(() => readJson("nav.expanded", true));
  const [drawer, setDrawer] = useState(false);
  const toggle = useCallback(() => setExpanded((v) => { writeJson("nav.expanded", !v); return !v; }), []);
  const closeDrawer = useCallback(() => setDrawer(false), []);
  useEffect(() => {
    const flip = () => {
      if (window.matchMedia?.("(min-width: 768px)").matches ?? true) toggle();
      else setDrawer((v) => !v);
    };
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "b" && !isTyping(e.target)) {
        e.preventDefault();
        flip();
      }
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("hydra:nav-toggle", flip); // CommandPalette NAV_TOGGLE_EVENT
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("hydra:nav-toggle", flip); };
  }, [toggle]);
  useEffect(() => setDrawer(false), [pathname]);
  // floating top bar: compact + lifted once the view scrolls (state flips only at the threshold)
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => setScrolled(false), [pathname]);
  const onMainScroll = useCallback((e: React.UIEvent<HTMLElement>) => {
    const next = e.currentTarget.scrollTop > 8;
    setScrolled((v) => (v === next ? v : next));
  }, []);
  const fixedHeight = pathname.startsWith("/simulation");
  return (
    <div className="flex h-full min-h-0 overflow-hidden bg-canvas text-fg">
      <SideNav expanded={expanded} onToggle={toggle} />
      <NavDrawer open={drawer} onClose={closeDrawer} />
      <div className="flex min-w-0 flex-1 flex-col">
        <StatusBar title={titleForPath(pathname)} crumbs={crumbsForPath(pathname)} onMenu={() => setDrawer(true)} scrolled={scrolled} />
        <div className="flex min-h-0 flex-1">
          <main onScroll={onMainScroll} className={cx("relative min-h-0 min-w-0 flex-1", fixedHeight ? "overflow-hidden" : "overflow-auto")}>
            <ErrorBoundary resetKeys={[pathname]} label="Ten widok">
              <Outlet key={runKey} />
            </ErrorBoundary>
          </main>
          <InspectorDrawer key={runKey} />
        </div>
        <BottomStatusBar />
      </div>
      <CommandPalette />
      <GeneExplainer />
    </div>
  );
}
