import { useSyncExternalStore } from "react";
import { createPersistentStore } from "./storage";

export type Theme = "light" | "dark";

/** OS preference (`prefers-color-scheme`), light when the browser cannot tell (B14, audit f450a70). */
export function systemTheme(): Theme {
  try {
    return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  } catch {
    return "light";
  }
}

/** A stored explicit choice wins; without one the OS preference decides. */
export function resolveTheme(stored: unknown, system: Theme): Theme {
  return stored === "dark" || stored === "light" ? stored : system;
}

export const themeStore = createPersistentStore<Theme>("theme", systemTheme(), (raw) => resolveTheme(raw, systemTheme()));

export function applyTheme(t: Theme): void {
  document.documentElement.dataset.theme = t;
}

export function useTheme(): Theme {
  return useSyncExternalStore(themeStore.subscribe, themeStore.get, themeStore.get);
}

export function toggleTheme(): void {
  themeStore.set((t) => (t === "dark" ? "light" : "dark"));
  applyTheme(themeStore.get());
}
