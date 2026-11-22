/** Client for hydra/api_geom.py: ORB/RootSIFT + MAGSAC++ verification of a pair's most similar images (display only). */
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "./client";
import { runCacheOptions } from "../lib/runSelection";

const enc = encodeURIComponent;

export interface GeometryPair {
  unit_a: string; unit_b: string; artifact_a: string; artifact_b: string;
  bbox_a: [number, number, number, number] | null; bbox_b: [number, number, number, number] | null; class_a: string | null; class_b: string | null;
  dinov2_cos: number; source_size_a: [number, number] | null; source_size_b: [number, number] | null;
  method: string; inliers: number; coverage_a: number; coverage_b: number; grid_a: number; grid_b: number;
  mirrored: boolean; homography: number[][] | null; anisotropy: number | null; perspective: number | null;
  scale: number | null; size_a: [number, number]; size_b: [number, number]; same_asset: boolean; crop_match: boolean;
  /** inlier correspondences [xa, ya, xb, yb], normalised 0..1 to the whole artefact */
  points: [number, number, number, number][];
  explanation_pl: string;
}

export interface PairGeometry {
  a: string; b: string; version: string; pairs: GeometryPair[];
  thresholds: { min_inliers: number; min_coverage: number; crop_coverage: number; crop_grid: number };
  scoring_flags: Record<string, boolean>;
  note_pl: string;
}

export const usePairGeometry = (a?: string, b?: string) => useQuery({
  retry: false, ...runCacheOptions, queryKey: ["task", "geometry", a, b], enabled: Boolean(a && b),
  queryFn: ({ signal }) => apiFetch<PairGeometry>(`/v1/pairs/${enc(a!)}/${enc(b!)}/geometry`, { signal }),
});
