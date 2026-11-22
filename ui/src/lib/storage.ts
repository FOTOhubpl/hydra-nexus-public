/**
 * localStorage access that never throws (private mode, blocked storage,
 * quota). Used only for per-browser conveniences, never for data of record.
 */
export const STORAGE_PREFIX = "hydra.ui.";

export function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(STORAGE_PREFIX + key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function writeJson(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(value));
  } catch {
    // Storage unavailable: the value simply is not persisted.
  }
}

export function removeKey(key: string): void {
  try {
    window.localStorage.removeItem(STORAGE_PREFIX + key);
  } catch {
    // ignore
  }
}

/** Tiny external store with localStorage persistence, for useSyncExternalStore. */
export function createPersistentStore<T>(key: string, initial: T, sanitize: (raw: unknown) => T) {
  let state: T = sanitize(readJson<unknown>(key, initial));
  const listeners = new Set<() => void>();

  return {
    get: (): T => state,
    set(next: T | ((prev: T) => T)) {
      state = typeof next === "function" ? (next as (prev: T) => T)(state) : next;
      writeJson(key, state);
      listeners.forEach((listener) => listener());
    },
    reset() {
      state = initial;
      removeKey(key);
      listeners.forEach((listener) => listener());
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
