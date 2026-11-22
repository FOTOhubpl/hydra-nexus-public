/**
 * Live activity of one analyse job (hydra/api_progress.py): Server-Sent Events from
 * `/v1/analyse/{id}/progress/stream`, falling back to polling `/v1/analyse/{id}/progress` every second when
 * EventSource is unavailable or fails. Every snapshot is kept with its arrival time (for rates, ETA and the stall
 * indicator); nothing is interpolated.
 */
import { useEffect, useRef, useState } from "react";
import { apiFetch, apiUrl } from "./client";
import { withRun } from "../lib/runSelection";

export type StageState = "done" | "running" | "waiting" | "failed";

export interface ImportPhase {
  phase: "listing" | "importing" | "done" | "failed";
  files_total: number | null;
  files_done: number;
  bytes_total: number | null;
  bytes_done: number;
  rejected: number;
  incidents_total: number | null;
  incidents_done: number;
  current: string | null;
  percent: number | null;
  started_at?: string;
  updated_at?: string | null;
  error?: string | null;
}

export interface AnalyseProgress {
  job_id: string;
  status: "queued" | "running" | "done" | "failed";
  mode: "corpus" | "single";
  source?: string | null;
  stage: string;
  stage_label: string;
  stage_percent: number | null;
  counters: { done?: number; total?: number; unit?: string; item?: string | null; sub?: string };
  stages: { key: string; label: string; state: StageState; weight: number; percent: number | null }[];
  overall_percent: number;
  weights_source: string;
  import: ImportPhase | null;
  core: Record<string, unknown> | null;
  error_code?: string | null;
  error?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface ProgressSample {
  snap: AnalyseProgress;
  /** Date.now() when the snapshot arrived. */
  at: number;
}

const TERMINAL = new Set(["done", "failed"]);

export function useAnalyseProgress(jobId: string | null): { samples: ProgressSample[]; transport: "sse" | "poll" | null } {
  const [samples, setSamples] = useState<ProgressSample[]>([]);
  const [transport, setTransport] = useState<"sse" | "poll" | null>(null);
  const last = useRef<string>("");
  useEffect(() => {
    setSamples([]);
    last.current = "";
    if (!jobId) return;
    let closed = false;
    let es: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const ctrl = new AbortController();
    const push = (snap: AnalyseProgress) => {
      const sig = JSON.stringify(snap);
      if (sig === last.current) return;
      last.current = sig;
      setSamples((prev) => [...prev.slice(-299), { snap, at: Date.now() }]);
    };
    const poll = async () => {
      if (closed) return;
      try {
        const snap = await apiFetch<AnalyseProgress>(`/v1/analyse/${encodeURIComponent(jobId)}/progress`, { signal: ctrl.signal });
        push(snap);
        if (TERMINAL.has(snap.status)) return;
      } catch {
        /* transient: keep polling; the stall indicator shows the silence */
      }
      if (!closed) timer = setTimeout(poll, 1000);
    };
    if (typeof EventSource === "function") {
      setTransport("sse");
      es = new EventSource(apiUrl(withRun(`/v1/analyse/${encodeURIComponent(jobId)}/progress/stream`)), { withCredentials: true });
      es.addEventListener("progress", (e) => {
        try {
          const snap = JSON.parse((e as MessageEvent<string>).data) as AnalyseProgress;
          push(snap);
          if (TERMINAL.has(snap.status)) es?.close();
        } catch {
          /* ignore a malformed message */
        }
      });
      es.onerror = () => {
        es?.close();
        if (closed) return;
        setTransport("poll");
        void poll();
      };
    } else {
      setTransport("poll");
      void poll();
    }
    return () => {
      closed = true;
      es?.close();
      ctrl.abort();
      if (timer) clearTimeout(timer);
    };
  }, [jobId]);
  return { samples, transport };
}
