import type { BadgeVariant } from "../components/Badge";

const SUCCESS = new Set(["done", "ok", "success", "succeeded", "completed", "complete", "healthy", "ready", "up", "pass", "passed"]);
const INFO = new Set(["running", "processing", "active", "started", "in_progress", "importing", "indexing"]);
const WARNING = new Set(["warning", "warn", "degraded", "partial", "stale", "retrying", "skipped"]);
const DANGER = new Set(["failed", "fail", "error", "errored", "down", "rejected", "invalid", "offline", "unhealthy"]);

/** Map an arbitrary backend status string onto a badge colour. Unknown → neutral. */
export function statusVariant(status: string | null | undefined): BadgeVariant {
  if (!status) return "neutral";
  const key = status.trim().toLowerCase().replace(/[\s-]+/g, "_");
  if (SUCCESS.has(key)) return "success";
  if (INFO.has(key)) return "info";
  if (WARNING.has(key)) return "warning";
  if (DANGER.has(key)) return "danger";
  return "neutral";
}
