import { useEffect, useState } from "react";

/** Live `window.matchMedia(query).matches`; `fallback` where matchMedia does not exist (jsdom, SSR). */
export function useMediaQuery(query: string, fallback = false): boolean {
  const read = () => (typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(query).matches : fallback);
  const [matches, setMatches] = useState(read);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const m = window.matchMedia(query);
    const on = () => setMatches(m.matches);
    on();
    m.addEventListener?.("change", on);
    return () => m.removeEventListener?.("change", on);
  }, [query]);
  return matches;
}

/** Tailwind `md` breakpoint (48rem); desktop when matchMedia is unavailable. */
export const MD_UP = "(min-width: 48rem)";

export function useReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)", false);
}
