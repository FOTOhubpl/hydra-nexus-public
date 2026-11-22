import { useSyncExternalStore } from "react";

/** What the right inspector drawer shows. One selection at a time, app-wide. */
export type Selection =
  | { kind: "campaign"; id: string }
  | { kind: "pair"; a: string; b: string }
  | { kind: "incident"; id: string }
  | null;

let current: Selection = null;
const listeners = new Set<() => void>();

export const inspector = {
  get: () => current,
  open(sel: Selection) {
    current = sel;
    listeners.forEach((l) => l());
  },
  close() {
    inspector.open(null);
  },
  subscribe(l: () => void) {
    listeners.add(l);
    return () => listeners.delete(l);
  },
};

export function useInspector(): Selection {
  return useSyncExternalStore(inspector.subscribe, inspector.get, inspector.get);
}
