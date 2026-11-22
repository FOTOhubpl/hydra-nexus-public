/**
 * Wire types for the local backend. Only the fields the shell relies on are
 * required; everything else is optional so the UI keeps working while the
 * backend contract evolves.
 */

/** GET /health */
export interface Health {
  status?: string;
  version?: string;
  device?: string;
  offline?: boolean;
  [key: string]: unknown;
}

export const JOB_STATUSES = ["queued", "running", "done", "failed"] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];
export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = ["done", "failed"];

/** GET /v1/jobs/{id} */
export interface Job {
  job_id?: string;
  id?: string;
  status: JobStatus;
  /** Fraction 0..1 (values above 1 are read as a percentage). */
  progress?: number;
  total?: number;
  processed?: number;
  /** Arbitrary named counters, rendered as-is. */
  counts?: Record<string, number>;
  error?: string | null;
  message?: string | null;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
}

/** POST /v1/import → 202 */
export interface ImportAccepted {
  job_id: string;
  [key: string]: unknown;
}

/** One row of GET /v1/artifacts */
export interface Artifact {
  id: string;
  mime: string;
  size: number;
  source_ref: string;
  status: string;
  [key: string]: unknown;
}

/** GET /v1/artifacts?limit&offset */
export interface ArtifactPage {
  items: Artifact[];
  total: number;
}

export interface GraphNode {
  id: string;
  label: string;
  group?: string | null;
  size?: number | null;
  [key: string]: unknown;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind?: string | null;
  weight?: number | null;
  [key: string]: unknown;
}

/** GET /v1/graph */
export interface GraphPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
  [key: string]: unknown;
}
