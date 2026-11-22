import { useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { cx } from "../lib/cx";
import { SkeletonBlock } from "./Skeleton";

export type SortDirection = "asc" | "desc";

export interface SortState {
  key: string;
  direction: SortDirection;
}

export interface Column<T> {
  /** Unique column key; also the default property read for display and sorting. */
  key: string;
  header: ReactNode;
  /** Value used for sorting (and default display). Defaults to `row[key]`. */
  accessor?: (row: T) => unknown;
  /** Custom cell renderer. Defaults to the accessor value as text. */
  render?: (row: T) => ReactNode;
  sortable?: boolean;
  align?: "left" | "right" | "center";
  /** CSS width, e.g. "120px" or "20%". */
  width?: string;
  mono?: boolean;
  /** Tooltip for the header cell. */
  title?: string;
}

export interface DataTableProps<T> {
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  /** Uncontrolled initial sort. */
  initialSort?: SortState | null;
  /** Controlled sort (pair with onSortChange). */
  sort?: SortState | null;
  onSortChange?: (sort: SortState) => void;
  onRowClick?: (row: T) => void;
  /** Keys of highlighted (selected) rows. */
  selectedKeys?: ReadonlySet<string>;
  /** Rendered in place of the body when there are no rows. */
  empty?: ReactNode;
  loading?: boolean;
  caption?: string;
  className?: string;
  /** Sticky header inside a scrolling parent. */
  stickyHeader?: boolean;
}

function readValue<T>(row: T, column: Column<T>): unknown {
  if (column.accessor) return column.accessor(row);
  return (row as Record<string, unknown>)[column.key];
}

function rank(value: unknown): number {
  if (value === null || value === undefined || value === "" || (typeof value === "number" && Number.isNaN(value))) return 3;
  if (typeof value === "number" || typeof value === "bigint" || value instanceof Date || typeof value === "boolean") return 0;
  return 1;
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

/** Total order for mixed cell values. Empty values always sort last (both directions). */
export function compareValues(a: unknown, b: unknown): number {
  const ra = rank(a);
  const rb = rank(b);
  if (ra !== rb) return ra - rb;
  if (ra === 3) return 0;
  if (a instanceof Date && b instanceof Date) return a.getTime() - b.getTime();
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  if (typeof a === "bigint" && typeof b === "bigint") return a < b ? -1 : a > b ? 1 : 0;
  return collator.compare(String(a), String(b));
}

export function sortRows<T>(rows: readonly T[], columns: readonly Column<T>[], sort: SortState | null | undefined): T[] {
  const copy = rows.slice();
  if (!sort) return copy;
  const column = columns.find((c) => c.key === sort.key);
  if (!column) return copy;
  const factor = sort.direction === "asc" ? 1 : -1;
  // Array.prototype.sort is stable, so ties keep their incoming order.
  return copy.sort((ra, rb) => {
    const a = readValue(ra, column);
    const b = readValue(rb, column);
    const emptyA = rank(a) === 3;
    const emptyB = rank(b) === 3;
    if (emptyA || emptyB) return Number(emptyA) - Number(emptyB);
    return compareValues(a, b) * factor;
  });
}

function nextSort(current: SortState | null | undefined, key: string): SortState {
  if (current?.key === key) return { key, direction: current.direction === "asc" ? "desc" : "asc" };
  return { key, direction: "asc" };
}

function defaultCell(value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") return <span className="text-subtle">—</span>;
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

const ALIGN = { left: "text-left", right: "text-right", center: "text-center" } as const;

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  initialSort = null,
  sort: controlledSort,
  onSortChange,
  onRowClick,
  selectedKeys,
  empty,
  loading = false,
  caption,
  className,
  stickyHeader = true,
}: DataTableProps<T>) {
  const [internalSort, setInternalSort] = useState<SortState | null>(initialSort);
  const sort = controlledSort !== undefined ? controlledSort : internalSort;
  const sorted = useMemo(() => sortRows(rows, columns, sort), [rows, columns, sort]);

  const toggleSort = (key: string) => {
    const next = nextSort(sort, key);
    if (controlledSort === undefined) setInternalSort(next);
    onSortChange?.(next);
  };

  const onRowKeyDown = (event: KeyboardEvent<HTMLTableRowElement>, row: T) => {
    if (!onRowClick) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onRowClick(row);
    }
  };

  return (
    <div className={cx("min-w-0 overflow-auto overscroll-x-contain", className)}>
      {/* phones: the table keeps a readable width and scrolls inside its container, never the page */}
      <table className="w-full min-w-[44rem] border-collapse text-sm tabular-nums sm:min-w-0" aria-busy={loading || undefined}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead className={cx(stickyHeader && "sticky top-0 z-10")}>
          <tr className="table-head-blur">
            {columns.map((column) => {
              const sortable = column.sortable !== false;
              const active = sort?.key === column.key;
              const ariaSort = active ? (sort?.direction === "asc" ? "ascending" : "descending") : sortable ? "none" : undefined;
              return (
                <th
                  key={column.key}
                  scope="col"
                  aria-sort={ariaSort}
                  title={column.title}
                  style={column.width ? { width: column.width } : undefined}
                  className={cx(
                    "h-9 border-b border-line px-3 text-2xs font-semibold tracking-wider whitespace-nowrap text-subtle uppercase first:pl-4 last:pr-4",
                    ALIGN[column.align ?? "left"],
                  )}
                >
                  {sortable ? (
                    <button
                      type="button"
                      onClick={() => toggleSort(column.key)}
                      className={cx(
                        "inline-flex items-center gap-1 rounded-sm uppercase transition-colors hover:text-fg",
                        active && "text-fg",
                        column.align === "right" && "flex-row-reverse",
                      )}
                    >
                      <span>{column.header}</span>
                      <span aria-hidden="true" className={cx("font-mono text-2xs", active ? "text-accent" : "text-line-strong")}>
                        {active ? (sort?.direction === "asc" ? "▲" : "▼") : "↕"}
                      </span>
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="p-0">
                {loading ? (
                  <div className="space-y-2 px-4 py-3">
                    {Array.from({ length: 5 }, (_, i) => (
                      <SkeletonBlock key={i} className={cx("h-6", i === 4 ? "w-2/3" : "w-full")} />
                    ))}
                  </div>
                ) : (
                  (empty ?? <div className="px-4 py-8 text-center text-sm text-subtle">Brak wierszy</div>)
                )}
              </td>
            </tr>
          ) : (
            sorted.map((row) => {
              const key = rowKey(row);
              const selected = selectedKeys?.has(key) ?? false;
              return (
                <tr
                  key={key}
                  data-key={key}
                  aria-selected={selectedKeys ? selected : undefined}
                  tabIndex={onRowClick ? 0 : undefined}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  onKeyDown={onRowClick ? (event) => onRowKeyDown(event, row) : undefined}
                  className={cx(
                    "h-10 border-b border-line/60 transition-colors last:border-b-0 hover:bg-hover/70",
                    onRowClick && "cursor-pointer hover:bg-hover",
                    selected && "bg-accent-soft hover:bg-accent-soft",
                  )}
                >
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      className={cx(
                        "max-w-0 truncate px-3 whitespace-nowrap first:pl-4 last:pr-4",
                        ALIGN[column.align ?? "left"],
                        column.mono && "font-mono text-xs",
                        column.align === "right" && "tabular-nums",
                      )}
                    >
                      {column.render ? column.render(row) : defaultCell(readValue(row, column))}
                    </td>
                  ))}
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
