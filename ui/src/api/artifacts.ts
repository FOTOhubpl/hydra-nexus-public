/** Client for hydra/api_artifacts.py: artefact bytes, previews, per-artefact genes, HAR, transcripts, materials. */
import { useQuery } from "@tanstack/react-query";
import { apiFetch, apiUrl } from "./client";
import type { EvidencePointer, Gene, Hypothesis } from "./task";

const enc = encodeURIComponent;

export type ArtifactKind = "image" | "video" | "audio" | "html" | "har" | "json" | "text" | "other";

export const rawUrl = (id: string, download = false) => apiUrl(`/v1/artifacts/${enc(id)}/raw${download ? "?download=1" : ""}`);
export const thumbUrl = (id: string, w = 320) => apiUrl(`/v1/artifacts/${enc(id)}/thumb?w=${w}`);
export const frameUrl = (id: string, t: number, w = 320) => apiUrl(`/v1/artifacts/${enc(id)}/frame?t=${Math.max(0, Math.round(t * 10) / 10)}&w=${w}`);

/** Same classification as hydra.api_artifacts.kind_of (used before /info has loaded). */
export function kindOf(mime: string | null | undefined, path?: string | null, role?: string | null): ArtifactKind {
  const m = (mime ?? "").toLowerCase();
  const p = (path ?? "").toLowerCase();
  if (role === "har" || p.endsWith(".har")) return "har";
  if (m === "image/svg+xml") return "text";
  if (m.startsWith("image/")) return "image";
  if (m.startsWith("video/")) return "video";
  if (m.startsWith("audio/")) return "audio";
  if (["text/html", "application/xhtml+xml", "multipart/related", "application/x-mimearchive"].includes(m) || /\.(html?|mhtml?)$/.test(p)) return "html";
  if (m === "application/json" || m === "application/ld+json" || p.endsWith(".json")) return "json";
  if (m.startsWith("text/") || ["application/javascript", "application/xml", "application/x-javascript", "application/vnd.apple.mpegurl", "message/rfc822"].includes(m)) return "text";
  return "other";
}

export const KIND_LABEL: Record<ArtifactKind, string> = {
  image: "obrazy", video: "wideo", audio: "audio", html: "strony HTML", har: "logi HAR", json: "JSON / metadane", text: "tekst / kod", other: "inne",
};

export interface ArtifactLink { incident_id: string; role: string; path_in_incident: string; meta: Record<string, unknown> }
export interface MediaInfo {
  width?: number | null; height?: number | null; format?: string | null; duration_s?: number | null; bit_rate?: number | null;
  rotation?: number; video_codec?: string | null; audio_codec?: string | null; fps?: string | null; sample_rate?: number | null; error?: string;
}
export interface ProvFact { kind: string; value: string | null; status: string; note: string | null; scope: "artifact" | "incident"; incident_id: string; extractor: string }
export interface ArtifactInfo {
  id: string; sha256: string; mime: string; size: number; kind: ArtifactKind; status: string | null;
  observed_at: string | null; created_at: string | null; source_ref: string | null; meta: Record<string, unknown>;
  incidents: ArtifactLink[]; sources: { source_ref: string; job_id: string | null; imported_at: string | null }[];
  media: MediaInfo | null; gene_counts: { family: string; status: string; n: number }[]; timecodes: number[];
  provenance: ProvFact[]; limits: { text_bytes: number; har_bytes: number };
}

export interface GenePair {
  a: string; b: string; run_id: string; bits: number | null; probability: number; hypothesis: Hypothesis;
  explanation_pl: string | null; side: "a" | "b"; evidence: EvidencePointer; other_evidence: EvidencePointer | null;
}
export interface ArtifactGene extends Gene {
  level: string; tier: string | null; doc_freq: number | null; rarity_bits: number | null; pairs: GenePair[];
}
export interface ArtifactGenes {
  artifact_id: string; run_id: string | null; incidents_in_run: number; total: number; truncated: boolean;
  by_family: Record<string, number>; by_status: Record<string, number>; genes: ArtifactGene[];
}

export interface ArtifactText {
  artifact_id: string; mime: string; kind: ArtifactKind; size: number; encoding: string; truncated: boolean; limit: number;
  text: string; srcdoc?: string; rewritten_assets?: number;
}

export interface HarRow {
  idx: number; method: string | null; url: string | null; host: string | null; status: number | null; mime: string | null;
  size: number | null; time_ms: number | null; started: string | null; redirect: string | null; resource_type: string | null; server_ip: string | null;
}
export interface HarTableData {
  artifact_id: string; entries: number; truncated: boolean; rows: HarRow[]; hosts: { host: string; requests: number }[];
  statuses: Record<string, number>; redirect_chains: { idx: number | null; host: string | null; status: number | null; url: string | null }[][];
  pages: { title: string | null; started: string | null }[]; creator: string | null;
}

export interface Transcript {
  artifact_id: string; available: boolean; model: string | null; segments: { start: number; end: number; text: string }[];
  gene: { status: string; note: string | null } | null;
}

export interface Materials {
  total: number; bytes: number; by_kind: Record<string, number>; bytes_by_kind: Record<string, number>; by_role: Record<string, number>;
  by_corpus: Record<string, number>; kind_by_corpus: Record<string, Record<string, number>>;
  recent: { artifact_id: string; mime: string; size: number; kind: ArtifactKind; created_at: string | null; incident_id: string; role: string; path_in_incident: string }[];
}

const base = { staleTime: 60_000 } as const;
const on = (id: string | null | undefined) => Boolean(id && /^[0-9a-f]{64}$/.test(id));

export function useArtifactInfo(id: string | null | undefined) {
  return useQuery({ ...base, queryKey: ["art", "info", id], enabled: on(id), queryFn: ({ signal }) => apiFetch<ArtifactInfo>(`/v1/artifacts/${enc(id!)}/info`, { signal }) });
}
export function useArtifactGenes(id: string | null | undefined) {
  return useQuery({ ...base, queryKey: ["art", "genes", id], enabled: on(id), queryFn: ({ signal }) => apiFetch<ArtifactGenes>(`/v1/artifacts/${enc(id!)}/genes`, { signal }) });
}
export function useArtifactText(id: string | null | undefined, preview = false, enabled = true) {
  return useQuery({ ...base, queryKey: ["art", "text", id, preview], enabled: enabled && on(id), queryFn: ({ signal }) => apiFetch<ArtifactText>(`/v1/artifacts/${enc(id!)}/text${preview ? `?preview=1&origin=${enc(window.location.origin)}` : ""}`, { signal }) });
}
export function useHar(id: string | null | undefined, enabled = true) {
  return useQuery({ ...base, queryKey: ["art", "har", id], enabled: enabled && on(id), queryFn: ({ signal }) => apiFetch<HarTableData>(`/v1/artifacts/${enc(id!)}/har`, { signal }) });
}
export function useTranscript(id: string | null | undefined, enabled = true) {
  return useQuery({ ...base, queryKey: ["art", "tr", id], enabled: enabled && on(id), queryFn: ({ signal }) => apiFetch<Transcript>(`/v1/artifacts/${enc(id!)}/transcript`, { signal }) });
}
export function useMaterials() {
  return useQuery({ ...base, queryKey: ["art", "materials"], queryFn: ({ signal }) => apiFetch<Materials>("/v1/materials", { signal }) });
}
