import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { Badge } from "../components/Badge";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { Panel } from "../components/Panel";
import { StatTile } from "../components/StatTile";
import { formatBytes } from "../lib/format";
import { statusVariant } from "../lib/status";
import { ArtifactLoader, ArtifactPreview, ArtifactProperties } from "./ArtifactView";

function CompareWith({ id }: { id: string }) {
  const [other, setOther] = useState("");
  const navigate = useNavigate();
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const target = other.trim();
    if (target) navigate(`/detail/${encodeURIComponent(id)}/${encodeURIComponent(target)}`);
  };
  return (
    <form onSubmit={onSubmit} className="flex items-center gap-2">
      <input
        value={other}
        onChange={(event) => setOther(event.target.value)}
        placeholder="Porównaj z identyfikatorem…"
        aria-label="Identyfikator elementu do porównania"
        className="h-6 w-44 rounded-[4px] border border-line bg-canvas px-2 font-mono text-xs text-fg placeholder:font-sans placeholder:text-subtle focus:border-accent focus:outline-none"
      />
      <Button size="sm" type="submit" disabled={!other.trim()}>
        Porównaj
      </Button>
    </form>
  );
}

/** Evidence pointer from the URL (provenance jump): `?bbox=x,y,w,h` is drawn on the image; others are listed. */
function useEvidenceOverlay() {
  const [sp] = useSearchParams();
  const bbox = (sp.get("bbox") ?? "").split(",").map(Number);
  const overlays = bbox.length === 4 && bbox.every(Number.isFinite)
    ? [{ type: "box" as const, x: bbox[0]!, y: bbox[1]!, w: bbox[2]!, h: bbox[3]!, color: "#e34948", fill: "rgba(227,73,72,0.12)", label: sp.get("label") ?? "dowód" }]
    : undefined;
  const pointer = ["har", "chars", "dom", "t"].filter((k) => sp.get(k)).map((k) => `${k}=${sp.get(k)}`).join(" · ");
  return { overlays, pointer, label: sp.get("label") };
}

export function ItemPage() {
  const evidence = useEvidenceOverlay();
  const { id = "" } = useParams();
  if (!id) return <EmptyState title="Nie wybrano elementu" />;

  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <Link to="/" className="hover:text-fg">
          Pulpit
        </Link>
        <span aria-hidden="true">/</span>
        <span className="font-mono break-all text-fg">{id}</span>
        <div className="ml-auto">
          <CompareWith id={id} />
        </div>
      </div>
      <ArtifactLoader id={id}>
        {(artifact) => (
          <>
            <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
              <StatTile label="Typ" value={<span className="font-mono text-base">{artifact.mime || null}</span>} />
              <StatTile label="Rozmiar" value={formatBytes(artifact.size)} hint={Number.isFinite(artifact.size) ? `${artifact.size} B` : undefined} />
              <StatTile
                label="Status"
                value={
                  <Badge variant={statusVariant(artifact.status)} dot className="align-middle">
                    {artifact.status || "nieznany"}
                  </Badge>
                }
              />
              <StatTile label="Źródło" value={<span className="font-mono text-sm">{artifact.source_ref || null}</span>} />
            </div>
            <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
              <Panel title="Właściwości" flush>
                <ArtifactProperties artifact={artifact} />
              </Panel>
              <Panel title="Podgląd" flush bodyClassName="flex min-h-[360px] flex-col">
                {(evidence.overlays || evidence.pointer) && (
                  <div className="rounded-md border border-against bg-against-soft px-3 py-1.5 text-xs">wskazany dowód: {evidence.label ?? "gen"}{evidence.overlays ? " · ramka zaznaczona na obrazie" : ""}{evidence.pointer ? ` · ${evidence.pointer}` : ""}</div>
                )}
                <ArtifactPreview artifact={artifact} overlays={evidence.overlays} className="flex-1" />
              </Panel>
            </div>
          </>
        )}
      </ArtifactLoader>
    </div>
  );
}
