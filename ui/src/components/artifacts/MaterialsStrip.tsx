import { Link } from "react-router";
import { KIND_LABEL, thumbUrl, useMaterials, type ArtifactKind } from "../../api/artifacts";
import { ROLE_LABEL } from "../../lib/evidence";
import { formatBytes, formatCount } from "../../lib/format";

const KIND_ORDER: ArtifactKind[] = ["image", "video", "audio", "html", "har", "json", "text", "other"];

/** Dashboard "Materiały": stored artefacts by preview type and corpus, plus the newest ad/screen materials. */
export function MaterialsStrip() {
  const q = useMaterials();
  const m = q.data;
  if (!m) return <div className="text-sm text-subtle">{q.isError ? "Nie udało się pobrać podsumowania materiałów (/v1/materials)." : "Ładowanie materiałów…"}</div>;
  if (m.total === 0) return <div className="text-sm text-subtle">Brak zaimportowanych artefaktów.</div>;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2 text-xs">
        <span className="rounded-md border border-line px-2 py-1"><b className="font-mono text-fg">{formatCount(m.total)}</b> artefaktów · {formatBytes(m.bytes)}</span>
        {KIND_ORDER.filter((k) => m.by_kind[k]).map((k) => (
          <span key={k} className="rounded-md border border-line px-2 py-1" title={`${formatBytes(m.bytes_by_kind[k] ?? 0)}`}>{KIND_LABEL[k]} <b className="font-mono text-fg">{formatCount(m.by_kind[k])}</b></span>
        ))}
        <span className="mx-1 self-center text-subtle" title="liczone jako wystąpienia artefaktu w incydencie (ten sam plik w dwóch incydentach liczy się dwa razy)">korpusy (wystąpienia w incydentach):</span>
        {Object.entries(m.by_corpus).sort((a, b) => b[1] - a[1]).map(([c, n]) => (
          <span key={c} className="rounded-md border border-line px-2 py-1" title={Object.entries(m.kind_by_corpus[c] ?? {}).map(([k, v]) => `${KIND_LABEL[k as ArtifactKind] ?? k}: ${v}`).join(", ")}>
            <span className="font-mono">{c}</span> <b className="font-mono text-fg">{formatCount(n)}</b>
          </span>
        ))}
      </div>
      {m.recent.length > 0 && (
        <div className="flex gap-2 overflow-x-auto pb-1" aria-label="Najnowsze materiały">
          {m.recent.map((r) => (
            <Link key={r.artifact_id} to={`/incident/${encodeURIComponent(r.incident_id)}?art=${r.artifact_id}`} title={`${r.path_in_incident}\n${r.mime} · ${formatBytes(r.size)}`} className="w-28 shrink-0 rounded-md border border-line p-1 hover:border-accent">
              <span className="relative block">
                <img src={thumbUrl(r.artifact_id, 160)} alt={`${r.incident_id}: ${ROLE_LABEL[r.role] ?? r.role}`} loading="lazy" className="h-20 w-full rounded-[3px] bg-raised object-cover" />
                {r.kind === "video" && <span className="absolute right-1 bottom-1 rounded-[2px] bg-fg/80 px-1 text-2xs text-canvas">▶</span>}
              </span>
              <span className="mt-0.5 block truncate font-mono text-2xs text-fg">{r.incident_id}</span>
              <span className="block truncate text-2xs text-subtle">{ROLE_LABEL[r.role] ?? r.role}</span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
