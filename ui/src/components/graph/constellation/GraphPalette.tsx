/**
 * Graph command palette (Ctrl/Cmd+K on the graph screen): campaigns from the loaded overview (instant), incidents
 * and gene values (domains, phones, hashes, ...) from GET /v1/search. Picking a result flies the constellation to it.
 * While the graph screen is open it takes Ctrl/Cmd+K before the global palette (capture phase).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { OvCampaign } from "../../../api/graph";
import { useSearch } from "../../../api/task";
import { cx } from "../../../lib/cx";

export type PaletteHit =
  | { kind: "campaign"; id: string }
  | { kind: "incident"; id: string }
  | { kind: "gene"; label: string; incidents: string[] };

const GENE_GROUP: { label: string; test: (kind: string) => boolean }[] = [
  { label: "domena / host", test: (k) => /domain|host|url_label|ns_set|registrar/.test(k) },
  { label: "telefon", test: (k) => /phone|msisdn/.test(k) },
  { label: "hash", test: (k) => /sha|hash|phash|pdq|mmh3|md5|dhash/.test(k) },
];

export function geneGroup(kind: string): string {
  return GENE_GROUP.find((g) => g.test(kind))?.label ?? "gen";
}

interface Entry { key: string; label: string; hint: string; hit: PaletteHit }

export function GraphPalette({ open, onClose, campaigns, onPick }: { open: boolean; onClose: () => void; campaigns: readonly OvCampaign[]; onPick: (h: PaletteHit) => void }) {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [active, setActive] = useState(0);
  const list = useRef<HTMLUListElement>(null);
  const back = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (open) {
      back.current = document.activeElement as HTMLElement | null;
      setQ("");
      setActive(0);
    } else back.current?.focus?.();
  }, [open]);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 180);
    return () => clearTimeout(t);
  }, [q]);
  const search = useSearch(open ? debounced : "");

  const entries = useMemo<Entry[]>(() => {
    const ql = q.trim().toLowerCase();
    const camps = campaigns
      .filter((c) => !ql || c.id.toLowerCase().includes(ql) || c.label.toLowerCase().includes(ql))
      .sort((a, b) => b.size - a.size)
      .slice(0, ql ? 8 : 6)
      .map((c) => ({ key: `c:${c.id}`, label: c.unassigned ? "bez kampanii" : `${c.label}`, hint: `kampania · ${c.size} inc.`, hit: { kind: "campaign" as const, id: c.id } }));
    const d = search.data;
    if (!d || ql.length < 2) return camps;
    const inc = d.incidents.slice(0, 8).map((i) => ({ key: `i:${i.id}`, label: `${i.id}${i.title ? ` · ${i.title}` : ""}`, hint: "incydent", hit: { kind: "incident" as const, id: i.id } }));
    const genes = d.genes.slice(0, 12).map((g) => ({
      key: `g:${g.kind}:${g.value}`,
      label: `${g.kind}: ${g.value.slice(0, 90)}`,
      hint: `${geneGroup(g.kind)} · ${g.doc_freq} inc.`,
      hit: { kind: "gene" as const, label: `${g.kind}: ${g.value.slice(0, 60)}`, incidents: g.incidents },
    }));
    return [...camps, ...inc, ...genes];
  }, [q, campaigns, search.data]);

  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (!open) return null;
  const go = (e: Entry | undefined) => {
    if (!e) return;
    onClose();
    onPick(e.hit);
  };
  const activeId = entries[active] ? `gp-${active}` : undefined;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-fg/25 pt-24" onClick={onClose} role="presentation">
      <div role="dialog" aria-modal="true" aria-label="Szukaj w grafie" className="w-[640px] max-w-[92vw] overflow-hidden rounded-lg border border-line-strong bg-panel shadow-xl" onClick={(e) => e.stopPropagation()}>
        <input
          autoFocus
          role="combobox"
          aria-expanded="true"
          aria-controls="graph-palette-list"
          aria-activedescendant={activeId}
          value={q}
          onChange={(e) => { setQ(e.target.value); setActive(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, entries.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
            else if (e.key === "Enter") { e.preventDefault(); go(entries[active]); }
            else if (e.key === "Escape") { e.preventDefault(); onClose(); }
          }}
          placeholder="Kampania, incydent, domena, telefon, hash…"
          aria-label="Szukaj w grafie"
          className="h-12 w-full border-b border-line bg-panel px-4 text-base text-fg placeholder:text-subtle focus:outline-none"
        />
        <ul id="graph-palette-list" ref={list} className="max-h-[60vh] overflow-auto py-1" role="listbox" aria-label="Wyniki">
          {entries.map((e, i) => (
            <li key={e.key} id={`gp-${i}`} data-idx={i} role="option" aria-selected={i === active}>
              <button type="button" tabIndex={-1} onMouseEnter={() => setActive(i)} onClick={() => go(e)} className={cx("flex w-full items-center gap-3 px-4 py-1.5 text-left text-sm text-fg", i === active && "bg-active")}>
                <span className="min-w-0 flex-1 truncate">{e.label}</span>
                <span className="shrink-0 text-xs text-subtle">{e.hint}</span>
              </button>
            </li>
          ))}
          {entries.length === 0 && !search.isFetching && <li className="px-4 py-2 text-sm text-muted">Brak wyników{q.trim().length < 2 ? " (wpisz co najmniej 2 znaki, aby szukać incydentów i genów)" : ""}.</li>}
          {search.isFetching && <li className="px-4 py-1.5 text-xs text-subtle">szukam w incydentach i genach…</li>}
          {search.isError && <li className="px-4 py-1.5 text-xs text-err">Wyszukiwanie w genach nie odpowiedziało; kampanie z grafu nadal działają.</li>}
        </ul>
        <ul className="flex flex-wrap gap-x-4 border-t border-line px-4 py-1.5 text-2xs text-subtle" aria-label="Klawisze">
          <li>strzałki: wybór</li>
          <li>Enter: leć do wyniku</li>
          <li>Esc: zamknij</li>
          <li>Ctrl/⌘K: przełącz</li>
        </ul>
      </div>
    </div>
  );
}
