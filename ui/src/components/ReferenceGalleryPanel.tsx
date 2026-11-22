import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { apiFetch, extractErrorMessage, isApiError } from "../api/client";
import { useSession } from "../api/auth";
import { Button } from "./Button";

/** GALLERY: local face gallery + logo templates built from the operator's own imported material. */
export interface ReferenceJob {
  id: string;
  status: string;
  total?: number;
  done?: number;
  result?: { ok?: boolean; message_pl?: string; incidents?: number; faces?: { entries: number }; logos?: { templates: number } } | null;
}

export interface ReferenceStatus {
  face_entries: number;
  logo_templates: number;
  built?: string | null;
  explain_pl: string;
  job?: ReferenceJob | null;
}

const STATUS_KEY = ["reference", "status"] as const;
const BUSY = new Set(["queued", "running"]);

export function useReferenceStatus(poll: boolean) {
  return useQuery({
    queryKey: STATUS_KEY,
    queryFn: ({ signal }) => apiFetch<ReferenceStatus>("/v1/reference/status", { signal }),
    refetchInterval: poll ? 1000 : false,
  });
}

function useIsAdmin(): boolean {
  const s = useSession().data;
  if (!s?.auth_enabled) return true;
  return s.user?.role === "admin";
}

export function ReferenceGalleryPanel({ compact = false }: { compact?: boolean }) {
  const client = useQueryClient();
  const admin = useIsAdmin();
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reanalyse, setReanalyse] = useState<{ id: string; status: string } | null>(null);
  const status = useReferenceStatus(polling);
  const job = status.data?.job ?? null;
  const busy = !!job && BUSY.has(job.status);
  useEffect(() => {
    if (polling && job && !busy) setPolling(false);
  }, [polling, job, busy]);

  async function build() {
    setError(null);
    try {
      await apiFetch("/v1/reference/build", { method: "POST", json: { faces: true, logos: true } });
      setPolling(true);
      await client.invalidateQueries({ queryKey: STATUS_KEY });
    } catch (e) {
      setError(isApiError(e) ? e.message : extractErrorMessage(e, "Nie udało się uruchomić budowy galerii."));
    }
  }

  async function rerun() {
    setError(null);
    try {
      const r = await apiFetch<{ job_id: string }>("/v1/reference/reanalyse", { method: "POST" });
      setReanalyse({ id: r.job_id, status: "queued" });
      const tick = async () => {
        const j = await apiFetch<{ job: { status: string } }>(`/v1/analyse/${r.job_id}`);
        setReanalyse({ id: r.job_id, status: j.job.status });
        if (BUSY.has(j.job.status)) window.setTimeout(() => void tick(), 2000);
        else void client.invalidateQueries();
      };
      void tick();
    } catch (e) {
      setError(isApiError(e) ? e.message : "Nie udało się uruchomić analizy.");
    }
  }

  const d = status.data;
  const result = job?.result;
  return (
    <div className="flex flex-col gap-2 rounded-md border border-line p-3 text-xs" data-testid="reference-gallery">
      <div className="text-sm font-semibold text-fg">Galeria referencyjna (twarze, logo)</div>
      {!compact && d && <p className="text-muted">{d.explain_pl}</p>}
      {d && (
        <div data-testid="reference-counts">
          W użyciu: osób w galerii twarzy <b>{d.face_entries}</b>, wzorców logo <b>{d.logo_templates}</b>
          {d.built ? ` (zbudowano ${d.built})` : ""}.
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="primary" disabled={!admin || busy} title={admin ? undefined : "Wymagana rola: administrator"} onClick={() => void build()}>
          Zbuduj galerię referencyjną z zaimportowanych materiałów
        </Button>
        {busy && <span className="text-accent">budowanie… {job?.done ?? 0}/{job?.total ?? "?"}</span>}
        <Button size="sm" disabled={!!reanalyse && BUSY.has(reanalyse.status)} onClick={() => void rerun()}>
          Przelicz analizę
        </Button>
        {reanalyse && (
          <span className="text-muted">
            analiza: {BUSY.has(reanalyse.status) ? "w toku…" : reanalyse.status === "done" ? "zakończona — geny ID/logo są w wynikach" : "błąd"}
          </span>
        )}
      </div>
      {result?.message_pl && !busy && <div className={result.ok === false ? "text-abstain-ink" : "text-ok"}>{result.message_pl}</div>}
      {error && <div className="text-err">{error}</div>}
    </div>
  );
}
