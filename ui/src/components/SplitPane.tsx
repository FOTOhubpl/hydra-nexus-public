import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { cx } from "../lib/cx";

export interface SplitPaneProps {
  left: ReactNode;
  right: ReactNode;
  /** Header cell above the left column. Both header cells share the column grid, so they always line up. */
  leftHeader?: ReactNode;
  rightHeader?: ReactNode;
  /** Optional full-width row above both column headers (shared toolbar). */
  toolbar?: ReactNode;
  /** Mirror vertical/horizontal scroll between the two panes. */
  syncScroll?: boolean;
  /** Initial share of the left pane, 0..1. */
  initialRatio?: number;
  /** Minimum share for either pane, 0..0.5. */
  minRatio?: number;
  resizable?: boolean;
  className?: string;
  /** Accessible names for the two panes. */
  leftLabel?: string;
  rightLabel?: string;
}

const DIVIDER_PX = 5;

function clampRatio(value: number, min: number): number {
  return Math.min(1 - min, Math.max(min, value));
}

/**
 * Two side-by-side scroll panes with a synchronised header row and a
 * draggable divider. Header and body use the same grid template, so header
 * cells stay aligned with their columns at any split ratio.
 */
export function SplitPane({
  left,
  right,
  leftHeader,
  rightHeader,
  toolbar,
  syncScroll = false,
  initialRatio = 0.5,
  minRatio = 0.2,
  resizable = true,
  className,
  leftLabel = "Lewy panel",
  rightLabel = "Prawy panel",
}: SplitPaneProps) {
  const [ratio, setRatio] = useState(() => clampRatio(initialRatio, minRatio));
  const rootRef = useRef<HTMLDivElement>(null);
  const leftRef = useRef<HTMLDivElement>(null);
  const rightRef = useRef<HTMLDivElement>(null);
  const syncing = useRef<HTMLDivElement | null>(null);
  const dragging = useRef(false);

  const template = `minmax(0, ${ratio}fr) ${DIVIDER_PX}px minmax(0, ${1 - ratio}fr)`;

  const mirror = useCallback(
    (from: HTMLDivElement | null, to: HTMLDivElement | null) => {
      if (!syncScroll || !from || !to) return;
      // Ignore the echo scroll event caused by our own assignment.
      if (syncing.current === to) {
        syncing.current = null;
        return;
      }
      syncing.current = from;
      to.scrollTop = from.scrollTop;
      to.scrollLeft = from.scrollLeft;
    },
    [syncScroll],
  );

  useEffect(() => {
    if (!syncScroll) syncing.current = null;
  }, [syncScroll]);

  const updateFromPointer = (clientX: number) => {
    const root = rootRef.current;
    if (!root) return;
    const rect = root.getBoundingClientRect();
    if (rect.width <= 0) return;
    setRatio(clampRatio((clientX - rect.left) / rect.width, minRatio));
  };

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (!resizable) return;
    dragging.current = true;
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (dragging.current) updateFromPointer(event.clientX);
  };

  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
    dragging.current = false;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const onDividerKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!resizable) return;
    const step = event.shiftKey ? 0.1 : 0.02;
    if (event.key === "ArrowLeft") setRatio((r) => clampRatio(r - step, minRatio));
    else if (event.key === "ArrowRight") setRatio((r) => clampRatio(r + step, minRatio));
    else if (event.key === "Home") setRatio(minRatio);
    else if (event.key === "End") setRatio(1 - minRatio);
    else if (event.key === "Enter") setRatio(0.5);
    else return;
    event.preventDefault();
  };

  const hasHeader = leftHeader !== undefined || rightHeader !== undefined;

  /** The divider is draggable in both rows; only one instance is the focusable, labelled separator. */
  const renderDivider = (accessible: boolean) => (
    <div
      role={accessible ? "separator" : undefined}
      aria-hidden={accessible ? undefined : true}
      aria-orientation={accessible ? "vertical" : undefined}
      aria-valuemin={accessible ? Math.round(minRatio * 100) : undefined}
      aria-valuemax={accessible ? Math.round((1 - minRatio) * 100) : undefined}
      aria-valuenow={accessible ? Math.round(ratio * 100) : undefined}
      aria-label={accessible ? "Zmień szerokość paneli" : undefined}
      tabIndex={accessible && resizable ? 0 : undefined}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onKeyDown={accessible ? onDividerKey : undefined}
      onDoubleClick={() => resizable && setRatio(0.5)}
      className={cx("group relative flex touch-none justify-center bg-canvas", resizable && "cursor-col-resize")}
    >
      <span className="h-full w-px bg-line transition-colors group-hover:bg-accent group-focus-visible:bg-accent" />
    </div>
  );

  return (
    <div ref={rootRef} className={cx("flex min-h-0 min-w-0 flex-col overflow-hidden rounded-md border border-line bg-panel", className)}>
      {toolbar && <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line px-3">{toolbar}</div>}
      {hasHeader && (
        <div className="grid shrink-0 border-b border-line bg-raised" style={{ gridTemplateColumns: template }}>
          <div className="flex h-9 min-w-0 items-center gap-2 px-3">{leftHeader}</div>
          {renderDivider(true)}
          <div className="flex h-9 min-w-0 items-center gap-2 px-3">{rightHeader}</div>
        </div>
      )}
      <div className="grid min-h-0 flex-1" style={{ gridTemplateColumns: template }}>
        <div
          ref={leftRef}
          role="region"
          aria-label={leftLabel}
          onScroll={() => mirror(leftRef.current, rightRef.current)}
          className="min-h-0 min-w-0 overflow-auto"
        >
          {left}
        </div>
        {renderDivider(!hasHeader)}
        <div
          ref={rightRef}
          role="region"
          aria-label={rightLabel}
          onScroll={() => mirror(rightRef.current, leftRef.current)}
          className="min-h-0 min-w-0 overflow-auto"
        >
          {right}
        </div>
      </div>
    </div>
  );
}
