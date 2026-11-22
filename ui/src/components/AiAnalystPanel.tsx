import { Fragment } from "react";
import { useAiBrief, type AiCitation } from "../api/task";
import { Panel } from "./Panel";

/** Jump to an evidence row (`ev-<section>-<index>`) and flash it. */
export function jumpToEvidence(section: string, index: number) {
  const el = document.getElementById(`ev-${section}-${index}`);
  if (!el) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.add("bg-abstain-soft");
  window.setTimeout(() => el.classList.remove("bg-abstain-soft"), 1600);
}

function withCitations(text: string, cites: AiCitation[]) {
  const byN = new Map(cites.map((c) => [c.n, c]));
  return text.split(/(\[\d+\])/g).map((part, i) => {
    const m = /^\[(\d+)\]$/.exec(part);
    const c = m ? byN.get(Number(m[1])) : undefined;
    if (!c) return <Fragment key={i}>{part}</Fragment>;
    return (
      <button key={i} type="button" onClick={() => jumpToEvidence(c.section, c.index)} title={c.label ?? `${c.section} #${c.index + 1}`}
        className="mx-0.5 rounded-[3px] border border-accent/40 bg-for-soft px-1 font-mono text-xs text-accent hover:underline">
        [{c.n}]
      </button>
    );
  });
}

/** Optional LLM summary over the evidence on this page; the evidence below never depends on it. */
export function AiAnalystPanel({ kind, a, b }: { kind: "campaign" | "pair"; a: string; b?: string }) {
  const q = useAiBrief(kind, a, b);
  const d = q.data;
  return (
    <Panel title="AI analityk" actions={<span className="text-xs text-subtle">streszczenie dowodów; nie zmienia decyzji</span>}>
      {q.isPending ? <div className="text-sm text-subtle">Przygotowywanie streszczenia…</div>
        : !d || d.status !== "ok" ? (
          <div className="flex items-start gap-2 text-sm text-muted">
            <span aria-hidden="true" className="mt-0.5 text-subtle">○</span>
            <span>{d?.reason_pl ?? "Warstwa AI wyłączona — wyniki opierają się wyłącznie na dowodach."}</span>
          </div>
        ) : (
          <div className="space-y-2">
            <p className="text-sm leading-6 whitespace-pre-line text-fg">{withCitations(d.text_pl ?? "", d.citations ?? [])}</p>
            {(d.citations ?? []).length > 0 && (
              <ol className="space-y-0.5 text-xs text-muted">
                {d.citations!.map((c) => (
                  <li key={c.n}><button type="button" onClick={() => jumpToEvidence(c.section, c.index)} className="text-left hover:underline">[{c.n}] {c.label ?? `${c.section} #${c.index + 1}`}</button></li>
                ))}
              </ol>
            )}
            <div className="text-2xs text-subtle">Model: <span className="font-mono">{d.model ?? "—"}</span>{d.generated_at ? ` · ${d.generated_at}` : ""}. Tekst generowany; źródłem prawdy są wiersze dowodów, do których prowadzą przypisy.</div>
          </div>
        )}
    </Panel>
  );
}
