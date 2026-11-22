import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { isApiError, isTerminalStatus, queryKeys, useJob } from "../api/client";
import type { Job } from "../api/types";
import { cx } from "../lib/cx";
import { formatCount, formatPercent, formatTime, truncateMiddle } from "../lib/format";
import { useSettings } from "../lib/settings";
import { statusVariant } from "../lib/status";
import { Badge } from "./Badge";
import { Button } from "./Button";

const STATUS_LABELS: Record<string, string> = {
  queued: "oczekuje", running: "w toku", done: "zakończone", failed: "błąd",
};
const COUNT_LABELS: Record<string, string> = {
  processed: "przetworzone", total: "łącznie", imported: "zaimportowane", skipped: "pominięte",
  duplicate: "duplikaty", duplicates: "duplikaty", failed: "błędy", error: "błędy", errors: "błędy",
  quarantined: "kwarantanna", rejected: "odrzucone",
};

/** Completed fraction 0..1, or null when the backend reports nothing measurable. */
export function jobFraction(job: Pick<Job, "status" | "progress" | "total" | "processed"> | undefined): number | null {
  if (!job) return null;
  if (job.status === "done") return 1;
  if (typeof job.total === "number" && job.total > 0 && typeof job.processed === "number") {
    return Math.min(1, Math.max(0, job.processed / job.total));
  }
  if (typeof job.progress === "number" && Number.isFinite(job.progress)) {
    const value = job.progress > 1 ? job.progress / 100 : job.progress;
    return Math.min(1, Math.max(0, value));
  }
  return null;
}

export interface JobProgressProps {
  jobId: string;
  /** Free-text origin shown next to the id. */
  label?: string;
  /** Fired once when the job reaches done/failed. */
  onTerminal?: (job: Job) => void;
  onRemove?: () => void;
  className?: string;
}

export function JobProgress({ jobId, label, onTerminal, onRemove, className }: JobProgressProps) {
  const { jobPollMs } = useSettings();
  const query = useJob(jobId, jobPollMs);
  const client = useQueryClient();
  const job = query.data;
  const firedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!job || !isTerminalStatus(job.status)) return;
    const marker = `${jobId}:${job.status}`;
    if (firedFor.current === marker) return;
    firedFor.current = marker;
    void client.invalidateQueries({ queryKey: queryKeys.artifactsAll });
    void client.invalidateQueries({ queryKey: queryKeys.graph });
    onTerminal?.(job);
  }, [job, jobId, client, onTerminal]);

  const fraction = jobFraction(job);
  const status = job?.status;
  const active = status === "queued" || status === "running";
  const error = query.error;
  const lookupFailed = Boolean(error) && !job;

  const counts: Array<[string, number]> = [];
  if (job) {
    if (typeof job.processed === "number" || typeof job.total === "number") {
      counts.push(["processed", job.processed ?? 0]);
      if (typeof job.total === "number") counts.push(["total", job.total]);
    }
    for (const [name, value] of Object.entries(job.counts ?? {})) {
      if (typeof value === "number") counts.push([name, value]);
    }
  }

  const barTone = status === "failed" ? "bg-err" : status === "done" ? "bg-ok" : "bg-accent";

  return (
    <div className={cx("flex flex-col gap-1.5 px-3 py-2.5", className)} data-job={jobId} data-status={status ?? "unknown"}>
      <div className="flex items-center gap-2">
        <span className="min-w-0 truncate font-mono text-xs text-fg" title={jobId}>
          {truncateMiddle(jobId, 28)}
        </span>
        {label && <span className="truncate text-xs text-subtle">{label}</span>}
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {status ? (
            <Badge variant={statusVariant(status)} dot pulse={active}>
              {STATUS_LABELS[status] ?? status}
            </Badge>
          ) : lookupFailed ? (
            <Badge variant={isApiError(error) && error.isOffline ? "warning" : "danger"} dot>
              {isApiError(error) && error.isOffline ? "brak połączenia" : isApiError(error) && error.status === 404 ? "nieznane" : "błąd"}
            </Badge>
          ) : (
            <Badge variant="neutral" dot pulse>
              wczytywanie
            </Badge>
          )}
          {onRemove && (
            <Button size="sm" variant="ghost" aria-label={`Usuń zadanie ${jobId} z listy`} onClick={onRemove} className="px-1.5">
              ✕
            </Button>
          )}
        </span>
      </div>

      <div
        role="progressbar"
        aria-label={`Postęp zadania ${jobId}`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={fraction === null ? undefined : Math.round(fraction * 100)}
        className="relative h-1 overflow-hidden rounded-full bg-hover"
      >
        {fraction !== null ? (
          <div className={cx("h-full w-full origin-left rounded-full transition-transform duration-300", barTone)} style={{ transform: `scaleX(${fraction})` }} />
        ) : active ? (
          <div className={cx("absolute inset-y-0 w-1/3 rounded-full animate-indeterminate", barTone)} />
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
        {fraction !== null && <span className="font-mono text-fg">{formatPercent(fraction)}</span>}
        {counts.map(([name, value]) => (
          <span key={name}>
            <span className="text-subtle">{COUNT_LABELS[name] ?? name}</span> <span className="font-mono text-fg">{formatCount(value)}</span>
          </span>
        ))}
        {job?.updated_at && <span className="text-subtle">aktualizacja: {formatTime(job.updated_at)}</span>}
        {job?.message && <span className="truncate">{job.message}</span>}
      </div>

      {(job?.error || (lookupFailed && error)) && (
        <div className="rounded-sm border border-err/30 bg-err/5 px-2 py-1 font-mono text-xs break-words text-err">
          {job?.error ?? error?.message}
        </div>
      )}
    </div>
  );
}
