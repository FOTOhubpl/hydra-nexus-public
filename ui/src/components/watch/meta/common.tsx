/** Small shared parts of the Meta collector view: mode badge, permanent mode banner, number/date helpers. */
import { Badge, type BadgeVariant } from "../../Badge";
import { MODE_BADGE, MODE_BANNER, type MetaMode } from "../../../api/metaAds";
import { cx } from "../../../lib/cx";

const MODE_VARIANT: Record<MetaMode, BadgeVariant> = { live: "success", recorded: "info", synthetic: "warning" };

export function ModeBadge({ mode, className }: { mode: MetaMode; className?: string }) {
  return (
    <Badge variant={MODE_VARIANT[mode] ?? "neutral"} dot className={className}>
      <span data-testid="mode-badge">{MODE_BADGE[mode] ?? mode}</span>
    </Badge>
  );
}

/** Always visible, never dismissable: which space of data the page shows. */
export function ModeBanner({ mode }: { mode: MetaMode }) {
  return (
    <div
      role="status"
      data-testid="meta-mode-banner"
      data-mode={mode}
      className={cx(
        "rounded-lg border px-3 py-2 text-sm font-semibold",
        mode === "synthetic" ? "border-warn/50 bg-warn/10 text-warn" : mode === "recorded" ? "border-info/50 bg-info/10 text-info" : "border-ok/40 bg-ok/10 text-ok",
      )}
    >
      {MODE_BANNER[mode]}
    </div>
  );
}

export function short(sha: string | null | undefined, n = 12): string {
  return sha ? sha.slice(0, n) : "—";
}

export function bytesPl(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1).replace(".", ",")} KB`;
  return `${(n / 1024 / 1024).toFixed(1).replace(".", ",")} MB`;
}

export function numPl(n: number | null | undefined, digits = 2): string {
  if (n == null) return "—";
  return new Intl.NumberFormat("pl-PL", { maximumFractionDigits: digits }).format(n);
}
