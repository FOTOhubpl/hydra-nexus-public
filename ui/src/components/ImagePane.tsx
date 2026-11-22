import { useEffect, useState, type ReactNode } from "react";
import { cx } from "../lib/cx";
import { EmptyState } from "./EmptyState";

interface OverlayBase {
  id?: string;
  /** Stroke colour; defaults to the accent colour. */
  color?: string;
  /** Stroke width in screen pixels (does not scale with the image). */
  width?: number;
  dashed?: boolean;
  /** Short text drawn next to the shape. */
  label?: string;
  /** Free-form data handed back on click. */
  data?: unknown;
}

export interface LineOverlay extends OverlayBase {
  type: "line";
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface BoxOverlay extends OverlayBase {
  type: "box";
  x: number;
  y: number;
  w: number;
  h: number;
  /** Fill colour (use an alpha colour); default none. */
  fill?: string;
}

export interface PointOverlay extends OverlayBase {
  type: "point";
  x: number;
  y: number;
  /** Radius in screen pixels. */
  r?: number;
}

export interface PolylineOverlay extends OverlayBase {
  type: "polyline";
  points: ReadonlyArray<readonly [number, number]>;
  closed?: boolean;
  fill?: string;
}

export type Overlay = LineOverlay | BoxOverlay | PointOverlay | PolylineOverlay;

export interface ImagePaneProps {
  src: string;
  alt: string;
  overlays?: readonly Overlay[];
  /**
   * "pixel": overlay coordinates are natural image pixels (default).
   * "normalized": coordinates are fractions 0..1 of width/height.
   */
  coordinateSpace?: "pixel" | "normalized";
  /** Controlled overlay visibility; omit to let the built-in toggle manage it. */
  showOverlays?: boolean;
  onOverlayClick?: (overlay: Overlay, index: number) => void;
  /** Hide the small toolbar (dimensions, overlay toggle). */
  bare?: boolean;
  /** Extra content for the toolbar's right side. */
  toolbarExtra?: ReactNode;
  className?: string;
}

const DEFAULT_COLOR = "#4c9ffe";

function anchorOf(overlay: Overlay): [number, number] {
  switch (overlay.type) {
    case "line":
      return [Math.min(overlay.x1, overlay.x2), Math.min(overlay.y1, overlay.y2)];
    case "box":
      return [overlay.x, overlay.y];
    case "point":
      return [overlay.x, overlay.y];
    case "polyline": {
      const first = overlay.points[0];
      return first ? [first[0], first[1]] : [0, 0];
    }
  }
}

/**
 * An image with an SVG annotation layer on top. The SVG shares the image's
 * box and coordinate system (viewBox = natural size, or 0..1), so overlays
 * stay registered at any display size. Strokes do not scale.
 */
export function ImagePane({
  src,
  alt,
  overlays = [],
  coordinateSpace = "pixel",
  showOverlays,
  onOverlayClick,
  bare = false,
  toolbarExtra,
  className,
}: ImagePaneProps) {
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [failed, setFailed] = useState(false);
  const [internalVisible, setInternalVisible] = useState(true);
  const visible = showOverlays ?? internalVisible;

  useEffect(() => {
    setNatural(null);
    setFailed(false);
  }, [src]);

  const vbW = coordinateSpace === "normalized" ? 1 : (natural?.w ?? 1);
  const vbH = coordinateSpace === "normalized" ? 1 : (natural?.h ?? 1);

  const renderShape = (overlay: Overlay, index: number) => {
    const color = overlay.color ?? DEFAULT_COLOR;
    const common = {
      stroke: color,
      strokeWidth: overlay.width ?? 2,
      strokeDasharray: overlay.dashed ? "6 4" : undefined,
      vectorEffect: "non-scaling-stroke" as const,
      style: onOverlayClick ? { cursor: "pointer", pointerEvents: "all" as const } : undefined,
      onClick: onOverlayClick ? () => onOverlayClick(overlay, index) : undefined,
      "data-overlay": overlay.type,
    };
    const key = overlay.id ?? `${overlay.type}-${index}`;
    switch (overlay.type) {
      case "line":
        return <line key={key} x1={overlay.x1} y1={overlay.y1} x2={overlay.x2} y2={overlay.y2} fill="none" {...common} />;
      case "box":
        return <rect key={key} x={overlay.x} y={overlay.y} width={overlay.w} height={overlay.h} fill={overlay.fill ?? "none"} {...common} />;
      case "point":
        // Zero-length round-capped line: a dot of constant screen size under non-scaling stroke.
        return (
          <line
            key={key}
            x1={overlay.x}
            y1={overlay.y}
            x2={overlay.x}
            y2={overlay.y}
            {...common}
            strokeWidth={(overlay.r ?? 4) * 2}
            strokeLinecap="round"
          />
        );
      case "polyline": {
        const points = overlay.points.map(([x, y]) => `${x},${y}`).join(" ");
        return overlay.closed ? (
          <polygon key={key} points={points} fill={overlay.fill ?? "none"} {...common} />
        ) : (
          <polyline key={key} points={points} fill="none" {...common} />
        );
      }
    }
  };

  return (
    <div className={cx("flex min-h-0 min-w-0 flex-col", className)}>
      {!bare && (
        <div className="flex h-8 shrink-0 items-center justify-between gap-2 border-b border-line px-2 text-xs text-muted">
          <span className="font-mono">{natural ? `${natural.w} × ${natural.h}px` : failed ? "niedostępny" : "wczytywanie…"}</span>
          <div className="flex items-center gap-2">
            {toolbarExtra}
            {overlays.length > 0 && showOverlays === undefined && (
              <label className="flex cursor-pointer items-center gap-1.5 select-none">
                <input
                  type="checkbox"
                  className="accent-accent"
                  checked={internalVisible}
                  onChange={(event) => setInternalVisible(event.target.checked)}
                />
                Nakładki ({overlays.length})
              </label>
            )}
          </div>
        </div>
      )}
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-canvas p-2">
        {failed ? (
          <EmptyState compact tone="neutral" title="Obraz niedostępny" description="Nie udało się wczytać obrazu." detail={src} />
        ) : (
          <div className="relative inline-block max-h-full max-w-full leading-none">
            <img
              src={src}
              alt={alt}
              draggable={false}
              onLoad={(event) => setNatural({ w: event.currentTarget.naturalWidth, h: event.currentTarget.naturalHeight })}
              onError={() => setFailed(true)}
              className="block max-h-full max-w-full object-contain select-none"
            />
            {natural && visible && overlays.length > 0 && (
              <>
                <svg
                  className="pointer-events-none absolute inset-0 h-full w-full overflow-visible"
                  viewBox={`0 0 ${vbW} ${vbH}`}
                  preserveAspectRatio="none"
                  role="img"
                  aria-label={`Adnotacje na obrazie: ${overlays.length}`}
                >
                  {overlays.map(renderShape)}
                </svg>
                {overlays.map((overlay, index) => {
                  if (!overlay.label) return null;
                  const [ax, ay] = anchorOf(overlay);
                  return (
                    <span
                      key={`label-${overlay.id ?? index}`}
                      className="pointer-events-none absolute -translate-y-full rounded-[2px] px-1 font-mono text-2xs leading-4 whitespace-nowrap text-canvas"
                      style={{
                        left: `${(ax / vbW) * 100}%`,
                        top: `${(ay / vbH) * 100}%`,
                        backgroundColor: overlay.color ?? DEFAULT_COLOR,
                      }}
                    >
                      {overlay.label}
                    </span>
                  );
                })}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
