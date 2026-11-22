/** Review-driven revisions of the clustering: who / what / when, the membership diff each caused, and undo. */
import { useState } from "react";
import { useRevisions, useUndo } from "../api/visual";
import { cx } from "../lib/cx";
import { formatDatePl, REVIEW_LABEL } from "../lib/evidence";

export function RevisionsPanel({ campaignId }: { campaignId?: string }) {
  const q = useRevisions(campaignId);
  const undo = useUndo();
  const [flash, setFlash] = useState<string | null>(null);
  const items = [...(q.data?.items ?? [])].reverse();
  if (q.isPending) return <div className="text-xs text-subtle">Ładowanie historii…</div>;
  if (!items.length) return <div className="text-sm text-subtle">Brak rewizji{campaignId ? " dotyczących tej kampanii" : ""}: grupowanie pochodzi wprost z przebiegu analizy.</div>;
  return (
    <ol className="relative space-y-4 border-l border-line pl-5">
      {items.map((r) => (
        <li key={r.run_id} className="relative">
          <span className="absolute top-1.5 -left-[25px] size-2.5 rounded-full bg-accent ring-4 ring-panel" />
          <div className="flex flex-wrap items-baseline gap-2 text-sm">
            <b>{REVIEW_LABEL[r.action ?? ""] ?? r.action}</b>
            <span className="text-muted">{r.analyst ?? "operator"} · {formatDatePl(r.created_at)}</span>
            <span className="font-mono text-xs text-subtle">{r.run_id}</span>
            {r.review_id && r.action !== "undo" && (
              <button type="button" disabled={undo.isPending} onClick={() => undo.mutate(r.review_id!, { onSuccess: () => setFlash(r.run_id) })}
                className="ml-auto h-7 rounded-md border border-line-strong px-2.5 text-xs hover:bg-hover disabled:cursor-not-allowed disabled:text-subtle">↶ Cofnij</button>
            )}
          </div>
          {r.note && <div className="text-xs text-muted">„{r.note}”</div>}
          {r.moved.length ? (
            <ul className="mt-1.5 flex flex-wrap gap-2 text-xs">
              {r.moved.map((m) => (
                <li key={m.incident_id} className={cx("inline-flex items-center gap-1.5 rounded-md border border-line px-2 py-1 transition-colors duration-700", flash === r.run_id && "border-accent bg-for-soft")}>
                  <span className="font-mono">{m.incident_id}</span>
                  <span className="rounded bg-against-soft px-1 font-mono line-through decoration-err">{m.from ?? "—"}</span>
                  <span aria-hidden="true">→</span>
                  <span className="rounded bg-for-soft px-1 font-mono">{m.to ?? "—"}</span>
                  {m.to_successor_of && <span className="text-subtle">(dawniej {m.to_successor_of})</span>}
                </li>
              ))}
            </ul>
          ) : <div className="mt-1 text-xs text-subtle">bez zmian przynależności ({r.constraints} ograniczeń w rewizji)</div>}
          {r.renamed && Object.keys(r.renamed).length > 0 && <div className="mt-1 text-xs text-subtle">nowe identyfikatory kampanii w rewizji: {Object.entries(r.renamed).map(([o, n]) => `${o} → ${n}`).join(", ")}</div>}
        </li>
      ))}
      {undo.isError && <li className="text-xs text-err">{undo.error instanceof Error ? undo.error.message : "Cofnięcie nie powiodło się"}</li>}
    </ol>
  );
}
