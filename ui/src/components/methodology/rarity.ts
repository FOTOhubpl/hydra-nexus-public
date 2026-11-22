/**
 * Client-side mirror of the HYDRA-RGE discrete rarity formula, for the illustrative calculator on /methodology.
 * Source of truth: hydra/entropy.py (`Rarity.u_estimate`, `Rarity.discrete_bits`, `operator_seed_soft`) and
 * hydra/synapse.py (`quality_weight`). Constants below are copied from those files; if they change there, the
 * unit test in rarity.test.ts that reproduces the METHODOLOGY §5 worked example is the tripwire.
 * This is an illustration of the formula, never a result: the product computes bits server-side per gene.
 */

export const DEFAULT_M = 0.5; // entropy.DEFAULT_M: P(agree | same campaign), conservative
export const PRIOR_STRENGTH = 20; // entropy.PRIOR_STRENGTH: A pseudo-incidents behind u0
export const DISCRETE_CAP = 14; // entropy.DISCRETE_CAP
export const DEFAULT_U0 = 2 ** -8; // entropy.DEFAULT_U0
export const HUB_MIN = 5; // entropy.HUB_MIN
export const HUB_FRAC = 0.2; // entropy.HUB_FRAC (campaign / kit units)
export const HUB_FRAC_INCIDENTS = 0.5; // entropy.HUB_FRAC_INCIDENTS (incident units)
export const QUALITY_FULL = 0.5; // synapse.QUALITY_FULL
export const QUALITY_MIN = 0.25; // synapse.QUALITY_MIN
export const MIN_KIND_UNITS = 200; // hydra/priors MIN_KIND_UNITS: thinner background kinds are ignored

/** A selection of per-kind background priors u0 from entropy.KIND_U0 (assumptions, not fitted values). */
export const KIND_U0_EXAMPLES: { kind: string; label: string; log2u0: number }[] = [
  { kind: "iban", label: "IBAN", log2u0: -16 },
  { kind: "tracker", label: "identyfikator śledzący / piksel", log2u0: -14 },
  { kind: "phone", label: "telefon", log2u0: -14 },
  { kind: "domain", label: "domena rejestrowalna", log2u0: -11 },
  { kind: "phrase", label: "fraza tekstu", log2u0: -9 },
  { kind: "default", label: "rodzaj bez wpisu (domyślny)", log2u0: -8 },
  { kind: "url_skeleton", label: "szkielet adresu URL", log2u0: -7 },
  { kind: "asn", label: "numer ASN", log2u0: -3 },
];

/** û (Beta prior with mean u0 and strength A), with the optional one-sided background max rule. */
export function uEstimate(dfOthers: number, nOthers: number, u0: number, bg?: { df: number; n: number } | null, a = PRIOR_STRENGTH): number {
  const uLoc = (dfOthers + a * u0) / (nOthers + a);
  if (!bg || bg.df <= 0 || bg.n <= 0 || bg.n < MIN_KIND_UNITS) return uLoc;
  return Math.max(uLoc, (dfOthers + bg.df + a * u0) / (nOthers + bg.n + a));
}

/** synapse.quality_weight: the weaker observation decides; q < 0.25 gives 0, q ≥ 0.5 counts fully. */
export function qualityWeight(q: number): number {
  if (q < QUALITY_MIN) return 0;
  return Math.max(0, Math.min(1, q / QUALITY_FULL));
}

/** Hub limit: holders above which a value is boilerplate (0 bits). */
export function hubLimit(nUnits: number, frac = HUB_FRAC_INCIDENTS): number {
  return Math.max(HUB_MIN, Math.ceil(frac * nUnits));
}

export interface BitsInput {
  /** corpus size in incidents (both incidents of the pair included) */
  n: number;
  /** incidents holding the value, the pair included (so ≥ 2) */
  holders: number;
  u0: number;
  quality?: number;
  background?: { df: number; n: number } | null;
}

export interface BitsResult {
  bits: number;
  raw: number;
  u: number;
  dfOthers: number;
  nOthers: number;
  hub: boolean;
  capped: boolean;
  weight: number;
}

/** b(v) = min(14, max(0, log2(m / û))) × quality weight, incident units, pair excluded (entropy.discrete_bits). */
export function discreteBits({ n, holders, u0, quality = 1, background = null }: BitsInput): BitsResult {
  const pair = Math.min(2, n);
  const dfOthers = Math.max(0, holders - pair);
  const nOthers = Math.max(0, n - pair);
  const hub = holders > hubLimit(n);
  const u = uEstimate(dfOthers, nOthers, u0, background);
  const raw = Math.log2(DEFAULT_M / Math.max(u, 1e-12));
  const weight = qualityWeight(quality);
  const clipped = hub ? 0 : Math.min(DISCRETE_CAP, Math.max(0, raw));
  return { bits: clipped * weight, raw, u, dfOthers, nOthers, hub, capped: !hub && raw > DISCRETE_CAP, weight };
}

/** Smooth operator units (entropy.operator_seed_soft): L, U and the linear weight w for h holders. */
export function smoothBounds(n: number): { L: number; U: number } {
  return { L: Math.max(HUB_MIN, Math.ceil(HUB_FRAC * n)), U: Math.max(2 * HUB_MIN, Math.ceil(HUB_FRAC_INCIDENTS * n)) };
}

/** w = (U + 1 − h) / (U + 1 − L) for L < h ≤ U; 1 at or below L (unit), 0 above U (hub). */
export function smoothWeight(n: number, h: number): number {
  const { L, U } = smoothBounds(n);
  if (h <= L) return 1;
  if (h > U) return 0;
  return (U + 1 - h) / (U + 1 - L);
}
