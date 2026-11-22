import { useState } from "react";
import { kindOf, KIND_LABEL, thumbUrl, type ArtifactKind } from "../../api/artifacts";
import type { IncidentArtifactRow } from "../../api/task";
import { cx } from "../../lib/cx";
import { ROLE_LABEL } from "../../lib/evidence";
import { formatBytes } from "../../lib/format";

const COLLAPSED_ROLES = new Set(["landing_asset"]);
const KIND_ICON: Record<ArtifactKind, string> = { image: "IMG", video: "▶ WIDEO", audio: "♪ AUDIO", html: "</> HTML", har: "HAR", json: "{ } JSON", text: "TXT", other: "?" };
const ROLE_ORDER = ["ad_creative", "ad_video", "feed_screenshot", "screen_recording", "landing_screenshot", "landing_html", "har", "platform_metadata", "text", "url", "landing_asset", "other"];

export function artifactKind(a: Pick<IncidentArtifactRow, "mime" | "path_in_incident" | "role">): ArtifactKind {
  return kindOf(a.mime, a.path_in_incident, a.role);
}

/** First artefact worth opening: the ad itself, then recordings and screenshots, then the landing page. */
export function defaultArtifact(arts: readonly IncidentArtifactRow[]): IncidentArtifactRow | undefined {
  return [...arts].sort((x, y) => ROLE_ORDER.indexOf(x.role) - ROLE_ORDER.indexOf(y.role))[0];
}

function Tile({ a, selected, onSelect }: { a: IncidentArtifactRow; selected: boolean; onSelect: () => void }) {
  const kind = artifactKind(a);
  const [broken, setBroken] = useState(false);
  const name = a.path_in_incident.split("/").pop() ?? a.path_in_incident;
  return (
    <button type="button" onClick={onSelect} aria-pressed={selected} data-testid="artifact-tile" data-path={a.path_in_incident} title={`${a.path_in_incident}\n${a.mime} · ${formatBytes(a.size)}\nsha256 ${a.artifact_id}`}
      className={cx("flex w-36 flex-col gap-1 rounded-md border p-1.5 text-left", selected ? "border-accent ring-2 ring-accent/40" : "border-line hover:border-line-strong")}>
      {(kind === "image" || kind === "video") && !broken ? (
        <span className="relative block">
          <img src={thumbUrl(a.artifact_id, 320)} alt={name} loading="lazy" onError={() => setBroken(true)} className="h-20 w-full rounded-[3px] bg-raised object-cover" />
          {kind === "video" && <span className="absolute right-1 bottom-1 rounded-[2px] bg-fg/80 px-1 text-2xs text-canvas">▶</span>}
        </span>
      ) : (
        <span className="flex h-20 items-center justify-center rounded-[3px] bg-raised font-mono text-xs text-muted">{KIND_ICON[kind]}</span>
      )}
      <span className="truncate font-mono text-2xs text-fg">{name}</span>
      <span className="flex justify-between text-2xs text-subtle"><span className="truncate">{a.mime}</span><span className="shrink-0">{formatBytes(a.size)}</span></span>
    </button>
  );
}

function RoleGroup({ role, arts, selected, onSelect }: { role: string; arts: IncidentArtifactRow[]; selected: string | null; onSelect: (a: IncidentArtifactRow) => void }) {
  const [open, setOpen] = useState(!COLLAPSED_ROLES.has(role) || arts.some((a) => a.artifact_id === selected));
  const [all, setAll] = useState(false);
  const shown = all ? arts : arts.slice(0, 18);
  const kinds = arts.reduce<Record<string, number>>((acc, a) => ({ ...acc, [artifactKind(a)]: (acc[artifactKind(a)] ?? 0) + 1 }), {});
  return (
    <div>
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="mb-1 text-xs font-semibold text-muted hover:text-fg">
        {open ? "▾" : "▸"} {ROLE_LABEL[role] ?? role} <span className="font-normal text-subtle">({arts.length}: {Object.entries(kinds).map(([k, n]) => `${KIND_LABEL[k as ArtifactKind]} ${n}`).join(", ")})</span>
      </button>
      {open && (
        <div className="flex flex-wrap gap-2">
          {shown.map((a) => <Tile key={a.artifact_id + a.path_in_incident} a={a} selected={a.artifact_id === selected} onSelect={() => onSelect(a)} />)}
          {arts.length > shown.length && <button type="button" onClick={() => setAll(true)} className="self-center text-xs text-accent hover:underline">pokaż wszystkie ({arts.length})</button>}
        </div>
      )}
    </div>
  );
}

/** Every artefact of an incident grouped by role, with real thumbnails (images, video frames) and type tiles. */
export function ArtifactGallery({ artifacts, selected, onSelect }: { artifacts: readonly IncidentArtifactRow[]; selected: string | null; onSelect: (a: IncidentArtifactRow) => void }) {
  const byRole = new Map<string, IncidentArtifactRow[]>();
  for (const a of artifacts) byRole.set(a.role, [...(byRole.get(a.role) ?? []), a]);
  const roles = [...byRole.keys()].sort((x, y) => (ROLE_ORDER.indexOf(x) + 99) % 99 - (ROLE_ORDER.indexOf(y) + 99) % 99);
  return (
    <div className="space-y-3">
      {roles.map((r) => <RoleGroup key={r} role={r} arts={byRole.get(r)!} selected={selected} onSelect={onSelect} />)}
    </div>
  );
}
