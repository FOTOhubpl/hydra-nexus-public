import { describe, expect, it } from "vitest";
import { MINI_OPENAPI as doc } from "../pages/integrations/console/__fixtures__/openapi";
import {
  buildPath,
  buildQuery,
  buildRequestPath,
  bodySkeleton,
  defaultParamValues,
  deref,
  exampleFromSchema,
  filterOperations,
  findOperation,
  groupByTag,
  normalizeText,
  parseOperations,
  resolvePointer,
  schemaFields,
  schemaTypeLabel,
  unwrapNullable,
  validateJsonBody,
  validateParams,
} from "./openapi";

const ops = parseOperations(doc);
const op = (id: string) => ops.find((o) => o.id === id)!;

describe("$ref resolution", () => {
  it("resolves local pointers and returns undefined for broken / remote refs", () => {
    expect(resolvePointer(doc, "#/components/schemas/WatchItem")).toBe(doc.components!.schemas!.WatchItem);
    expect(resolvePointer(doc, "#/components/schemas/Nope")).toBeUndefined();
    expect(resolvePointer(doc, "http://x/y.json")).toBeUndefined();
  });
  it("derefs chains and keeps sibling overrides", () => {
    const s = deref(doc, { $ref: "#/components/schemas/WatchItem", description: "nadpisany" });
    expect(s.required).toEqual(["kind", "value"]);
    expect(s.description).toBe("nadpisany");
  });
  it("is cycle-safe", () => {
    const v = exampleFromSchema(doc, { $ref: "#/components/schemas/Node" }) as Record<string, unknown>;
    expect(v.name).toBe("n");
    expect(v.child).toEqual({});
  });
  it("unwraps nullable anyOf", () => {
    const r = unwrapNullable(doc, { anyOf: [{ type: "integer", minimum: 0 }, { type: "null" }] });
    expect(r.nullable).toBe(true);
    expect(r.schema.type).toBe("integer");
    expect(schemaTypeLabel(doc, { anyOf: [{ type: "string" }, { type: "null" }] })).toBe("string | null");
    expect(schemaTypeLabel(doc, { type: "array", items: { $ref: "#/components/schemas/WatchItem" } })).toBe("array<WatchItem>");
  });
});

describe("operations", () => {
  it("parses every method with tag fallback, deprecated and streaming flags", () => {
    expect(ops).toHaveLength(6);
    expect(op("health_health_get").tag).toBe("system");
    expect(op("old_delete").deprecated).toBe(true);
    expect(op("stream_v1_stream_get").streaming).toBe(true);
    expect(op("list_campaigns_v1_campaigns_get").streaming).toBe(false);
    expect(op("change_watchlist_v1_watchlist_post").body?.schemaName).toBe("WatchlistChange");
  });
  it("groups by tag with system first", () => {
    const groups = groupByTag(ops);
    expect(groups.map((g) => g.tag)).toEqual(["system", "exports", "stream", "task"]);
    expect(groups.find((g) => g.tag === "task")!.operations).toHaveLength(3);
  });
  it("searches path, summary, tag and ignores diacritics", () => {
    expect(filterOperations(ops, "health").map((o) => o.id)).toEqual(["health_health_get"]);
    expect(filterOperations(ops, "stix campaign")).toHaveLength(1);
    expect(filterOperations(ops, "eksporty")).toHaveLength(1); // Polish tag label
    expect(filterOperations(ops, "")).toHaveLength(6);
    expect(normalizeText("Źródła Łódź")).toBe("zrodla lodz");
  });
  it("finds by operationId or 'method path'", () => {
    expect(findOperation(ops, "campaign_stix")?.path).toBe("/v1/campaigns/{campaign_id}/stix");
    expect(findOperation(ops, "post /v1/watchlist")?.id).toBe("change_watchlist_v1_watchlist_post");
    expect(findOperation(ops, "nope")).toBeUndefined();
  });
  it("types params: enum, default, bounds, nullable", () => {
    const [limit, run, order] = op("list_campaigns_v1_campaigns_get").params;
    expect(limit).toMatchObject({ kind: "integer", default: 50, min: 1, max: 500, required: false });
    expect(run).toMatchObject({ kind: "string", nullable: true });
    expect(order!.enum).toEqual(["bits", "size"]);
    expect(op("campaign_stix").params[0]!.required).toBe(true);
  });
});

describe("example skeletons", () => {
  it("builds a body from components with required fields first and first enum", () => {
    const v = JSON.parse(bodySkeleton(doc, op("change_watchlist_v1_watchlist_post")));
    expect(Object.keys(v)[0]).toBe("add");
    expect(v.add).toEqual([{ kind: "keyword", value: "xx" }]);
    expect(v.note).toBe("");
  });
  it("prefers explicit examples and defaults", () => {
    expect(exampleFromSchema(doc, { type: "string", example: "ex" })).toBe("ex");
    expect(exampleFromSchema(doc, { type: "integer", default: 7 })).toBe(7);
    expect(exampleFromSchema(doc, { type: "boolean" })).toBe(false);
    expect(
      exampleFromSchema(doc, { allOf: [{ type: "object", properties: { a: { type: "integer" } } }, { type: "object", properties: { b: { type: "string" } } }] }),
    ).toEqual({ a: 0, b: "" });
  });
  it("lists schema fields for the reference", () => {
    const f = schemaFields(doc, { $ref: "#/components/schemas/WatchItem" });
    expect(f.map((x) => [x.name, x.required])).toEqual([
      ["kind", true],
      ["value", true],
    ]);
  });
});

describe("param serialisation", () => {
  it("encodes path params and keeps missing ones as templates", () => {
    expect(buildPath("/v1/campaigns/{campaign_id}/stix", { "path:campaign_id": "c 1/ż" })).toBe("/v1/campaigns/c%201%2F%C5%BC/stix");
    expect(buildPath("/v1/campaigns/{campaign_id}/stix", {})).toBe("/v1/campaigns/{campaign_id}/stix");
  });
  it("skips empty query params, keeps defaults, encodes values", () => {
    const o = op("list_campaigns_v1_campaigns_get");
    const values = defaultParamValues(o);
    expect(values).toEqual({ "query:limit": "50", "query:run_id": "", "query:order": "" });
    expect(buildQuery(o, { ...values, "query:run_id": "a&b=c" })).toBe("limit=50&run_id=a%26b%3Dc");
    expect(buildRequestPath(o, { "query:limit": "" })).toBe("/v1/campaigns");
  });
  it("validates required, numeric and bounds in Polish", () => {
    expect(validateParams(op("campaign_stix"), {})).toEqual(["Brak wymaganego parametru „campaign_id”."]);
    expect(validateParams(op("list_campaigns_v1_campaigns_get"), { "query:limit": "abc" })[0]).toContain("liczbą");
    expect(validateParams(op("list_campaigns_v1_campaigns_get"), { "query:limit": "900" })[0]).toContain("≤ 500");
    expect(validateJsonBody("{", true)).toContain("Niepoprawny JSON");
    expect(validateJsonBody("", true)).toContain("wymagana");
    expect(validateJsonBody("", false)).toBeNull();
  });
});
