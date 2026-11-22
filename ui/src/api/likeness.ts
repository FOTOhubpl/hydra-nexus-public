/**
 * "Nadużycie wizerunku" triage card (hydra/api_likeness.py): GET /v1/incidents/{id}/likeness.
 * Analyst triage aid only: not deepfake detection and not a campaign-link rule.
 */
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "./client";
import type { EvidencePointer } from "./task";

export type LikenessState = "present" | "absent" | "unknown";
export type LikenessStrength = "brak" | "słabe" | "umiarkowane" | "silne";

export interface LikenessItem {
  family: string;
  kind: string;
  value: string | null;
  quality: number | null;
  provenance: string | null;
  label_pl: string | null;
  other_incident: string | null;
  evidence: EvidencePointer;
}

export interface LikenessCheck {
  key: string; // c2pa | watermark | avatar_geometry | audio_watermark | av_sync | synthetic_image | synthetic_video
  label_pl: string;
  state: LikenessState;
  summary_pl: string;
  /** ai_generator checks: evidence (counted) | tool_mark | hint (both shown, never counted). */
  role?: "evidence" | "tool_mark" | "hint";
  counted?: boolean;
  /** Backend wording for the badge (e.g. "wykryto" / "nie wykryto" / "nieznane"). */
  state_label_pl?: string;
}

export interface LikenessSignal {
  key: string; // public_figure | financial_promise | media_brand | reused_media | ai_generator | manipulation_suspicion
  label_pl: string;
  state: LikenessState;
  summary_pl: string;
  hint_pl: string | null;
  items: LikenessItem[];
  checks: LikenessCheck[];
  counted?: boolean; // false: shown for triage, not counted in `present` / `strength`
  checked?: boolean; // ai_generator: checks ran; "unknown" then means "nieznane", not "nie dało się sprawdzić"
  state_label_pl?: string | null;
}

export interface LikenessCard {
  incident_id: string;
  run_id: string | null;
  title_pl: string;
  disclaimer_pl: string;
  triggered: boolean;
  present: number;
  total: number;
  strength: LikenessStrength;
  signals: LikenessSignal[];
  limits_pl: string[];
}

export const likenessApi = {
  card: (id: string, runId?: string | null, signal?: AbortSignal) =>
    apiFetch<LikenessCard>(
      `/v1/incidents/${encodeURIComponent(id)}/likeness${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`,
      { signal },
    ),
};

export function useLikeness(id: string | undefined, runId?: string | null) {
  return useQuery({
    queryKey: ["task", "likeness", id ?? "", runId ?? null],
    queryFn: ({ signal }) => likenessApi.card(id as string, runId, signal),
    enabled: Boolean(id),
    retry: false,
  });
}
