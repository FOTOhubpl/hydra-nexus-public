import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { runAwareQueryKeyHash, runSelection, runStaleTime, RUN_STALE_MS, switchRun, withRun } from "./runSelection";

afterEach(() => runSelection.set(null));

// Same wiring as createQueryClient() in App.tsx (importing App here would pull the WebGL graph stack into jsdom).
const createQueryClient = () => new QueryClient({ defaultOptions: { queries: { retry: false, queryKeyHashFn: runAwareQueryKeyHash } } });

describe("run-aware query cache", () => {
  it("hashes run-scoped keys with the selected run and leaves mutable keys alone", () => {
    const scoped = ["task", "dashboard"];
    const mutable = ["task", "status"];
    runSelection.set("run-a");
    const a = [runAwareQueryKeyHash(scoped), runAwareQueryKeyHash(mutable), runAwareQueryKeyHash(["task", "graphx", "explore"])];
    runSelection.set("run-b");
    const b = [runAwareQueryKeyHash(scoped), runAwareQueryKeyHash(mutable), runAwareQueryKeyHash(["task", "graphx", "explore"])];
    expect(a[0]).not.toBe(b[0]);
    expect(a[1]).toBe(b[1]);
    expect(a[2]).not.toBe(b[2]);
  });

  it("keeps one cache entry per run, so switching back is served from cache", () => {
    const client = createQueryClient();
    runSelection.set("run-a");
    client.setQueryData(["task", "dashboard"], { run: "a" });
    runSelection.set("run-b");
    expect(client.getQueryData(["task", "dashboard"])).toBeUndefined();
    client.setQueryData(["task", "dashboard"], { run: "b" });
    runSelection.set("run-a");
    expect(client.getQueryData(["task", "dashboard"])).toEqual({ run: "a" });
  });

  it("uses a long stale time only for an explicitly selected (immutable) run", () => {
    expect(runStaleTime()).toBeLessThan(RUN_STALE_MS);
    runSelection.set("run-a");
    expect(runStaleTime()).toBe(RUN_STALE_MS);
  });

  it("corpus switch aborts the old run's in-flight requests instead of invalidating everything", async () => {
    const client: QueryClient = createQueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    runSelection.set("run-a");
    const signals: Record<string, AbortSignal> = {};
    const hang = (name: string) => ({ signal }: { signal: AbortSignal }) => {
      signals[name] = signal;
      return new Promise<never>(() => {});
    };
    void client.prefetchQuery({ queryKey: ["task", "graph", { min_p: 0 }], queryFn: hang("graph") });
    void client.prefetchQuery({ queryKey: ["task", "status"], queryFn: hang("status") });
    await Promise.resolve();
    switchRun(client, "run-b");
    expect(runSelection.get()).toBe("run-b");
    expect(signals.graph?.aborted).toBe(true);
    expect(signals.status?.aborted).toBe(false);
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("appends run_id to /v1/system/flow", () => {
    runSelection.set("run-a");
    expect(withRun("/v1/system/flow")).toBe("/v1/system/flow?run_id=run-a");
    expect(withRun("/v1/status")).toBe("/v1/status");
  });
});
