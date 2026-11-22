/**
 * Display-only Polish wording for backend values (UI-FINAL, review X01/E). Nothing here changes a score or a
 * decision: it maps backend codes/English fragments to Polish text and names what a number is (and is not).
 */
import type { EvidencePointer } from "../api/task";
import { FAMILY_LABEL } from "./evidence";
import { polishCodes } from "./labelsPl";

/** Label for every conformal p-value: a rank statistic against calibration scores, not a campaign probability. */
export const P_VALUE_LABEL = "p-wartość konformalna (nie prawdopodobieństwo kampanii)";
/** Label for every `probability` field that is not calibrated (the bit-derived ranking score). */
export const UNCALIBRATED_LABEL = "wynik nieskalibrowany";
/** Label for `empirical_coverage` of the conformal set (raw set, leave-one-out over calibration). */
export const COVERAGE_LABEL = "pokrycie zbioru surowego (jackknife), nie polityki końcowej";

/** Backend reason for an abstain that would re-join campaigns split by an analyst (hydra/assign.py BRIDGE_REASON). */
export const BRIDGE_REASON_EN = "bridges cannot-link";
export const BRIDGE_REASON_PL = "łączy kampanie rozdzielone decyzją analityka";

// family codes as standalone words only: "rodziny cech (TXT)", "INF, VIS" — never inside gene keys like INF:domain
const FAMILY_CODE = /(?<![\w:/.-])(TXT|VIS|INF)(?![\w:/.-])/g;

/** Backend reason text with the English bridge reason and the TXT/VIS/INF family codes shown in Polish. */
export function polishReason(text: string | null | undefined): string {
  if (!text) return "";
  return polishCodes(text.split(BRIDGE_REASON_EN).join(BRIDGE_REASON_PL).replace(FAMILY_CODE, (code) => FAMILY_LABEL[code] ?? code));
}

/** Gene kinds whose pointer means the whole file even without a bbox (hydra/genome/aud.py, vid_avsync.py). */
const WHOLE_FILE_KINDS = new Set(["audio_fp_sig", "audio_fp_band", "av_sync_suspicion"]);

/**
 * True when an evidence pointer locates the whole file rather than a frame: timecode 0 with a full-frame bbox
 * (origin 0,0; size equal to the media when its dimensions are known), or timecode 0 without a bbox for the
 * whole-file kinds. Such pointers are shown as "cały plik", not "klatka 00:00".
 */
export function isWholeFile(e: EvidencePointer | null | undefined, kind?: string | null, dims?: { w?: number | null; h?: number | null } | null): boolean {
  if (!e || e.timecode_s !== 0) return false;
  const b = e.bbox;
  if (!b) return Boolean(kind && WHOLE_FILE_KINDS.has(kind));
  if (b.length !== 4 || b[0] !== 0 || b[1] !== 0 || !(b[2] > 0) || !(b[3] > 0)) return false;
  if (dims?.w && dims?.h) return Math.abs(b[2] - dims.w) < 1 && Math.abs(b[3] - dims.h) < 1;
  return true;
}

export const WHOLE_FILE_TEXT = "cały plik";
