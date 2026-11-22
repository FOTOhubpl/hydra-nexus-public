import type { ReactNode } from "react";
import { artifactContentUrl, useArtifact } from "../api/client";
import type { Artifact } from "../api/types";
import { ApiErrorState } from "../components/ApiErrorState";
import { EmptyState } from "../components/EmptyState";
import { ImagePane, type Overlay } from "../components/ImagePane";
import { PropertyList } from "../components/PropertyList";

export const ARTIFACT_FIELD_ORDER = ["id", "mime", "size", "status", "source_ref"] as const;

export function isPreviewableImage(artifact: Pick<Artifact, "mime"> | undefined): boolean {
  return Boolean(artifact?.mime && /^image\/(png|jpe?g|gif|webp|bmp|svg\+xml|avif)$/i.test(artifact.mime));
}

export interface ArtifactPreviewProps {
  artifact: Artifact;
  overlays?: readonly Overlay[];
  className?: string;
}

/** Content preview for types the browser can render natively; everything else gets an honest empty state. */
export function ArtifactPreview({ artifact, overlays, className }: ArtifactPreviewProps) {
  if (isPreviewableImage(artifact)) {
    return <ImagePane src={artifactContentUrl(artifact.id)} alt={`Artefakt ${artifact.id}`} overlays={overlays} className={className} />;
  }
  return (
    <EmptyState
      compact
      title="Brak podglądu w przeglądarce"
      description={artifact.mime ? `Brak podglądu dla typu ${artifact.mime}.` : "Artefakt nie ma typu MIME."}
      className={className}
    />
  );
}

/**
 * Loads one artifact and hands it to `children`, or renders the matching
 * loading / offline / not-implemented / error state.
 */
export function ArtifactLoader({ id, children }: { id: string; children: (artifact: Artifact) => ReactNode }) {
  const query = useArtifact(id);
  if (query.isPending) {
    return (
      <div className="space-y-2 p-3" aria-busy="true">
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="h-6 animate-pulse rounded-sm bg-raised" />
        ))}
      </div>
    );
  }
  if (query.isError) {
    return <ApiErrorState error={query.error} resource={`element ${id}`} endpoint={`GET /v1/artifacts/${id}`} compact />;
  }
  return <>{children(query.data)}</>;
}

export function ArtifactProperties({ artifact, compareTo }: { artifact: Artifact; compareTo?: Artifact | null }) {
  return <PropertyList data={artifact} order={ARTIFACT_FIELD_ORDER} compareTo={compareTo ?? undefined} />;
}
