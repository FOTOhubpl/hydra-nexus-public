/** Campaign colours of the constellation (kept free of sigma so the explorer shell loads before the WebGL chunk). */
import type { Overview } from "../../../api/graph";
import { familyColors } from "../../../lib/evidence";
import type { Theme } from "../../../lib/theme";

export const NEUTRAL = { light: "#8a9099", dark: "#6b7580" } as const;

/** Colour of a campaign = dominant gene family of its internal links (neutral when the API sent no family). */
export function campaignColors(ov: Overview, theme: Theme): Map<string, string> {
  const fam = familyColors(theme);
  const out = new Map<string, string>();
  for (const c of ov.campaigns) {
    const best = Object.entries(c.family_bits ?? {}).sort((a, b) => b[1] - a[1])[0];
    const f = best && best[1] > 0 ? best[0] : c.family ?? null;
    out.set(c.id, (f && fam[f]) || NEUTRAL[theme]);
  }
  return out;
}

/** WebGL2 probe without importing sigma. */
export function hasWebGL2(): boolean {
  try {
    return Boolean(document.createElement("canvas").getContext("webgl2"));
  } catch {
    return false;
  }
}
