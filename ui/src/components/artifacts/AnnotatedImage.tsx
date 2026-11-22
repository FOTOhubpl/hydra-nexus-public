import { useCallback, useState } from "react";
import { cx } from "../../lib/cx";
import { LAYER_COLOR, LAYER_LABEL, type BoxMark, type LayerKey } from "../../lib/artifactEvidence";

export interface AnnotatedImageProps {
  src: string;
  alt: string;
  marks?: readonly BoxMark[];
  /** Coordinate space of the marks (source pixels). Defaults to the natural size of the loaded image. */
  space?: { w: number; h: number } | null;
  focusKey?: string | null;
  onMark?: (m: BoxMark) => void;
  /** Show the layer toggles and zoom bar. */
  controls?: boolean;
  maxHeight?: string;
  className?: string;
}

const ZOOMS = [["fit", "dopasuj"], [1, "100%"], [2, "200%"]] as const;

/**
 * Image with bbox layers drawn from stored evidence pointers (OCR lines, logos, faces, regions) and a zoom bar.
 * The SVG shares the image box and uses the source coordinate space, so boxes stay registered at any zoom.
 */
export function AnnotatedImage({ src, alt, marks = [], space, focusKey, onMark, controls = true, maxHeight = "70vh", className }: AnnotatedImageProps) {
  // Size and failure are keyed by src: a new src never shows the previous image's state, and no reset effect can
  // race (and clobber) the load of an image that is already complete from the memory cache.
  const [loaded, setLoaded] = useState<{ src: string; w: number; h: number } | null>(null);
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const [zoom, setZoom] = useState<"fit" | number>("fit");
  const [hidden, setHidden] = useState<Set<LayerKey>>(new Set());
  const natural = loaded && loaded.src === src ? { w: loaded.w, h: loaded.h } : null;
  const failed = failedSrc === src;
  const measure = useCallback((img: HTMLImageElement | null) => {
    // onLoad may not fire for an image that is already complete (memory cache) when the listener is attached
    if (img && img.complete && img.naturalWidth > 0) setLoaded({ src, w: img.naturalWidth, h: img.naturalHeight });
  }, [src]);

  const vb = space ?? natural;
  const layers = [...new Set(marks.map((m) => m.layer))];
  const shown = marks.filter((m) => !hidden.has(m.layer));
  const toggle = (l: LayerKey) => setHidden((h) => { const n = new Set(h); if (n.has(l)) n.delete(l); else n.add(l); return n; });
  const width = zoom === "fit" || !natural ? undefined : natural.w * zoom;

  return (
    <div className={cx("flex min-w-0 flex-col", className)}>
      {controls && (
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-2 py-1 text-xs text-muted">
          <span className="font-mono" data-testid="image-size">{natural ? `${natural.w} × ${natural.h} px` : failed ? "obraz niedostępny" : "ładowanie…"}</span>
          <span className="flex overflow-hidden rounded-[4px] border border-line" role="group" aria-label="Powiększenie">
            {ZOOMS.map(([z, label]) => (
              <button key={String(z)} type="button" aria-pressed={zoom === z} onClick={() => setZoom(z)} className={cx("px-2 py-0.5", zoom === z ? "bg-active text-fg" : "hover:bg-hover")}>{label}</button>
            ))}
          </span>
          {layers.map((l) => (
            <label key={l} className="inline-flex cursor-pointer items-center gap-1 select-none">
              <input type="checkbox" checked={!hidden.has(l)} onChange={() => toggle(l)} className="accent-accent" />
              <span className="inline-block size-2.5 rounded-[2px]" style={{ backgroundColor: LAYER_COLOR[l] }} />
              {LAYER_LABEL[l]} ({marks.filter((m) => m.layer === l).length})
            </label>
          ))}
          {marks.length === 0 && <span className="text-subtle">brak ramek w zapisanych dowodach</span>}
        </div>
      )}
      <div className="overflow-auto bg-canvas p-2" style={{ maxHeight }}>
        {failed ? (
          <div className="p-6 text-center text-xs text-subtle">Nie udało się wczytać obrazu.</div>
        ) : (
          <div className={cx("relative mx-auto leading-none", zoom === "fit" ? "w-fit max-w-full" : "")} style={width ? { width } : undefined}>
            <img
              ref={measure} src={src} alt={alt} draggable={false}
              onLoad={(e) => setLoaded({ src, w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
              onError={() => setFailedSrc(src)}
              className={cx("block select-none", zoom === "fit" ? "max-w-full object-contain" : "w-full")}
              style={zoom === "fit" ? { maxHeight: `calc(${maxHeight} - 1rem)` } : undefined}
            />
            {vb && shown.length > 0 && (
              <svg className="absolute inset-0 h-full w-full" viewBox={`0 0 ${vb.w} ${vb.h}`} preserveAspectRatio="none" role="img" aria-label={`${shown.length} ramek dowodów`}>
                {shown.map((m) => {
                  const focused = focusKey === m.key;
                  return (
                    <rect key={m.key} x={m.bbox[0]} y={m.bbox[1]} width={m.bbox[2]} height={m.bbox[3]}
                      fill={focused ? "rgba(227,73,72,0.18)" : "transparent"} stroke={focused ? "var(--color-against)" : LAYER_COLOR[m.layer]}
                      strokeWidth={focused ? 3 : 1.5} vectorEffect="non-scaling-stroke" data-mark={m.layer}
                      style={{ cursor: onMark ? "pointer" : undefined, pointerEvents: "all" }} onClick={onMark ? () => onMark(m) : undefined}>
                      <title>{`${LAYER_LABEL[m.layer]}: ${m.label}${m.genes.length > 1 ? ` (+${m.genes.length - 1} genów)` : ""}`}</title>
                    </rect>
                  );
                })}
              </svg>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
