/** Matched keypoints / inlier lines of the pair's most similar images (ORB/RootSIFT + MAGSAC++, hydra/api_geom.py). */
import { thumbUrl } from "../api/artifacts";
import { usePairGeometry, type GeometryPair } from "../api/geometry";
import { plNumber } from "../lib/evidence";

const W = 220; // drawn width of each image
const GAP = 16;

function aspect(size: [number, number] | null): number {
  return size && size[0] > 0 ? size[1] / size[0] : 1;
}

export function GeometryPairView({ p }: { p: GeometryPair }) {
  const ha = Math.round(W * Math.min(2.2, aspect(p.source_size_a)));
  const hb = Math.round(W * Math.min(2.2, aspect(p.source_size_b)));
  const h = Math.max(ha, hb);
  const lines = p.points.slice(0, 150);
  const verdict = p.crop_match ? "kadr / zmiana rozmiaru tego samego obrazu" : p.same_asset ? "ten sam zasób graficzny" : "brak potwierdzenia geometrycznego";
  return (
    <figure className="space-y-2">
      <svg viewBox={`0 0 ${2 * W + GAP} ${h}`} width="100%" role="img" aria-label={`Dopasowane punkty: ${p.inliers} zgodnych par punktów, ${verdict}`} className="max-w-[460px] rounded-md border border-line bg-hover">
        <image href={thumbUrl(p.artifact_a, 480)} x={0} y={0} width={W} height={ha} preserveAspectRatio="none" />
        <image href={thumbUrl(p.artifact_b, 480)} x={W + GAP} y={0} width={W} height={hb} preserveAspectRatio="none" />
        {p.bbox_a && p.source_size_a && <rect x={(p.bbox_a[0] / p.source_size_a[0]) * W} y={(p.bbox_a[1] / p.source_size_a[1]) * ha} width={(p.bbox_a[2] / p.source_size_a[0]) * W} height={(p.bbox_a[3] / p.source_size_a[1]) * ha} fill="none" stroke="var(--color-accent)" strokeWidth={1.5} />}
        {p.bbox_b && p.source_size_b && <rect x={W + GAP + (p.bbox_b[0] / p.source_size_b[0]) * W} y={(p.bbox_b[1] / p.source_size_b[1]) * hb} width={(p.bbox_b[2] / p.source_size_b[0]) * W} height={(p.bbox_b[3] / p.source_size_b[1]) * hb} fill="none" stroke="var(--color-accent)" strokeWidth={1.5} />}
        <g stroke="var(--color-for)" strokeOpacity={0.55} strokeWidth={0.8} data-testid="geometry-lines">
          {lines.map(([xa, ya, xb, yb], i) => <line key={i} x1={xa * W} y1={ya * ha} x2={W + GAP + xb * W} y2={yb * hb} />)}
        </g>
        <g fill="var(--color-for)">
          {lines.map(([xa, ya, xb, yb], i) => <g key={i}><circle cx={xa * W} cy={ya * ha} r={1.6} /><circle cx={W + GAP + xb * W} cy={yb * hb} r={1.6} /></g>)}
        </g>
      </svg>
      <figcaption className="text-xs text-muted">
        <span className="font-semibold text-fg">{verdict}</span>: {p.inliers} zgodnych punktów ({p.method.toUpperCase()} + MAGSAC++), pokrycie {plNumber(p.coverage_a, 2)} / {plNumber(p.coverage_b, 2)}, DINOv2 cos {plNumber(p.dinov2_cos, 3)}{p.mirrored ? ", odbicie lustrzane" : ""}{p.anisotropy !== null ? `, anizotropia ${plNumber(p.anisotropy, 2)}` : ""}
      </figcaption>
    </figure>
  );
}

export function GeometryOverlay({ a, b }: { a: string; b: string }) {
  const q = usePairGeometry(a, b);
  if (q.isError) return null; // older API without the endpoint: the rest of the page stays usable
  if (!q.data) return <div className="text-xs text-subtle">weryfikacja geometryczna…</div>;
  const shown = q.data.pairs.filter((p) => p.inliers > 0);
  if (!shown.length) return <div className="text-xs text-subtle">Weryfikacja geometryczna: brak wspólnych punktów kluczowych między najbardziej podobnymi obrazami.</div>;
  return (
    <div className="space-y-4" data-testid="geometry-overlay">
      {shown.map((p) => <GeometryPairView key={`${p.unit_a}|${p.unit_b}`} p={p} />)}
      <p className="text-2xs text-subtle">{q.data.note_pl}</p>
    </div>
  );
}
