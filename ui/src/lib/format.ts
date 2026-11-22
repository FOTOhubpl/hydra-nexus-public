const oneDecimal = new Intl.NumberFormat("pl-PL", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/** 1536 → "1,5 KB" (Polish decimal comma). Binary multiples, one decimal above bytes. */
export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value < 0) return "—";
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < BYTE_UNITS.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${size} ${BYTE_UNITS[0]}` : `${oneDecimal.format(size)} ${BYTE_UNITS[unit]}`;
}

const integerFormat = new Intl.NumberFormat("pl-PL");

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return integerFormat.format(value);
}

/** 0.4231 → "42%". Accepts fractions (0..1) or percentages (>1). */
export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const pct = value > 1 ? value : value * 100;
  return `${Math.round(Math.max(0, Math.min(100, pct)))}%`;
}

export function formatTime(value: string | number | Date | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Middle-truncate long identifiers: "abcdef…uvwxyz". */
export function truncateMiddle(value: string, max = 24): string {
  if (value.length <= max) return value;
  const keep = Math.max(2, Math.floor((max - 1) / 2));
  return `${value.slice(0, keep)}…${value.slice(-keep)}`;
}

/** Render an unknown JSON value as a compact single-line string. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
