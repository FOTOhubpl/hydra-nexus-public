import { useSyncExternalStore } from "react";
import { createPersistentStore } from "./storage";

/**
 * Jobs started (or tracked) from this browser. The backend exposes
 * GET /v1/jobs/{id} but no job listing, so the shell remembers the ids it
 * has seen. Purely a convenience: status always comes from the backend.
 */
export interface SessionJob {
  id: string;
  /** ISO timestamp of when this browser registered the job. */
  addedAt: string;
  /** Free-text origin, e.g. "3 files" or "tracked manually". */
  label: string;
}

const MAX_JOBS = 50;

function sanitize(raw: unknown): SessionJob[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((item): item is SessionJob =>
      Boolean(item) &&
      typeof (item as SessionJob).id === "string" &&
      typeof (item as SessionJob).addedAt === "string" &&
      typeof (item as SessionJob).label === "string",
    )
    .slice(0, MAX_JOBS);
}

export const sessionJobsStore = createPersistentStore<SessionJob[]>("jobs", [], sanitize);

export function useSessionJobs(): SessionJob[] {
  return useSyncExternalStore(sessionJobsStore.subscribe, sessionJobsStore.get, sessionJobsStore.get);
}

export function addSessionJob(id: string, label: string): void {
  const trimmed = id.trim();
  if (!trimmed) return;
  sessionJobsStore.set((prev) => [
    { id: trimmed, label, addedAt: new Date().toISOString() },
    ...prev.filter((job) => job.id !== trimmed),
  ].slice(0, MAX_JOBS));
}

export function removeSessionJob(id: string): void {
  sessionJobsStore.set((prev) => prev.filter((job) => job.id !== id));
}

export function clearSessionJobs(): void {
  sessionJobsStore.set([]);
}
