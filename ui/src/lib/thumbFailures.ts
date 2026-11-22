/**
 * Short-lived, in-memory memory of thumbnails that failed to load (B12, audit f450a70: the Pulpit re-requested the
 * same failing thumbnails on every navigation). CR-ASTRA-RT-01: a DOM `error` event does not say WHY an image failed
 * (expired session, network blip, server restart and an undecodable file all look the same), so a failure is never
 * treated as proof of a corrupt image and is never persisted. It is kept for this page session only, for a short
 * TTL; the next session, the TTL expiring or an explicit retry load the image again. The persistent list written by
 * earlier builds is removed on load so it cannot keep blocking images.
 */
import { removeKey } from "./storage";

export const THUMB_FAILURE_TTL_MS = 5 * 60_000;
const CAP = 500;

const failed = new Map<string, number>();
removeKey("thumb-failures"); // legacy persistent negative cache (pre RT-01)

export function thumbFailed(artifactId: string, now: number = Date.now()): boolean {
  const at = failed.get(artifactId);
  if (at === undefined) return false;
  if (now - at >= THUMB_FAILURE_TTL_MS) {
    failed.delete(artifactId);
    return false;
  }
  return true;
}

export function markThumbFailed(artifactId: string, now: number = Date.now()): void {
  failed.delete(artifactId);
  failed.set(artifactId, now);
  while (failed.size > CAP) failed.delete(failed.keys().next().value as string);
}

/** Explicit retry from the UI: forget the failure so the image is requested again. */
export function clearThumbFailure(artifactId: string): void {
  failed.delete(artifactId);
}

/** Tests only: simulates a new page session (module state lives for one page load). */
export function resetThumbFailures(): void {
  failed.clear();
}
