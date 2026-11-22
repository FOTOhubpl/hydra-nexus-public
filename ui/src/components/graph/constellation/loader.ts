/**
 * Lazy campaign loader: keeps the campaigns the explorer wants (expanded by the analyst or zoomed into) loaded,
 * cancels requests for campaigns that left the wanted set, and keeps an LRU of loaded details under a node budget.
 * Framework-free (subscribe/getVersion fit React's useSyncExternalStore) and unit-tested with a fake fetcher.
 */
export type LoadStatus = "idle" | "loading" | "ready" | "error";

export interface LoaderOptions<T> {
  fetch: (id: string, signal: AbortSignal) => Promise<T>;
  /** Weight of a loaded item (e.g. its node count) for the budget. */
  weight?: (v: T) => number;
  /** Evict least-recently-used items above this total weight (wanted items are never evicted). */
  budget?: number;
  /** Max items kept regardless of weight. */
  capacity?: number;
  concurrency?: number;
  onLoaded?: (id: string, v: T, ms: number) => void;
}

interface Inflight { ctrl: AbortController; started: number }

export class CampaignLoader<T> {
  private readonly cache = new Map<string, T>(); // insertion order = LRU order (oldest first)
  private readonly inflight = new Map<string, Inflight>();
  private readonly errors = new Map<string, string>();
  private wanted: string[] = [];
  private listeners = new Set<() => void>();
  private version = 0;
  readonly stats = { requests: 0, cancelled: 0, hits: 0, evicted: 0, failed: 0 };

  constructor(private readonly opts: LoaderOptions<T>) {}

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getVersion = (): number => this.version;

  private emit() {
    this.version++;
    for (const fn of this.listeners) fn();
  }

  get(id: string): T | undefined {
    return this.cache.get(id);
  }

  status(id: string): LoadStatus {
    if (this.cache.has(id)) return "ready";
    if (this.inflight.has(id)) return "loading";
    if (this.errors.has(id)) return "error";
    return "idle";
  }

  error(id: string): string | undefined {
    return this.errors.get(id);
  }

  loadedIds(): string[] {
    return [...this.cache.keys()];
  }

  inflightIds(): string[] {
    return [...this.inflight.keys()];
  }

  totalWeight(): number {
    let w = 0;
    for (const v of this.cache.values()) w += this.opts.weight?.(v) ?? 1;
    return w;
  }

  /**
   * Declares the campaigns that should be loaded, highest priority first. In-flight requests for campaigns no
   * longer wanted are aborted; missing ones start up to the concurrency limit; cached ones are marked recently used.
   */
  want(ids: readonly string[]): void {
    const next = [...new Set(ids)];
    const keep = new Set(next);
    let changed = false;
    for (const [id, f] of this.inflight) {
      if (!keep.has(id)) {
        f.ctrl.abort();
        this.inflight.delete(id);
        this.stats.cancelled++;
        changed = true;
      }
    }
    for (const id of next) {
      const v = this.cache.get(id);
      if (v !== undefined) {
        if (!this.wanted.includes(id)) this.stats.hits++;
        this.cache.delete(id);
        this.cache.set(id, v);
      }
    }
    this.wanted = next;
    this.pump();
    if (changed) this.emit();
  }

  /** Drops a failed campaign's error so the next want() retries it. */
  retry(id: string): void {
    if (this.errors.delete(id)) {
      this.pump();
      this.emit();
    }
  }

  dispose(): void {
    for (const f of this.inflight.values()) f.ctrl.abort();
    this.inflight.clear();
    this.listeners.clear();
  }

  private pump() {
    const limit = this.opts.concurrency ?? 4;
    for (const id of this.wanted) {
      if (this.inflight.size >= limit) break;
      if (this.cache.has(id) || this.inflight.has(id) || this.errors.has(id)) continue;
      this.start(id);
    }
  }

  private start(id: string) {
    const ctrl = new AbortController();
    const started = typeof performance !== "undefined" ? performance.now() : Date.now();
    this.inflight.set(id, { ctrl, started });
    this.stats.requests++;
    this.opts.fetch(id, ctrl.signal).then(
      (v) => {
        if (this.inflight.get(id)?.ctrl !== ctrl) return; // cancelled meanwhile
        this.inflight.delete(id);
        this.cache.set(id, v);
        const now = typeof performance !== "undefined" ? performance.now() : Date.now();
        this.opts.onLoaded?.(id, v, now - started);
        this.evict();
        this.pump();
        this.emit();
      },
      (e: unknown) => {
        if (this.inflight.get(id)?.ctrl !== ctrl) return;
        this.inflight.delete(id);
        if (ctrl.signal.aborted) return;
        this.stats.failed++;
        this.errors.set(id, e instanceof Error ? e.message : String(e));
        this.pump();
        this.emit();
      },
    );
  }

  private evict() {
    const keep = new Set(this.wanted);
    const budget = this.opts.budget ?? Infinity;
    const cap = this.opts.capacity ?? Infinity;
    let total = this.totalWeight();
    for (const [id, v] of this.cache) {
      if (total <= budget && this.cache.size <= cap) break;
      if (keep.has(id)) continue;
      this.cache.delete(id);
      total -= this.opts.weight?.(v) ?? 1;
      this.stats.evicted++;
    }
  }
}
