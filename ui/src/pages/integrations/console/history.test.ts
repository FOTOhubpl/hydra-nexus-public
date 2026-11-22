import { describe, expect, it } from "vitest";
import { HISTORY_CAP, HISTORY_KEY, clearHistory, loadHistory, recordHistory, redactQuery, storableBody, toHistoryEntry } from "./history";
import { STORAGE_PREFIX } from "../../../lib/storage";

const raw = () => window.localStorage.getItem(STORAGE_PREFIX + HISTORY_KEY) ?? "";

describe("console history redaction", () => {
  it("never stores headers", () => {
    recordHistory({ method: "get", path: "/health", query: "", status: 200, ms: 3.4, headers: { Authorization: "Bearer abc", "X-Api-Key": "k" } });
    expect(raw()).not.toContain("Bearer");
    expect(raw()).not.toMatch(/authorization|x-api-key/i);
    expect(loadHistory()[0]).toMatchObject({ method: "GET", path: "/health", status: 200, ms: 3 });
  });
  it("drops bodies with secret-like fields at any depth", () => {
    expect(storableBody("/v1/x", JSON.stringify({ a: { api_key: "x" } }))).toBeUndefined();
    expect(storableBody("/v1/x", JSON.stringify({ items: [{ password: "x" }] }))).toBeUndefined();
    expect(storableBody("/v1/x", JSON.stringify({ secrets: { META: "v" } }))).toBeUndefined();
    expect(storableBody("/v1/x", JSON.stringify({ Token: "v" }))).toBeUndefined();
    expect(storableBody("/v1/x", "not json")).toBeUndefined();
  });
  it("drops every body on secret / connect / webhook endpoints (e.g. {name, value})", () => {
    expect(storableBody("/v1/integrations/misp/secret", JSON.stringify({ name: "MISP_KEY", value: "abcdef" }))).toBeUndefined();
    expect(storableBody("/v1/integrations/misp/connect", JSON.stringify({}))).toBeUndefined();
    expect(storableBody("/v1/integrations/webhooks", JSON.stringify({ name: "x", url: "https://h" }))).toBeUndefined();
    recordHistory({ method: "put", path: "/v1/integrations/misp/secret", query: "", status: 200, ms: 1, body: JSON.stringify({ name: "MISP_KEY", value: "supersecretvalue" }) });
    expect(raw()).not.toContain("supersecretvalue");
  });
  it("keeps harmless bodies for replay", () => {
    const b = JSON.stringify({ add: [{ kind: "keyword", value: "bonus" }] });
    expect(toHistoryEntry({ method: "post", path: "/v1/watchlist", query: "", status: 200, ms: 1, body: b }).body).toBe(b);
  });
  it("redacts secret-like query values", () => {
    expect(redactQuery("limit=5&api_key=abc")).toBe(`limit=5&api_key=${encodeURIComponent("•••")}`);
    const e = recordHistory({ method: "get", path: "/x", query: "access_token=zzz", status: 200, ms: 1 })[0]!;
    expect(raw()).not.toContain("zzz");
    expect(e.query).not.toContain("zzz");
  });
  it("re-sanitises tampered storage and caps at 50", () => {
    window.localStorage.setItem(
      STORAGE_PREFIX + HISTORY_KEY,
      JSON.stringify([{ method: "post", path: "/v1/x/secret", query: "", status: 200, ms: 1, at: "bad", body: '{"value":"s"}', headers: { a: 1 } }]),
    );
    const [e] = loadHistory();
    expect(e!.body).toBeUndefined();
    expect((e as unknown as Record<string, unknown>).headers).toBeUndefined();
    for (let i = 0; i < 60; i++) recordHistory({ method: "get", path: `/p${i}`, query: "", status: 200, ms: 1 });
    expect(loadHistory()).toHaveLength(HISTORY_CAP);
    expect(loadHistory()[0]!.path).toBe("/p59");
    clearHistory();
    expect(loadHistory()).toEqual([]);
  });
});
