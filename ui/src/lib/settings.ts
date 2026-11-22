import { useSyncExternalStore } from "react";
import { createPersistentStore } from "./storage";

export interface UiSettings {
  /** GET /health poll interval for the status bar. */
  healthIntervalMs: number;
  /** GET /v1/jobs/{id} poll interval while a job is not terminal. */
  jobPollMs: number;
  /** Rows per page in the artifacts table. */
  pageSize: number;
  /** ForceAtlas2 iterations run once per graph load (bounded, synchronous). */
  layoutIterations: number;
  /** Show node labels in the graph regardless of zoom. */
  graphLabels: boolean;
}

export const DEFAULT_SETTINGS: UiSettings = {
  healthIntervalMs: 5000,
  jobPollMs: 1000,
  pageSize: 50,
  layoutIterations: 200,
  graphLabels: false,
};

export const SETTINGS_LIMITS = {
  healthIntervalMs: { min: 1000, max: 60000 },
  jobPollMs: { min: 250, max: 10000 },
  pageSize: { min: 10, max: 500 },
  layoutIterations: { min: 0, max: 2000 },
} as const;

function clamp(value: unknown, fallback: number, min: number, max: number): number {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.round(Math.min(max, Math.max(min, n)));
}

export function sanitizeSettings(raw: unknown): UiSettings {
  const src = raw && typeof raw === "object" ? (raw as Partial<Record<keyof UiSettings, unknown>>) : {};
  return {
    healthIntervalMs: clamp(src.healthIntervalMs, DEFAULT_SETTINGS.healthIntervalMs, SETTINGS_LIMITS.healthIntervalMs.min, SETTINGS_LIMITS.healthIntervalMs.max),
    jobPollMs: clamp(src.jobPollMs, DEFAULT_SETTINGS.jobPollMs, SETTINGS_LIMITS.jobPollMs.min, SETTINGS_LIMITS.jobPollMs.max),
    pageSize: clamp(src.pageSize, DEFAULT_SETTINGS.pageSize, SETTINGS_LIMITS.pageSize.min, SETTINGS_LIMITS.pageSize.max),
    layoutIterations: clamp(src.layoutIterations, DEFAULT_SETTINGS.layoutIterations, SETTINGS_LIMITS.layoutIterations.min, SETTINGS_LIMITS.layoutIterations.max),
    graphLabels: typeof src.graphLabels === "boolean" ? src.graphLabels : DEFAULT_SETTINGS.graphLabels,
  };
}

export const settingsStore = createPersistentStore<UiSettings>("settings", DEFAULT_SETTINGS, sanitizeSettings);

export function useSettings(): UiSettings {
  return useSyncExternalStore(settingsStore.subscribe, settingsStore.get, settingsStore.get);
}

export function updateSettings(patch: Partial<UiSettings>): void {
  settingsStore.set((prev) => sanitizeSettings({ ...prev, ...patch }));
}
