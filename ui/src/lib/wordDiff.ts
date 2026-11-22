/** Word-level LCS diff (for headline / text variants). Returns runs of shared / only-A / only-B words. */
export type DiffOp = { op: "same" | "del" | "add"; text: string };

export function wordDiff(a: string, b: string): DiffOp[] {
  const x = a.split(/\s+/).filter(Boolean), y = b.split(/\s+/).filter(Boolean);
  const n = x.length, m = y.length;
  if (n * m > 400_000) return [{ op: "del", text: a }, { op: "add", text: b }];
  const L = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i]![j] = x[i]!.toLowerCase() === y[j]!.toLowerCase() ? L[i + 1]![j + 1]! + 1 : Math.max(L[i + 1]![j]!, L[i]![j + 1]!);
  const out: DiffOp[] = [];
  const push = (op: DiffOp["op"], w: string) => { const last = out[out.length - 1]; if (last && last.op === op) last.text += ` ${w}`; else out.push({ op, text: w }); };
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i]!.toLowerCase() === y[j]!.toLowerCase()) { push("same", y[j]!); i++; j++; }
    else if (L[i + 1]![j]! >= L[i]![j + 1]!) push("del", x[i++]!);
    else push("add", y[j++]!);
  }
  while (i < n) push("del", x[i++]!);
  while (j < m) push("add", y[j++]!);
  return out;
}

/** Share of words of the shorter text that survive into the other (0..1). */
export function sharedRatio(ops: DiffOp[]): number {
  const same = ops.filter((o) => o.op === "same").reduce((s, o) => s + o.text.split(" ").length, 0);
  const a = ops.filter((o) => o.op !== "add").reduce((s, o) => s + o.text.split(" ").length, 0);
  const b = ops.filter((o) => o.op !== "del").reduce((s, o) => s + o.text.split(" ").length, 0);
  return same / Math.max(1, Math.min(a, b));
}
