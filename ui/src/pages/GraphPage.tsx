import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useGraph } from "../api/client";
import type { GraphEdge, GraphNode, GraphPayload } from "../api/types";
import { ApiErrorState } from "../components/ApiErrorState";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { GraphCanvas } from "../components/GraphCanvas";
import { PropertyList } from "../components/PropertyList";
import { formatCount } from "../lib/format";
import { updateSettings, useSettings } from "../lib/settings";

type Selection = { type: "node"; node: GraphNode } | { type: "edge"; edge: GraphEdge } | null;

function isGraphPayload(value: unknown): value is GraphPayload {
  return Boolean(value) && Array.isArray((value as GraphPayload).nodes) && Array.isArray((value as GraphPayload).edges);
}

function Inspector({
  data,
  selection,
  onSelectNode,
}: {
  data: GraphPayload;
  selection: Selection;
  onSelectNode: (node: GraphNode) => void;
}) {
  const nodesById = useMemo(() => new Map(data.nodes.map((node) => [String(node.id), node])), [data]);

  const neighbours = useMemo(() => {
    if (selection?.type !== "node") return [];
    const id = String(selection.node.id);
    const out: Array<{ edge: GraphEdge; other: GraphNode | undefined; direction: "out" | "in" }> = [];
    for (const edge of data.edges) {
      if (String(edge.source) === id) out.push({ edge, other: nodesById.get(String(edge.target)), direction: "out" });
      else if (String(edge.target) === id) out.push({ edge, other: nodesById.get(String(edge.source)), direction: "in" });
    }
    return out;
  }, [selection, data, nodesById]);

  if (!selection) {
    return (
      <EmptyState
        compact
        title="Nic nie zaznaczono"
        description="Kliknij węzeł lub krawędź, aby zobaczyć szczegóły. Kliknij puste miejsce, aby wyczyścić zaznaczenie."
      />
    );
  }

  if (selection.type === "edge") {
    const { edge } = selection;
    const source = nodesById.get(String(edge.source));
    const target = nodesById.get(String(edge.target));
    return (
      <div className="flex flex-col">
        <div className="border-b border-line px-3 py-2 text-xs text-muted">
          Krawędź{" "}
          <button type="button" className="font-mono text-fg hover:text-accent" onClick={() => source && onSelectNode(source)}>
            {source?.label ?? String(edge.source)}
          </button>{" "}
          →{" "}
          <button type="button" className="font-mono text-fg hover:text-accent" onClick={() => target && onSelectNode(target)}>
            {target?.label ?? String(edge.target)}
          </button>
        </div>
        <PropertyList data={edge} order={["id", "source", "target", "kind", "weight"]} />
      </div>
    );
  }

  const { node } = selection;
  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className="min-w-0 truncate text-sm font-semibold text-fg">{node.label || String(node.id)}</span>
        <Link to={`/item/${encodeURIComponent(String(node.id))}`} className="ml-auto shrink-0 text-xs text-accent hover:underline">
          Otwórz jako element
        </Link>
      </div>
      <PropertyList data={node} order={["id", "label", "group", "size"]} />
      <div className="border-b border-line bg-raised px-3 py-1.5 text-2xs font-semibold tracking-wider text-subtle uppercase">
        Połączenia · {formatCount(neighbours.length)}
      </div>
      {neighbours.length === 0 ? (
        <div className="px-3 py-3 text-xs text-subtle">Brak krawędzi.</div>
      ) : (
        <ul className="divide-y divide-line/70">
          {neighbours.slice(0, 500).map(({ edge, other, direction }) => (
            <li key={edge.id}>
              <button
                type="button"
                disabled={!other}
                onClick={() => other && onSelectNode(other)}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-hover disabled:cursor-not-allowed disabled:text-subtle"
              >
                <span className="w-4 shrink-0 font-mono text-subtle">{direction === "out" ? "→" : "←"}</span>
                <span className="min-w-0 truncate text-fg">{other?.label ?? "(brak węzła)"}</span>
                {edge.kind && <span className="ml-auto shrink-0 font-mono text-subtle">{edge.kind}</span>}
              </button>
            </li>
          ))}
          {neighbours.length > 500 && <li className="px-3 py-1.5 text-xs text-subtle">… i {formatCount(neighbours.length - 500)} więcej</li>}
        </ul>
      )}
    </div>
  );
}

export function GraphPage() {
  const query = useGraph();
  const settings = useSettings();
  const [selection, setSelection] = useState<Selection>(null);

  const data = query.data;
  const valid = isGraphPayload(data);

  let canvas;
  if (query.isPending) {
    canvas = <EmptyState tone="pending" title="Wczytywanie grafu…" description="GET /v1/graph" />;
  } else if (query.isError) {
    canvas = (
      <ApiErrorState
        error={query.error}
        resource="graf"
        endpoint="GET /v1/graph → { nodes: [{id, label, group?, size?}], edges: [{id, source, target, kind?, weight?}] }"
        action={
          <Button size="sm" onClick={() => void query.refetch()}>
            Ponów
          </Button>
        }
      />
    );
  } else if (!valid) {
    canvas = (
      <EmptyState
        tone="error"
        title="Nieoczekiwany format grafu"
        description="GET /v1/graph musi zwrócić obiekt z tablicami `nodes` i `edges`."
      />
    );
  } else if (data.nodes.length === 0) {
    canvas = <EmptyState title="Graf jest pusty" description="Backend nie zwrócił jeszcze węzłów. Najpierw zaimportuj dane." />;
  } else {
    canvas = (
      <ErrorBoundary label="Widok grafu" resetKeys={[data]}>
        <GraphCanvas
          data={data}
          layoutIterations={settings.layoutIterations}
          showLabels={settings.graphLabels}
          selectedNodeId={selection?.type === "node" ? String(selection.node.id) : null}
          selectedEdgeId={selection?.type === "edge" ? String(selection.edge.id) : null}
          onNodeClick={(node) => setSelection({ type: "node", node })}
          onEdgeClick={(edge) => setSelection({ type: "edge", edge })}
          onStageClick={() => setSelection(null)}
        />
      </ErrorBoundary>
    );
  }

  return (
    <div className="flex h-full min-h-[480px] flex-col">
      <div className="flex h-10 shrink-0 items-center gap-3 border-b border-line bg-panel px-4 text-xs text-muted">
        {valid ? (
          <span>
            <span className="font-mono text-fg">{formatCount(data.nodes.length)}</span> węzłów ·{" "}
            <span className="font-mono text-fg">{formatCount(data.edges.length)}</span> krawędzi
          </span>
        ) : (
          <span className="font-mono">GET /api/v1/graph</span>
        )}
        <div className="ml-auto flex items-center gap-3">
          <label className="flex cursor-pointer items-center gap-1.5 select-none">
            <input
              type="checkbox"
              className="accent-accent"
              checked={settings.graphLabels}
              onChange={(event) => updateSettings({ graphLabels: event.target.checked })}
            />
            Wszystkie etykiety
          </label>
          <label className="flex items-center gap-1.5">
            Układ
            <select
              value={settings.layoutIterations}
              onChange={(event) => updateSettings({ layoutIterations: Number(event.target.value) })}
              className="h-6 rounded-[4px] border border-line bg-canvas px-1 font-mono text-xs text-fg"
            >
              {[0, 50, 100, 200, 500, 1000].map((n) => (
                <option key={n} value={n}>
                  {n === 0 ? "wył." : `${n} iter.`}
                </option>
              ))}
            </select>
          </label>
          <Button size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}>
            {query.isFetching ? "Wczytywanie…" : "Odśwież"}
          </Button>
        </div>
      </div>
      <div className="flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1 bg-canvas">{canvas}</div>
        <aside aria-label="Inspektor" className="flex w-80 shrink-0 flex-col overflow-auto border-l border-line bg-panel">
          <div className="flex h-9 shrink-0 items-center border-b border-line px-3 text-xs font-semibold tracking-wider text-muted uppercase">
            Inspektor
          </div>
          {valid ? (
            <Inspector data={data} selection={selection} onSelectNode={(node) => setSelection({ type: "node", node })} />
          ) : (
            <EmptyState compact title="Nie wczytano grafu" />
          )}
        </aside>
      </div>
    </div>
  );
}
