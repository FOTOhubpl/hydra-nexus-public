/** Link from a decision card to the decision laboratory of the incident (route /lab/:incident). */
import { Link } from "react-router";
import type { Assignment, PairScore } from "../api/task";

export function labPath(incidentId: string): string {
  return `/lab/${encodeURIComponent(incidentId)}`;
}

export function LabLink({ incidentId }: { incidentId: string | null | undefined }) {
  if (!incidentId) return null;
  return <Link to={labPath(incidentId)} className="text-xs text-accent hover:underline" data-testid="lab-link">Laboratorium decyzji (co by zmieniło decyzję) →</Link>;
}

/** PairScore of the top assignment candidate (contradictions, missing families), or null. */
export function topPair(a: Pick<Assignment, "candidates"> | null | undefined): PairScore | null {
  return a?.candidates?.[0]?.best_pair ?? null;
}
