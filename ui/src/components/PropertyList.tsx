import { cx } from "../lib/cx";
import { formatValue } from "../lib/format";

export interface PropertyListProps {
  data: Record<string, unknown>;
  /** Keys rendered first, in this order; the rest follow alphabetically. */
  order?: readonly string[];
  /** Keys to leave out. */
  omit?: readonly string[];
  /** When given, keys whose value differs from this record are marked. */
  compareTo?: Record<string, unknown> | null;
  className?: string;
}

function sameValue(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

/** Generic key/value listing for any JSON object; values are shown verbatim, never interpreted. */
export function PropertyList({ data, order = [], omit = [], compareTo, className }: PropertyListProps) {
  const skip = new Set(omit);
  const seen = new Set<string>();
  const keys: string[] = [];
  for (const key of order) {
    if (key in data && !skip.has(key)) {
      keys.push(key);
      seen.add(key);
    }
  }
  for (const key of Object.keys(data).sort()) {
    if (!seen.has(key) && !skip.has(key)) keys.push(key);
  }
  if (compareTo) {
    for (const key of Object.keys(compareTo).sort()) {
      if (!(key in data) && !skip.has(key)) keys.push(key);
    }
  }

  if (keys.length === 0) return <div className="px-3 py-4 text-sm text-subtle">Brak właściwości</div>;

  return (
    <dl className={cx("grid grid-cols-[minmax(96px,max-content)_minmax(0,1fr)] text-sm", className)}>
      {keys.map((key) => {
        const present = key in data;
        const differs = compareTo ? !sameValue(data[key], compareTo[key]) : false;
        const value = data[key];
        const multiline = typeof value === "object" && value !== null;
        return (
          <div key={key} className={cx("contents", differs && "[&>*]:bg-warn/5")} data-differs={differs || undefined}>
            <dt className={cx("border-b border-line/70 px-3 py-1.5 font-mono text-xs whitespace-nowrap text-subtle", differs && "border-l-2 border-l-warn")}>
              {key}
            </dt>
            <dd className="m-0 min-w-0 border-b border-line/70 px-3 py-1.5 font-mono text-xs text-fg">
              {!present ? (
                <span className="text-subtle italic">brak</span>
              ) : multiline ? (
                <pre className="m-0 max-h-48 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(value, null, 2)}</pre>
              ) : (
                <span className="break-all">{formatValue(value)}</span>
              )}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}
