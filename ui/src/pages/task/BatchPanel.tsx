import { useEffect, useRef, useState, type FormEvent } from "react";
import { toUploadEntries, type UploadEntry } from "../../api/client";
import { useBatchResult, useStartBatch, type BatchReport, type BatchRow } from "../../api/batch";
import { Button } from "../../components/Button";
import { DecisionBadge } from "../../components/evidence";
import { Panel } from "../../components/Panel";
import { formatBits } from "../../lib/evidence";
import { splitUploads } from "../../lib/uploadPolicy";
import { CampaignLink, IncidentLink } from "./common";
import { polishReason } from "../../lib/displayPl";

/** Results of one batch: per item decision, the new patterns shared by several new items, duplicates. */
export function BatchReportView({ report }: { report: BatchReport }) {
  const s = report.summary;
  const rows: BatchRow[] = report.items ?? [];
  const multi = (report.new_patterns ?? []).filter((p) => p.size > 1);
  return (
    <div className="space-y-3 text-sm">
      {s && (
        <div className="text-xs text-muted">
          materiałów {s.items} · nowe wzorce {s.new_patterns} (wspólnych dla kilku materiałów: {s.multi_member_new_patterns},
          obejmują {s.items_in_multi_member_patterns}) · duplikaty {s.duplicates}
        </div>
      )}
      {multi.length > 0 ? (
        <div className="space-y-2">
          <div className="font-semibold">Nowe wzorce w tej partii</div>
          {multi.map((p) => (
            <div key={p.id} className="rounded-md border border-line p-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono font-semibold">{p.id}</span>
                <span className="text-xs text-muted">{p.size} materiały: {p.items.join(", ")}</span>
              </div>
              <div className="mt-1 text-xs text-fg">{polishReason(p.reason_pl)}</div>
              <ul className="mt-1 space-y-0.5 text-2xs text-muted">
                {p.links.slice(0, 6).map((l) => (
                  <li key={`${l.a}|${l.b}`}>
                    <IncidentLink id={l.a} /> ↔ <IncidentLink id={l.b} /> <span className="font-mono">{formatBits(l.support)}</span>
                    {l.reasons.length > 0 && <>: {l.reasons.slice(0, 3).map((r) => r.explanation_pl.split(" — ")[0]).join("; ")}</>}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-xs text-subtle">Żadne dwa nowe materiały tej partii nie mają między sobą dowodów na poziomie kampanii.</div>
      )}
      <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs">
        <thead className="text-2xs tracking-wider text-subtle uppercase">
          <tr><th className="py-1 text-left">Materiał</th><th className="py-1 text-left">Decyzja</th><th className="py-1 text-left">Kampania / nowy wzorzec</th><th className="py-1 text-left">Duplikat</th></tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.item}-${i}`} className="border-t border-line">
              <td className="py-1 font-mono">{r.incident_id ? <IncidentLink id={r.incident_id} title={r.item} /> : r.item}</td>
              <td className="py-1">{r.decision === "error" ? <span className="text-err">błąd</span> : <DecisionBadge value={r.decision} />}</td>
              <td className="py-1">{r.campaign_id ? <CampaignLink id={r.campaign_id} /> : r.new_pattern_id ? <span className="font-mono">{r.new_pattern_id}</span> : "—"}</td>
              <td className="py-1">{r.duplicate_of ? <span>kopia <span className="font-mono">{r.duplicate_of_item ?? r.duplicate_of}</span></span> : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
      {report.note_pl && <div className="text-2xs text-muted">{report.note_pl}</div>}
    </div>
  );
}

export function BatchPanel() {
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<UploadEntry[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const folder = useRef<HTMLInputElement>(null);
  const start = useStartBatch();
  const result = useBatchResult(jobId);
  useEffect(() => {
    folder.current?.setAttribute("webkitdirectory", "");
  }, []);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const input = entries.length ? { kind: "files" as const, entries } : path.trim() ? { kind: "path" as const, path: path.trim() } : null;
    if (!input) return;
    start.mutate(input, { onSuccess: ({ job_id }) => setJobId(job_id) });
  };
  const job = result.data?.job as (Record<string, unknown> & { status?: string }) | null | undefined;
  const report = result.data?.report;
  return (
    <Panel title="Partia nowych materiałów">
      <form onSubmit={submit} className="space-y-2">
        <p className="text-xs text-muted">
          Folder z wieloma nowymi materiałami (jeden podfolder, plik lub ZIP na materiał). Każdy materiał jest najpierw
          oceniany osobno względem znanych kampanii, potem nowe wzorce są porównywane między sobą: materiały jednej
          nieznanej kampanii dostają wspólny identyfikator nowego wzorca.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="folder na serwerze, np. ~/HydraNexus/data/nowe" aria-label="Folder partii"
            className="h-8 min-w-64 flex-1 rounded-[4px] border border-line bg-raised px-3 font-mono text-sm focus:border-accent focus:outline-none" />
          <Button onClick={() => folder.current?.click()}>Wybierz folder…</Button>
          <input ref={folder} type="file" hidden multiple onChange={(e) => setEntries(splitUploads(toUploadEntries(e.target.files ?? [])).keep)} />
          <Button type="submit" variant="primary" disabled={(!path.trim() && !entries.length) || start.isPending}>Analizuj partię</Button>
        </div>
        {entries.length > 0 && <div className="text-xs text-muted">wybrano {entries.length} plik(ów) z folderu</div>}
        {start.isError && <div className="text-xs text-err">{start.error instanceof Error ? start.error.message : "Błąd"}</div>}
      </form>
      {job && (
        <div className="mt-2 text-xs text-muted">
          zadanie <span className="font-mono">{String(job.id ?? jobId)}</span>:{" "}
          {job.status === "done" ? "zakończone" : job.status === "failed" ? <span className="text-err">błąd {String(job.error ?? "")}</span>
            : `przetwarzanie… ${String(job.done ?? 0)}/${String(job.total ?? "?")}`}
        </div>
      )}
      {report && <div className="mt-3"><BatchReportView report={report} /></div>}
    </Panel>
  );
}
