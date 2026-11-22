import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { useSearch } from "../api/task";
import { cx } from "../lib/cx";
import { toggleTheme } from "../lib/theme";
import { NAV_GROUPS } from "./AppShell";
import { Icon } from "./Icon";

interface Entry {
  key: string;
  label: string;
  hint: string;
  to?: string;
  run?: () => void;
  glyph?: string;
}

/** Window event the shell listens to (AppShell): collapse / expand the sidebar from the palette. */
export const NAV_TOGGLE_EVENT = "hydra:nav-toggle";
/** Window event the dashboard listens to (DashboardPage): show / hide its right rail. */
export const RAIL_TOGGLE_EVENT = "hydra:rail-toggle";

const ACTIONS: Entry[] = [
  { key: "act:theme", label: "Przełącz motyw jasny / ciemny", hint: "akcja", run: toggleTheme, glyph: "moon" },
  { key: "act:nav", label: "Zwiń lub rozwiń menu boczne", hint: "akcja · Ctrl/⌘ B", run: () => window.dispatchEvent(new CustomEvent(NAV_TOGGLE_EVENT)), glyph: "panel" },
  { key: "act:rail", label: "Pokaż lub ukryj panel boczny pulpitu", hint: "akcja · Ctrl/⌘ J", run: () => window.dispatchEvent(new CustomEvent(RAIL_TOGGLE_EVENT)), glyph: "rail" },
];

/** ⌘K / Ctrl+K: jump to screens, incidents, campaigns and gene values (GET /v1/search), plus a few shell actions. */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [active, setActive] = useState(0);
  const navigate = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      } else if (e.key === "Escape") setOpen(false);
    };
    const onOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("hydra:palette", onOpen);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("hydra:palette", onOpen); };
  }, []);
  useEffect(() => {
    if (open) {
      setQ("");
      setActive(0);
    }
  }, [open]);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 200);
    return () => clearTimeout(t);
  }, [q]);
  useEffect(() => {
    list.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const search = useSearch(open ? debounced : "");
  const entries = useMemo<Entry[]>(() => {
    const ql = q.trim().toLowerCase();
    const nav: Entry[] = NAV_GROUPS.flatMap((g) => g.items.map((n) => ({ key: `nav:${n.to}`, label: n.label, hint: g.title, to: n.to, glyph: n.glyph })))
      .filter((n) => !ql || n.label.toLowerCase().includes(ql) || n.hint.toLowerCase().includes(ql));
    const actions = ACTIONS.filter((a) => !ql || a.label.toLowerCase().includes(ql));
    const d = search.data;
    if (!d || ql.length < 2) return [...nav, ...actions];
    return [
      ...d.campaigns.slice(0, 8).map((c) => ({ key: `c:${c.id}`, label: `${c.id} · ${c.label}`, hint: "kampania", to: `/campaigns/${encodeURIComponent(c.id)}`, glyph: "campaigns" })),
      ...d.incidents.slice(0, 8).map((i) => ({ key: `i:${i.id}`, label: `${i.id}${i.title ? ` · ${i.title}` : ""}`, hint: "incydent", to: `/incident/${encodeURIComponent(i.id)}`, glyph: "incidents" })),
      ...d.genes.slice(0, 8).map((g) => ({ key: `g:${g.kind}:${g.value}`, label: `${g.family} ${g.kind}: ${g.value.slice(0, 80)}`, hint: `gen · ${g.doc_freq} inc.`, to: `/search?q=${encodeURIComponent(g.value.slice(0, 200))}`, glyph: "search" })),
      ...nav,
      ...actions,
      { key: "search", label: `Szukaj „${q.trim()}” we wszystkich genach`, hint: "wyszukiwanie", to: `/search?q=${encodeURIComponent(q.trim())}`, glyph: "search" },
    ];
  }, [q, search.data]);

  if (!open) return null;
  const go = (e: Entry | undefined) => {
    if (!e) return;
    setOpen(false);
    if (e.run) e.run();
    else if (e.to) navigate(e.to);
  };
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-fg/25 px-3 pt-[12vh] backdrop-blur-[2px] motion-safe:animate-[fade-in_120ms_ease-out]" onClick={() => setOpen(false)} role="presentation">
      <div role="dialog" aria-modal="true" aria-label="Paleta poleceń" className="w-[640px] max-w-full overflow-hidden rounded-2xl border border-line-strong bg-panel shadow-float motion-safe:animate-[pop-in_160ms_var(--ease-out-soft)]" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 border-b border-line px-4">
          <Icon name="search" size={16} className="shrink-0 text-subtle" />
          <input
            ref={input}
            autoFocus
            value={q}
            onChange={(e) => { setQ(e.target.value); setActive(0); }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, entries.length - 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
              else if (e.key === "Enter") { e.preventDefault(); go(entries[active]); }
            }}
            placeholder="Ekran, incydent, kampania, domena, hash, fraza…"
            aria-label="Polecenie"
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={entries[active] ? `palette-${active}` : undefined}
            className="h-12 min-w-0 flex-1 bg-transparent text-base focus:outline-none"
          />
          <kbd className="hidden shrink-0 rounded-md border border-line px-1.5 font-mono text-2xs text-subtle sm:inline">Esc</kbd>
        </div>
        <ul ref={list} id="palette-list" className="max-h-[60vh] overflow-auto p-1.5" role="listbox" aria-label="Wyniki">
          {entries.map((e, i) => (
            <li key={e.key} id={`palette-${i}`} role="option" aria-selected={i === active} data-index={i}>
              <button type="button" tabIndex={-1} onMouseEnter={() => setActive(i)} onClick={() => go(e)}
                className={cx("flex min-h-9 w-full items-center gap-3 rounded-lg px-3 py-1.5 text-left text-sm", i === active ? "bg-accent-soft text-fg" : "text-muted")}>
                {e.glyph && <Icon name={e.glyph} size={16} className={cx("shrink-0", i === active ? "text-accent" : "text-subtle")} />}
                <span className="min-w-0 flex-1 truncate">{e.label}</span>
                <span className="shrink-0 text-xs text-subtle">{e.hint}</span>
              </button>
            </li>
          ))}
          {search.isFetching && <li className="px-3 py-1.5 text-xs text-subtle">szukam…</li>}
          {!search.isFetching && entries.length === 0 && <li className="px-3 py-4 text-center text-sm text-subtle">Brak wyników dla „{q.trim()}”.</li>}
        </ul>
        <div className="flex gap-4 border-t border-line px-4 py-2 text-2xs text-subtle">
          <span>↑↓ wybór</span><span>Enter otwórz</span><span>Esc zamknij</span><span className="ml-auto">⌘K / Ctrl K</span>
        </div>
      </div>
    </div>
  );
}
