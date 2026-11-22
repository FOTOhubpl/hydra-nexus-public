import { lazy, Suspense } from "react";
import { useSearchParams } from "react-router";
import { EmptyState } from "../../components/EmptyState";
import { ConstellationExplorer } from "../../components/graph/constellation/ConstellationExplorer";

const GraphExplorer = lazy(() => import("../../components/graph/GraphExplorer").then((m) => ({ default: m.GraphExplorer })));

/**
 * "Graf powiązań": the lazy constellation explorer (components/graph/constellation); `?classic=1` keeps the previous
 * four-view explorer (lasso, evidence path, indicators, timeline), which loads the whole graph at once.
 */
export function GraphTaskPage() {
  const [params] = useSearchParams();
  if (params.get("classic") !== "1") return <ConstellationExplorer />;
  return (
    <Suspense fallback={<EmptyState tone="pending" title="Ładowanie widoku klasycznego…" />}>
      <GraphExplorer />
    </Suspense>
  );
}
