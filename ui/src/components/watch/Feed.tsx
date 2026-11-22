/**
 * Realtime feed of polled items (WATCH-RT): first page from GET /v1/watch/rt/items, then `watch.item` /
 * `watch.match` stream events merged on top (dedup by id). Rows have a fixed height and only the visible window
 * is rendered, so thousands of replayed items stay cheap. Values are always defanged and never links.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Badge } from "../Badge";
import { Button } from "../Button";
import { InfoTip } from "../Tooltip";
import { cx } from "../../lib/cx";
import { ITEM_KIND_LABEL, SOURCE_SHORT, rtApi, rtKeys, timePl, watchRtBus, type ItemFilters, type RtItem, type RtSource } from "../../api/watchRt";
import { ReplayTag } from "./parts";
import { ApiErrorState } from "../ApiErrorState";

export const ROW_H = 60;
export const VIEW_H = 480;
const OVERSCAN = 6;
const LIVE_CAP = 3000;

export function itemMatchesFilters(it: RtItem, f: ItemFilters, matchCount: number): boolean {
  if (f.source && it.source !== f.source) return false;
  if (f.kind && it.kind !== f.kind) return false;
  if (f.matched === "1" && matchCount <= 0) return false;
  if (f.matched === "0" && matchCount > 0) return false;
  if (f.q) {
    const q = f.q.toLowerCase();
    const hay = [it.value_defanged, it.external_id ?? "", ...(it.indicators ?? []).map((x) => x.value_display)].join(" ").toLowerCase();
    // the server matches the raw value; the client also accepts the defanged form ("[.]")
    if (!hay.includes(q) && !hay.replace(/\[\.\]/g, ".").includes(q)) return false;
  }
  return true;
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function Feed({ sources, selectedItemId, onSelect }: { sources: RtSource[]; selectedItemId: number | null; onSelect: (item: RtItem) => void }) {
  const [source, setSource] = useState("");
  const [kind, setKind] = useState("");
  const [matched, setMatched] = useState<"" | "1" | "0">("");
  const [text, setText] = useState("");
  const q = useDebounced(text.trim(), 300);
  const filters = useMemo<ItemFilters>(() => ({ source, kind, matched, q }), [source, kind, matched, q]);

  const query = useInfiniteQuery({
    queryKey: rtKeys.items(filters),
    queryFn: ({ pageParam, signal }) => rtApi.items(filters, pageParam, signal),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next_before ?? undefined,
    refetchInterval: 30_000,
  });

  // items and match counts that arrived through the stream since the page opened
  const [live, setLive] = useState<RtItem[]>([]);
  const [extra, setExtra] = useState<Record<number, number>>({});
  const [streamed, setStreamed] = useState(0);
  useEffect(() => watchRtBus.subscribe((e) => {
    if (e.type === "item") {
      setLive((prev) => [e.item, ...prev.filter((x) => x.id !== e.item.id)].slice(0, LIVE_CAP));
      setStreamed((n) => n + 1);
    } else if (e.type === "match") {
      const id = e.match.item_id;
      setExtra((prev) => ({ ...prev, [id]: (prev[id] ?? 0) + 1 }));
    }
  }), []);

  const rows = useMemo(() => {
    const byId = new Map<number, RtItem>();
    for (const p of query.data?.pages ?? []) for (const it of p.items) byId.set(it.id, it);
    for (const it of live) {
      const prev = byId.get(it.id);
      byId.set(it.id, prev ? { ...prev, ...it, match_count: Math.max(prev.match_count, it.match_count) } : it);
    }
    const count = (it: RtItem) => Math.max(it.match_count, extra[it.id] ?? 0);
    return [...byId.values()]
      .map((it) => (count(it) !== it.match_count ? { ...it, match_count: count(it) } : it))
      .filter((it) => itemMatchesFilters(it, filters, it.match_count))
      .sort((a, b) => b.id - a.id);
  }, [query.data, live, extra, filters]);

  // screen-reader summary, refreshed at most every 5 s so a fast replay does not flood the live region
  const rowsRef = useRef({ n: rows.length, s: streamed });
  rowsRef.current = { n: rows.length, s: streamed };
  const [announce, setAnnounce] = useState("");
  useEffect(() => {
    const upd = () => setAnnounce(`${rowsRef.current.n} pozycji w widoku, ${rowsRef.current.s} nowych ze strumienia`);
    upd();
    const t = setInterval(upd, 5000);
    return () => clearInterval(t);
  }, []);

  const [scrollTop, setScrollTop] = useState(0);
  const viewH = Math.min(VIEW_H, Math.max(ROW_H, rows.length * ROW_H));
  const first = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN);
  const last = Math.min(rows.length, Math.ceil((scrollTop + VIEW_H) / ROW_H) + OVERSCAN);
  const visible = rows.slice(first, last);

  const select = "h-7 min-w-0 rounded-[4px] border border-line bg-panel px-2 text-sm";
  return (
    <section className="surface-card flex min-w-0 flex-col" aria-labelledby="rt-feed-title">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3 sm:px-5">
        <h2 id="rt-feed-title" className="text-sm font-semibold text-fg">Strumień na żywo</h2>
        <InfoTip content="Nowe adresy, domeny, certyfikaty i reklamy pobrane przez silnik. Wiersz z oznaczeniem dopasowania zawiera wskaźnik z watchlisty. Adresy są rozbrojone ([.]) i nieklikalne." />
        <span className="ml-auto font-mono text-xs text-muted" data-testid="feed-count">{rows.length} poz. · +{streamed} ze strumienia</span>
        <span className="sr-only" aria-live="polite" aria-atomic="true">{announce}</span>
      </header>
      <div className="grid grid-cols-2 gap-2 border-b border-line px-4 py-2 sm:flex sm:flex-wrap sm:px-5">
        <select aria-label="Filtr źródła" value={source} onChange={(e) => setSource(e.target.value)} className={select} data-testid="feed-source">
          <option value="">wszystkie źródła</option>
          {sources.map((s) => <option key={s.id} value={s.id}>{s.name_pl}</option>)}
        </select>
        <select aria-label="Filtr rodzaju" value={kind} onChange={(e) => setKind(e.target.value)} className={select} data-testid="feed-kind">
          <option value="">wszystkie rodzaje</option>
          {Object.entries(ITEM_KIND_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select aria-label="Filtr dopasowania" value={matched} onChange={(e) => setMatched(e.target.value as "" | "1" | "0")} className={select} data-testid="feed-matched">
          <option value="">wszystkie pozycje</option>
          <option value="1">tylko dopasowane</option>
          <option value="0">bez dopasowania</option>
        </select>
        <input type="search" aria-label="Szukaj w strumieniu" placeholder="Szukaj (np. domena)" value={text} onChange={(e) => setText(e.target.value)} className={cx(select, "sm:w-56")} data-testid="feed-search" />
      </div>
      {query.isError && !query.data ? (
        <div className="p-3"><ApiErrorState error={query.error} resource="strumień pozycji" endpoint="/v1/watch/rt/items" compact /></div>
      ) : query.isPending && rows.length === 0 ? (
        <div className="p-4 text-sm text-subtle">Wczytywanie pozycji…</div>
      ) : rows.length === 0 ? (
        <div className="p-4 text-sm text-subtle" data-testid="feed-empty">Brak pozycji dla tych filtrów. Uruchom silnik albo tryb demonstracyjny (odtworzenie).</div>
      ) : (
        <div
          className="relative overflow-y-auto overscroll-contain"
          style={{ height: viewH }}
          onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
          data-testid="feed-viewport"
        >
          <div role="list" aria-label="Pozycje strumienia" style={{ height: rows.length * ROW_H, position: "relative" }}>
            {visible.map((it, k) => {
              const i = first + k;
              const sel = it.id === selectedItemId;
              return (
                <div key={it.id} role="listitem" aria-setsize={rows.length} aria-posinset={i + 1} style={{ position: "absolute", top: i * ROW_H, left: 0, right: 0, height: ROW_H }}>
                  <button
                    type="button"
                    onClick={() => onSelect(it)}
                    aria-pressed={sel}
                    data-testid="feed-row"
                    data-matched={it.match_count > 0 || undefined}
                    className={cx("flex h-full w-full min-w-0 flex-col justify-center gap-1 border-b border-line px-4 text-left hover:bg-hover sm:px-5", sel && "bg-accent-soft")}
                  >
                    <span className="flex w-full min-w-0 items-center gap-2">
                      <span className="min-w-0 flex-1 truncate font-mono text-sm text-fg" title={it.value_defanged}>{it.value_defanged}</span>
                      {it.match_count > 0 && <Badge variant="danger" dot>dopasowanie{it.match_count > 1 ? ` ×${it.match_count}` : ""}</Badge>}
                      {it.replay && <ReplayTag />}
                    </span>
                    <span className="flex w-full min-w-0 items-center gap-2 text-xs text-muted">
                      <span className="shrink-0">{SOURCE_SHORT[it.source] ?? it.source}</span>
                      <span aria-hidden="true">·</span>
                      <span className="shrink-0">{ITEM_KIND_LABEL[it.kind] ?? it.kind}</span>
                      <span aria-hidden="true">·</span>
                      <span className="truncate font-mono" title="pierwsze wystąpienie">{timePl(it.first_seen)}</span>
                    </span>
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}
      {query.hasNextPage && (
        <div className="border-t border-line px-4 py-2 sm:px-5">
          <Button size="sm" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>{query.isFetchingNextPage ? "Wczytuję…" : "Wczytaj starsze"}</Button>
        </div>
      )}
    </section>
  );
}
