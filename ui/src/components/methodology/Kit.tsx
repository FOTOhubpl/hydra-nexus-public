/** Building blocks of the /methodology explainer: section card, "Szczegóły techniczne", term tooltip, sticky TOC. */
import { useEffect, useState, type ReactNode } from "react";
import { Tooltip } from "../Tooltip";
import { cx } from "../../lib/cx";
import { useReducedMotion } from "../../lib/media";

export interface TocItem { id: string; n: number; title: string }

export function MethodSection({ id, n, title, lead, children, tech }: { id: string; n: number; title: string; lead: ReactNode; children?: ReactNode; tech?: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-h`} className="surface-card scroll-mt-20 p-4 sm:p-6" data-testid={`method-section-${id}`}>
      <div className="mb-2 flex items-baseline gap-3">
        <span aria-hidden="true" className="font-mono text-xs font-semibold text-accent">{String(n).padStart(2, "0")}</span>
        <h2 id={`${id}-h`} className="text-lg font-semibold text-fg">{title}</h2>
      </div>
      <div className="max-w-3xl text-sm leading-relaxed text-fg">{lead}</div>
      {children && <div className="mt-4 space-y-3 text-sm text-fg">{children}</div>}
      {tech && (
        <details className="group mt-4 rounded-xl border border-line bg-raised/40">
          <summary className="cursor-pointer list-none rounded-xl px-4 py-2.5 text-sm font-semibold text-fg hover:bg-hover focus-visible:outline-2">
            <span aria-hidden="true" className="mr-2 inline-block transition-transform group-open:rotate-90 motion-reduce:transition-none">›</span>
            Szczegóły techniczne
          </summary>
          <div className="space-y-2 border-t border-line px-4 py-3 text-xs leading-relaxed text-muted [&_code]:font-mono [&_code]:text-fg">{tech}</div>
        </details>
      )}
    </section>
  );
}

/** A glossary term with a definition tooltip (keyboard-focusable). */
export function Term({ children, tip }: { children: ReactNode; tip: ReactNode }) {
  return (
    <Tooltip content={tip} focusable>
      <span className="cursor-help underline decoration-dotted decoration-subtle underline-offset-2">{children}</span>
    </Tooltip>
  );
}

export function Code({ children }: { children: ReactNode }) {
  return <code className="rounded bg-raised px-1 py-0.5 font-mono text-[0.85em] text-fg">{children}</code>;
}

/** Scroll-spy: the id of the section nearest the top of the viewport (IntersectionObserver; none in jsdom). */
function useActive(ids: string[]): string {
  const [active, setActive] = useState(ids[0] ?? "");
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const seen = new Map<string, boolean>();
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) seen.set(e.target.id, e.isIntersecting);
      const first = ids.find((i) => seen.get(i));
      if (first) setActive(first);
    }, { rootMargin: "-15% 0px -70% 0px" });
    ids.forEach((i) => { const el = document.getElementById(i); if (el) io.observe(el); });
    return () => io.disconnect();
  }, [ids]);
  return active;
}

export function Toc({ items }: { items: TocItem[] }) {
  const active = useActive(items.map((i) => i.id));
  const reduced = useReducedMotion();
  const go = (id: string) => (e: React.MouseEvent) => {
    e.preventDefault();
    document.getElementById(id)?.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
    (document.getElementById(`${id}-h`) as HTMLElement | null)?.focus?.({ preventScroll: true });
  };
  return (
    <nav aria-label="Spis treści metodyki" data-testid="method-toc" className="surface-card sticky top-2 z-10 p-2 lg:top-4 lg:p-3">
      <div className="mb-1 hidden px-2 text-2xs font-semibold tracking-wider text-subtle uppercase lg:block">Spis treści</div>
      <ol className="flex gap-1 overflow-x-auto overscroll-x-contain lg:flex-col lg:overflow-visible">
        {items.map((i) => (
          <li key={i.id} className="shrink-0">
            <a href={`#${i.id}`} onClick={go(i.id)} aria-current={active === i.id ? "location" : undefined}
              className={cx("flex items-center gap-2 rounded-lg px-2 py-1.5 text-xs whitespace-nowrap hover:bg-hover",
                active === i.id ? "bg-accent-soft font-semibold text-fg" : "text-muted")}>
              <span className="font-mono text-2xs text-subtle">{String(i.n).padStart(2, "0")}</span>{i.title}
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}
