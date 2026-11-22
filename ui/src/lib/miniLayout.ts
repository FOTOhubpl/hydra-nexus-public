/**
 * Deterministic small force layout for the dashboard preview: each node is pulled to an anchor of its
 * campaign (anchors on a circle), nodes repel each other, edges act as springs. No randomness, so the
 * picture is the same on every load. Coordinates in [0, 1].
 */
export interface MiniNode { id: string; group: string | null }
export interface MiniEdge { source: string; target: string; weight: number }

export function miniLayout(nodes: MiniNode[], edges: MiniEdge[], iterations = 300): Record<string, { x: number; y: number }> {
  const groups = Array.from(new Set(nodes.map((n) => n.group ?? `solo:${n.id}`))).sort();
  const anchor = new Map(groups.map((g, i) => {
    const a = (2 * Math.PI * i) / Math.max(1, groups.length) - Math.PI / 2;
    return [g, { x: 0.5 + 0.38 * Math.cos(a), y: 0.47 + 0.3 * Math.sin(a) }] as const;
  }));
  const pos = new Map<string, { x: number; y: number }>(nodes.map((n, i) => {
    const an = anchor.get(n.group ?? `solo:${n.id}`)!;
    const a = i * 2.399963;
    const start: { x: number; y: number } = { x: an.x + 0.03 * Math.cos(a), y: an.y + 0.03 * Math.sin(a) };
    return [n.id, start];
  }));
  const ids = nodes.map((n) => n.id);
  for (let it = 0; it < iterations; it++) {
    const cool = 1 - it / iterations;
    const force = new Map(ids.map((id) => [id, { x: 0, y: 0 }]));
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const p = pos.get(ids[i]!)!, q = pos.get(ids[j]!)!;
        const dx = p.x - q.x, dy = p.y - q.y;
        const d2 = Math.max(1e-4, dx * dx + dy * dy);
        const f = 0.0016 / d2;
        force.get(ids[i]!)!.x += f * dx; force.get(ids[i]!)!.y += f * dy;
        force.get(ids[j]!)!.x -= f * dx; force.get(ids[j]!)!.y -= f * dy;
      }
    }
    for (const e of edges) {
      const p = pos.get(e.source), q = pos.get(e.target);
      if (!p || !q) continue;
      const dx = q.x - p.x, dy = q.y - p.y;
      const k = 0.02 * e.weight;
      force.get(e.source)!.x += k * dx; force.get(e.source)!.y += k * dy;
      force.get(e.target)!.x -= k * dx; force.get(e.target)!.y -= k * dy;
    }
    for (const n of nodes) {
      const p = pos.get(n.id)!, an = anchor.get(n.group ?? `solo:${n.id}`)!, f = force.get(n.id)!;
      f.x += 0.08 * (an.x - p.x); f.y += 0.08 * (an.y - p.y);
      const step = Math.min(0.03, Math.hypot(f.x, f.y)) * cool;
      const len = Math.hypot(f.x, f.y) || 1;
      p.x = Math.min(0.93, Math.max(0.07, p.x + (f.x / len) * step));
      p.y = Math.min(0.82, Math.max(0.1, p.y + (f.y / len) * step));
    }
  }
  return Object.fromEntries(pos);
}
