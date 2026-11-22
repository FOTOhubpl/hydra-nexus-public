import { geneKindPl } from "./labelsPl";
/** Polish labels and number formatting for evidence (DESIGN.md §1, §4). Pure, unit-tested. */

export const FAMILY_LABEL: Record<string, string> = {
  VIS: "obraz",
  TXT: "tekst",
  INF: "infrastruktura",
  ID: "tożsamość",
  NAR: "narracja",
  META: "metadane",
};

/** CSS token per family for DOM marks (tokens in index.css). */
export const FAMILY_VAR: Record<string, string> = {
  VIS: "var(--color-fam-vis)",
  TXT: "var(--color-fam-txt)",
  INF: "var(--color-fam-inf)",
  ID: "var(--color-fam-id)",
  NAR: "var(--color-fam-nar)",
  META: "var(--color-fam-meta)",
};

/** Same hues as hex for the WebGL graph (sigma cannot read CSS variables); keep in sync with index.css. */
export const FAMILY_COLOR: Record<string, string> = {
  VIS: "#6250d6",
  TXT: "#1baf7a",
  INF: "#008300",
  ID: "#e87ba4",
  NAR: "#eb6834",
  META: "#eda100",
};

export const FAMILY_COLOR_DARK: Record<string, string> = {
  VIS: "#9085e9",
  TXT: "#199e70",
  INF: "#0ca30c",
  ID: "#d55181",
  NAR: "#d95926",
  META: "#c98500",
};

/** Stacking order whose adjacent pairs pass the CVD / normal-vision checks in both modes. */
export const FAMILY_STACK_ORDER = ["NAR", "TXT", "META", "ID", "INF", "VIS"] as const;

export function familyColors(theme: "light" | "dark"): Record<string, string> {
  return theme === "dark" ? FAMILY_COLOR_DARK : FAMILY_COLOR;
}

export const HYPOTHESIS_LABEL: Record<string, string> = {
  same_campaign: "ta sama kampania",
  shared_template: "wspólny szablon",
  shared_asset: "wspólny zasób",
  independent: "niezależne",
  abstain: "wstrzymane (do analityka)",
};

export const DECISION_LABEL: Record<string, string> = {
  assign: "przypisano do kampanii",
  new_campaign: "nowa kampania / nowy wzorzec",
  abstain: "wstrzymano (niejednoznaczne)",
};

export const GENE_STATUS_LABEL: Record<string, string> = {
  observed: "zaobserwowano",
  unavailable: "niedostępne",
  failed: "błąd ekstrakcji",
  not_applicable: "nie dotyczy",
  unknown: "nieznane",
};

export const ROLE_LABEL: Record<string, string> = {
  ad_creative: "kreacja reklamy",
  feed_screenshot: "zrzut z feedu",
  screen_recording: "nagranie ekranu",
  ad_video: "wideo reklamy",
  landing_html: "strona docelowa (HTML)",
  landing_asset: "zasób strony",
  landing_screenshot: "zrzut strony docelowej",
  har: "log sieciowy (HAR)",
  platform_metadata: "metadane platformy",
  text: "tekst / log",
  url: "URL",
  other: "inne",
};

export const REVIEW_LABEL: Record<string, string> = {
  accept_link: "potwierdzono powiązanie",
  reject_link: "odrzucono powiązanie",
  merge_campaigns: "scalono kampanie",
  split_campaign: "rozdzielono kampanię",
  move_incident: "przeniesiono incydent",
  undo: "cofnięto",
  unsure: "oznaczono jako niepewne",
  unsure_undo: "cofnięto oznaczenie niepewne",
};

const nf = (digits: number) => new Intl.NumberFormat("pl-PL", { minimumFractionDigits: digits, maximumFractionDigits: digits });

/** Decimal comma, fixed digits; null → em dash (unknown is never 0). */
export function plNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return nf(digits).format(value);
}

/** Signed bits with one decimal: +6,2 b / −3,0 b. */
export function formatBits(bits: number | null | undefined): string {
  if (bits === null || bits === undefined || !Number.isFinite(bits)) return "—";
  const sign = bits > 0 ? "+" : bits < 0 ? "−" : "±";
  return `${sign}${nf(1).format(Math.abs(bits))} b`;
}

/** Spec wording for evidence strength (bits, not probability). */
export function bitsWord(bits: number): string {
  if (bits > 20) return "silne";
  if (bits >= 10) return "możliwe";
  return "słabe";
}

export function formatProbability(p: number | null | undefined): string {
  return plNumber(p, 3);
}

/** Display name of a reason kind; scorer-internal rows get a Polish explanation instead of the code. */
export function reasonKindLabel(kind: string): string {
  return geneKindPl(kind);
}

export function familyLabel(code: string): string {
  return FAMILY_LABEL[code] ?? code;
}

const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;

/** Polish date for display. Date-only values ("2026-10-01") stay a calendar day: no time and no timezone shift. */
export function formatDatePl(iso: string | null | undefined): string {
  if (!iso) return "—";
  const day = DATE_ONLY.exec(iso);
  if (day) {
    const d = new Date(Number(day[1]), Number(day[2]) - 1, Number(day[3]));
    if (Number.isNaN(d.getTime()) || d.getDate() !== Number(day[3])) return iso;
    return d.toLocaleDateString("pl-PL", { dateStyle: "medium" });
  }
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("pl-PL", { dateStyle: "medium", timeStyle: "short" });
}

export interface WaterfallStep {
  key: string;
  label: string;
  start: number;
  end: number;
  bits: number;
  kind: "for" | "against" | "unknown" | "total" | "cap";
  family?: string;
}

/**
 * Bits waterfall: one step per family in the fixed family order (missing → unknown, 0 b), then the
 * residual between the scorer's total and the family sum (contradictions / corrections not already in a
 * family), then the total. The bar always ends at the scorer's `total_bits`.
 */
export function waterfallSteps(
  familyBits: Record<string, number>,
  families: readonly string[],
  totalBits: number,
  missing: readonly string[],
): WaterfallStep[] {
  const steps: WaterfallStep[] = [];
  let acc = 0;
  for (const fam of families) {
    const has = Object.prototype.hasOwnProperty.call(familyBits, fam);
    const bits = has ? (familyBits[fam] ?? 0) : 0;
    const unknown = !has || missing.includes(fam);
    steps.push({
      key: fam,
      label: `${fam} · ${familyLabel(fam)}`,
      start: acc,
      end: acc + bits,
      bits,
      kind: unknown && bits === 0 ? "unknown" : bits < 0 ? "against" : "for",
      family: fam,
    });
    acc += bits;
  }
  // Extra family keys from the scorer that are not in the fixed order.
  for (const [fam, bits] of Object.entries(familyBits)) {
    if (families.includes(fam)) continue;
    steps.push({ key: fam, label: fam, start: acc, end: acc + bits, bits, kind: bits < 0 ? "against" : "for", family: fam });
    acc += bits;
  }
  const residual = totalBits - acc;
  if (Math.abs(residual) >= 0.05) {
    steps.push({
      key: "residual",
      label: residual < 0 ? "Sprzeczności / korekta" : "Ponad limit rodzin",
      start: acc,
      end: totalBits,
      bits: residual,
      kind: residual < 0 ? "against" : "for",
    });
  }
  acc = totalBits;
  steps.push({ key: "total", label: "Suma", start: 0, end: acc, bits: acc, kind: "total" });
  return steps;
}
