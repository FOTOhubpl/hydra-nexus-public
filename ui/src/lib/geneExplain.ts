import { useSyncExternalStore } from "react";

/** Which gene the "dlaczego to się liczy" panel explains (app-wide, one at a time). */
let current: { kind: string; value: string } | null = null;
const listeners = new Set<() => void>();
export function openGene(kind: string, value: string): void { current = { kind, value }; listeners.forEach((l) => l()); }
export function closeGene(): void { current = null; listeners.forEach((l) => l()); }
export function useOpenGene() {
  return useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, () => current, () => current);
}
