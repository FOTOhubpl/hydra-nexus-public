/**
 * OpenAPI 3.x helpers for the in-app API console and the generated API reference (Integracje & API).
 *
 * The document is the local FastAPI schema (`GET /api/openapi.json`, same origin; hydra/api.py strips `/api`).
 * Everything here is pure: $ref resolution, operation listing/grouping/search, parameter typing, request-body
 * skeletons and URL serialisation. No network, no dependency.
 */

export type JsonSchema = {
  $ref?: string;
  type?: string | string[];
  title?: string;
  description?: string;
  default?: unknown;
  example?: unknown;
  examples?: unknown[] | Record<string, unknown>;
  enum?: unknown[];
  const?: unknown;
  format?: string;
  nullable?: boolean;
  items?: JsonSchema;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean | JsonSchema;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  deprecated?: boolean;
  [key: string]: unknown;
};

export interface OpenApiParameter {
  name: string;
  in: "path" | "query" | "header" | "cookie";
  required?: boolean;
  description?: string;
  deprecated?: boolean;
  schema?: JsonSchema;
  example?: unknown;
}

interface RawOperation {
  tags?: string[];
  summary?: string;
  description?: string;
  operationId?: string;
  deprecated?: boolean;
  parameters?: (OpenApiParameter | { $ref: string })[];
  requestBody?: { required?: boolean; content?: Record<string, { schema?: JsonSchema; example?: unknown }> } | { $ref: string };
  responses?: Record<string, { description?: string; content?: Record<string, { schema?: JsonSchema; example?: unknown }> }>;
}

export interface OpenApiDoc {
  openapi: string;
  info?: { title?: string; version?: string; description?: string };
  paths: Record<string, Record<string, RawOperation | unknown>>;
  components?: { schemas?: Record<string, JsonSchema>; parameters?: Record<string, OpenApiParameter> };
}

export const METHODS = ["get", "post", "put", "patch", "delete"] as const;
export type HttpMethod = (typeof METHODS)[number];

export const UNSAFE_METHODS: ReadonlySet<string> = new Set(["post", "put", "patch", "delete"]);

export interface ParamInfo {
  name: string;
  in: "path" | "query" | "header" | "cookie";
  required: boolean;
  description: string;
  deprecated: boolean;
  /** Primitive kind for the form control. */
  kind: "string" | "integer" | "number" | "boolean" | "array" | "object";
  enum: string[] | null;
  default: unknown;
  nullable: boolean;
  min: number | null;
  max: number | null;
  /** Human-readable type, e.g. "integer ≥ 0", "string | null". */
  typeLabel: string;
}

export interface BodyInfo {
  contentType: string;
  required: boolean;
  schema: JsonSchema | null;
  /** Name of the referenced component, if any (e.g. "WatchlistChange"). */
  schemaName: string | null;
}

export interface ResponseInfo {
  status: string;
  description: string;
  contentType: string | null;
  schema: JsonSchema | null;
}

export interface Operation {
  /** operationId, or a stable `<method>_<path>` fallback. */
  id: string;
  method: HttpMethod;
  path: string;
  tag: string;
  tags: string[];
  summary: string;
  description: string;
  deprecated: boolean;
  params: ParamInfo[];
  body: BodyInfo | null;
  responses: ResponseInfo[];
  /** Server-Sent Events / long-lived stream: never executed with "Wyślij". */
  streaming: boolean;
}

export const UNTAGGED = "system";

/** Polish labels for the backend router tags (hydra/api*.py). Unknown tags are shown verbatim. */
export const TAG_LABELS_PL: Record<string, string> = {
  system: "System (zdrowie, import, zadania)",
  task: "Zadanie: kampanie, incydenty, pary",
  visual: "Widoki wizualne",
  review: "Przegląd analityka",
  graph: "Graf",
  exports: "Eksporty (STIX, XARF, podpisy)",
  batch: "Przypisanie wsadowe",
  watch: "Watch (obserwacja reklam)",
  stream: "Strumień SSE",
  hunt: "Polowanie (hipotezy)",
  likeness: "Podobieństwo",
  artifacts: "Artefakty",
  lineage: "Linia wariantów",
  kontra: "KONTRA (laboratorium)",
  integrations: "Integracje",
  webhooks: "Webhooki",
};

export function tagLabel(tag: string): string {
  return TAG_LABELS_PL[tag] ?? tag;
}

// ---------------------------------------------------------------------------
// $ref resolution
// ---------------------------------------------------------------------------

/** Resolve a local JSON pointer (`#/components/schemas/X`). Returns undefined for remote / broken refs. */
export function resolvePointer(doc: unknown, ref: string): unknown {
  if (!ref.startsWith("#/")) return undefined;
  let node: unknown = doc;
  for (const raw of ref.slice(2).split("/")) {
    const key = raw.replace(/~1/g, "/").replace(/~0/g, "~");
    if (node === null || typeof node !== "object" || !(key in (node as Record<string, unknown>))) return undefined;
    node = (node as Record<string, unknown>)[key];
  }
  return node;
}

export function refName(ref: string | undefined): string | null {
  if (!ref) return null;
  const i = ref.lastIndexOf("/");
  return i >= 0 ? ref.slice(i + 1) : ref;
}

/**
 * Follow `$ref` chains (cycle-safe) and return the target schema; sibling keywords next to `$ref`
 * (OpenAPI 3.1 allows them, e.g. a description) override the target's.
 */
export function deref(doc: OpenApiDoc, schema: JsonSchema | undefined | null, seen: Set<string> = new Set()): JsonSchema {
  if (!schema || typeof schema !== "object") return {};
  if (!schema.$ref) return schema;
  if (seen.has(schema.$ref)) return { title: refName(schema.$ref) ?? undefined, description: "(odwołanie cykliczne)" };
  const target = resolvePointer(doc, schema.$ref);
  if (!target || typeof target !== "object") return { title: refName(schema.$ref) ?? undefined };
  const next = new Set(seen);
  next.add(schema.$ref);
  const { $ref: _ignored, ...siblings } = schema;
  void _ignored;
  return { ...deref(doc, target as JsonSchema, next), ...siblings };
}

/** Split a nullable union (`anyOf: [X, {type: null}]`, `type: [X, "null"]`, `nullable: true`) into X + flag. */
export function unwrapNullable(doc: OpenApiDoc, input: JsonSchema | undefined): { schema: JsonSchema; nullable: boolean } {
  let schema = deref(doc, input);
  let nullable = schema.nullable === true;
  const union = schema.anyOf ?? schema.oneOf;
  if (union && union.length > 0) {
    const nonNull = union.filter((s) => deref(doc, s).type !== "null");
    if (nonNull.length < union.length) nullable = true;
    if (nonNull.length === 1) {
      const { anyOf: _a, oneOf: _o, ...rest } = schema;
      void _a;
      void _o;
      schema = { ...deref(doc, nonNull[0]), ...rest };
    }
  }
  if (Array.isArray(schema.type)) {
    const types = schema.type.filter((t) => t !== "null");
    if (types.length < schema.type.length) nullable = true;
    schema = { ...schema, type: types.length === 1 ? types[0] : types };
  }
  return { schema, nullable };
}

/** Short type label for tables: `string`, `integer ≥ 0`, `array<string>`, `WatchItem`, `string | integer`. */
export function schemaTypeLabel(doc: OpenApiDoc, input: JsonSchema | undefined, depth = 0): string {
  if (!input) return "dowolny";
  const name = refName(input.$ref);
  const { schema, nullable } = unwrapNullable(doc, input);
  let label: string;
  const union = schema.anyOf ?? schema.oneOf;
  if (union && union.length > 0 && depth < 3) {
    label = union.map((s) => schemaTypeLabel(doc, s, depth + 1)).join(" | ");
  } else if (name) {
    label = name;
  } else if (schema.enum) {
    label = schema.enum.map((v) => JSON.stringify(v)).join(" | ");
  } else if (schema.type === "array") {
    label = `array<${depth < 3 ? schemaTypeLabel(doc, schema.items, depth + 1) : "…"}>`;
  } else if (Array.isArray(schema.type)) {
    label = schema.type.join(" | ");
  } else {
    label = schema.type ?? (schema.properties ? "object" : "dowolny");
    if (schema.format) label += ` (${schema.format})`;
    const bounds: string[] = [];
    if (typeof schema.minimum === "number") bounds.push(`≥ ${schema.minimum}`);
    if (typeof schema.maximum === "number") bounds.push(`≤ ${schema.maximum}`);
    if (bounds.length) label += ` ${bounds.join(", ")}`;
  }
  return nullable ? `${label} | null` : label;
}

// ---------------------------------------------------------------------------
// Operations
// ---------------------------------------------------------------------------

function kindOf(schema: JsonSchema): ParamInfo["kind"] {
  const t = Array.isArray(schema.type) ? schema.type[0] : schema.type;
  if (t === "integer" || t === "number" || t === "boolean" || t === "array" || t === "object") return t;
  return "string";
}

export function paramInfo(doc: OpenApiDoc, p: OpenApiParameter): ParamInfo {
  const { schema, nullable } = unwrapNullable(doc, p.schema);
  const items = schema.type === "array" ? deref(doc, schema.items) : null;
  const enumValues = schema.enum ?? items?.enum ?? null;
  return {
    name: p.name,
    in: p.in,
    required: p.in === "path" ? true : Boolean(p.required),
    description: p.description ?? schema.description ?? "",
    deprecated: Boolean(p.deprecated || schema.deprecated),
    kind: kindOf(schema),
    enum: enumValues ? enumValues.map((v) => String(v)) : null,
    default: schema.default ?? p.example,
    nullable,
    min: typeof schema.minimum === "number" ? schema.minimum : null,
    max: typeof schema.maximum === "number" ? schema.maximum : null,
    typeLabel: schemaTypeLabel(doc, p.schema),
  };
}

const STREAM_PATH = /(^|\/)(stream)(\/|$)/;

export function isStreamingOperation(path: string, responses: ResponseInfo[]): boolean {
  if (responses.some((r) => r.contentType?.includes("text/event-stream"))) return true;
  return STREAM_PATH.test(path) && !path.endsWith("/stats");
}

function operationIdFallback(method: string, path: string): string {
  return `${method}_${path.replace(/[{}]/g, "").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "")}`;
}

/** Flatten `paths` into a sorted list of operations (tag, then path, then method order). */
export function parseOperations(doc: OpenApiDoc): Operation[] {
  const ops: Operation[] = [];
  for (const [path, item] of Object.entries(doc.paths ?? {})) {
    if (!item || typeof item !== "object") continue;
    const shared = ((item as Record<string, unknown>).parameters as OpenApiParameter[] | undefined) ?? [];
    for (const method of METHODS) {
      const raw = (item as Record<string, unknown>)[method] as RawOperation | undefined;
      if (!raw || typeof raw !== "object") continue;
      const rawParams = [...shared, ...(raw.parameters ?? [])].map((p) =>
        "$ref" in p && typeof p.$ref === "string" ? (resolvePointer(doc, p.$ref) as OpenApiParameter | undefined) : (p as OpenApiParameter),
      );
      const params = rawParams.filter((p): p is OpenApiParameter => Boolean(p && p.name)).map((p) => paramInfo(doc, p));
      let body: BodyInfo | null = null;
      const rb = raw.requestBody && "$ref" in raw.requestBody ? (resolvePointer(doc, String(raw.requestBody.$ref)) as RawOperation["requestBody"]) : raw.requestBody;
      if (rb && !("$ref" in rb) && rb.content) {
        const types = Object.keys(rb.content);
        const contentType = types.find((t) => t.includes("json")) ?? types[0] ?? "application/json";
        const media = rb.content[contentType];
        body = {
          contentType,
          required: Boolean(rb.required),
          schema: media?.schema ?? null,
          schemaName: refName(media?.schema?.$ref),
        };
        if (media?.example !== undefined && body.schema) body.schema = { ...body.schema, example: media.example };
      }
      const responses: ResponseInfo[] = Object.entries(raw.responses ?? {}).map(([status, r]) => {
        const types = Object.keys(r?.content ?? {});
        const contentType = types[0] ?? null;
        return {
          status,
          description: r?.description ?? "",
          contentType,
          schema: contentType ? (r?.content?.[contentType]?.schema ?? null) : null,
        };
      });
      const tags = raw.tags && raw.tags.length > 0 ? raw.tags : [UNTAGGED];
      ops.push({
        id: raw.operationId || operationIdFallback(method, path),
        method,
        path,
        tag: tags[0] ?? UNTAGGED,
        tags,
        summary: raw.summary ?? "",
        description: raw.description ?? "",
        deprecated: Boolean(raw.deprecated),
        params,
        body,
        responses,
        streaming: isStreamingOperation(path, responses),
      });
    }
  }
  const order = (m: string) => METHODS.indexOf(m as HttpMethod);
  ops.sort((a, b) => a.tag.localeCompare(b.tag) || a.path.localeCompare(b.path) || order(a.method) - order(b.method));
  return ops;
}

export interface OperationGroup {
  tag: string;
  label: string;
  operations: Operation[];
}

/** Group by first tag; groups keep first-seen order unless `sort` (default: alphabetical by tag, system first). */
export function groupByTag(ops: Operation[]): OperationGroup[] {
  const map = new Map<string, Operation[]>();
  for (const op of ops) {
    const list = map.get(op.tag) ?? [];
    list.push(op);
    map.set(op.tag, list);
  }
  return [...map.entries()]
    .sort(([a], [b]) => (a === UNTAGGED ? -1 : b === UNTAGGED ? 1 : a.localeCompare(b)))
    .map(([tag, operations]) => ({ tag, label: tagLabel(tag), operations }));
}

/** Lower-case, strip Polish (and other) diacritics so "zrodla" finds "źródła". */
export function normalizeText(text: string): string {
  return text
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/ł/g, "l");
}

/** All whitespace-separated terms must appear in method, path, summary, description, tag or operationId. */
export function filterOperations(ops: Operation[], query: string): Operation[] {
  const terms = normalizeText(query).split(/\s+/).filter(Boolean);
  if (terms.length === 0) return ops;
  return ops.filter((op) => {
    const hay = normalizeText([op.method, op.path, op.summary, op.description, op.tags.join(" "), tagLabel(op.tag), op.id].join(" "));
    return terms.every((t) => hay.includes(t));
  });
}

export function findOperation(ops: Operation[], idOrMethodPath: string | null | undefined): Operation | undefined {
  if (!idOrMethodPath) return undefined;
  const byId = ops.find((o) => o.id === idOrMethodPath);
  if (byId) return byId;
  const m = /^(get|post|put|patch|delete)\s+(\S+)$/i.exec(idOrMethodPath.trim());
  if (!m) return undefined;
  return ops.find((o) => o.method === (m[1] ?? "").toLowerCase() && o.path === m[2]);
}

// ---------------------------------------------------------------------------
// Example / skeleton generation
// ---------------------------------------------------------------------------

function firstExample(schema: JsonSchema): unknown {
  if (schema.example !== undefined) return schema.example;
  if (Array.isArray(schema.examples) && schema.examples.length > 0) return schema.examples[0];
  return undefined;
}

/**
 * JSON value that satisfies the schema's shape: explicit example → default → const → first enum → type skeleton.
 * Nullable unions take the non-null branch; objects include every property (required first) up to `maxDepth`.
 */
export function exampleFromSchema(doc: OpenApiDoc, input: JsonSchema | undefined | null, depth = 0, seen: Set<string> = new Set()): unknown {
  if (!input) return null;
  if (input.$ref) {
    if (seen.has(input.$ref) || depth > 6) return {};
    const next = new Set(seen);
    next.add(input.$ref);
    const own = firstExample(input);
    if (own !== undefined) return own;
    return exampleFromSchema(doc, deref(doc, input), depth, next);
  }
  const direct = firstExample(input);
  if (direct !== undefined) return direct;
  if (input.default !== undefined) return input.default;
  if (input.const !== undefined) return input.const;
  if (input.allOf && input.allOf.length > 0) {
    const merged: Record<string, unknown> = {};
    let scalar: unknown = undefined;
    for (const part of input.allOf) {
      const v = exampleFromSchema(doc, part, depth + 1, seen);
      if (v && typeof v === "object" && !Array.isArray(v)) Object.assign(merged, v);
      else scalar = v;
    }
    return Object.keys(merged).length > 0 ? merged : (scalar ?? {});
  }
  const union = input.anyOf ?? input.oneOf;
  if (union && union.length > 0) {
    const nonNull = union.find((s) => deref(doc, s).type !== "null");
    return nonNull ? exampleFromSchema(doc, nonNull, depth + 1, seen) : null;
  }
  if (input.enum && input.enum.length > 0) return input.enum[0];
  const type = Array.isArray(input.type) ? (input.type.find((t) => t !== "null") ?? "null") : input.type;
  switch (type) {
    case "string":
      if (input.format === "date-time") return "2026-11-22T00:00:00Z";
      if (input.format === "date") return "2026-11-22";
      if (input.format === "uri" || input.format === "url") return "https://example.org/";
      if (input.format === "binary") return "";
      return typeof input.minLength === "number" && input.minLength > 0 ? "x".repeat(Math.min(input.minLength, 8)) : "";
    case "integer":
    case "number":
      return typeof input.minimum === "number" ? input.minimum : 0;
    case "boolean":
      return false;
    case "null":
      return null;
    case "array":
      return depth > 5 ? [] : [exampleFromSchema(doc, input.items, depth + 1, seen)];
    case "object":
    default: {
      if (!input.properties) return {};
      if (depth > 5) return {};
      const req = new Set(input.required ?? []);
      const keys = Object.keys(input.properties).sort((a, b) => Number(req.has(b)) - Number(req.has(a)));
      const out: Record<string, unknown> = {};
      for (const key of keys) out[key] = exampleFromSchema(doc, input.properties[key], depth + 1, seen);
      return out;
    }
  }
}

export function bodySkeleton(doc: OpenApiDoc, op: Operation): string {
  if (!op.body || !op.body.contentType.includes("json")) return "";
  return JSON.stringify(exampleFromSchema(doc, op.body.schema), null, 2);
}

/** Flat field list of an object schema for the reference tables: name, type, required, description. */
export function schemaFields(doc: OpenApiDoc, input: JsonSchema | null | undefined): { name: string; type: string; required: boolean; description: string }[] {
  const { schema } = unwrapNullable(doc, input ?? undefined);
  if (!schema.properties) return [];
  const req = new Set(schema.required ?? []);
  return Object.entries(schema.properties).map(([name, s]) => {
    const d = deref(doc, s);
    let description = d.description ?? "";
    if (d.default !== undefined) description = `${description}${description ? " " : ""}(domyślnie ${JSON.stringify(d.default)})`;
    return { name, type: schemaTypeLabel(doc, s), required: req.has(name), description };
  });
}

// ---------------------------------------------------------------------------
// Request building
// ---------------------------------------------------------------------------

export type ParamValues = Record<string, string>;

/** Default form values: explicit default (stringified) for every parameter, "" otherwise. */
export function defaultParamValues(op: Operation): ParamValues {
  const out: ParamValues = {};
  for (const p of op.params) {
    if (p.in !== "path" && p.in !== "query") continue;
    out[`${p.in}:${p.name}`] = p.default === undefined || p.default === null ? "" : Array.isArray(p.default) ? p.default.join(",") : String(p.default);
  }
  return out;
}

/** Substitute `{name}` path params (each value percent-encoded). Missing values stay as `{name}`. */
export function buildPath(path: string, values: ParamValues): string {
  return path.replace(/\{([^}]+)\}/g, (whole, name: string) => {
    const v = values[`path:${name}`];
    return v === undefined || v === "" ? whole : encodeURIComponent(v);
  });
}

/**
 * Query string (without `?`) for the non-empty query params, in declaration order. Arrays (comma separated in the
 * form) become repeated keys (`?a=1&a=2`, FastAPI's `list[str]` convention). Values that equal the parameter's
 * default are still sent so the request is explicit.
 */
export function buildQuery(op: Operation, values: ParamValues): string {
  const qs = new URLSearchParams();
  for (const p of op.params) {
    if (p.in !== "query") continue;
    const v = values[`query:${p.name}`];
    if (v === undefined || v.trim() === "") continue;
    if (p.kind === "array") {
      for (const part of v.split(",").map((s) => s.trim()).filter(Boolean)) qs.append(p.name, part);
    } else {
      qs.append(p.name, v.trim());
    }
  }
  return qs.toString();
}

export function buildRequestPath(op: Operation, values: ParamValues): string {
  const q = buildQuery(op, values);
  return `${buildPath(op.path, values)}${q ? `?${q}` : ""}`;
}

/** Polish validation messages for the form: missing required, non-numeric numbers, path params left empty. */
export function validateParams(op: Operation, values: ParamValues): string[] {
  const problems: string[] = [];
  for (const p of op.params) {
    if (p.in !== "path" && p.in !== "query") continue;
    const v = (values[`${p.in}:${p.name}`] ?? "").trim();
    if (!v) {
      if (p.required) problems.push(`Brak wymaganego parametru „${p.name}”.`);
      continue;
    }
    if ((p.kind === "integer" || p.kind === "number") && !Number.isFinite(Number(v))) problems.push(`„${p.name}” musi być liczbą.`);
    if (p.kind === "integer" && Number.isFinite(Number(v)) && !Number.isInteger(Number(v))) problems.push(`„${p.name}” musi być liczbą całkowitą.`);
    if (p.min !== null && Number(v) < p.min) problems.push(`„${p.name}” musi być ≥ ${p.min}.`);
    if (p.max !== null && Number(v) > p.max) problems.push(`„${p.name}” musi być ≤ ${p.max}.`);
  }
  return problems;
}

/** Validate a JSON body text; returns a Polish error or null. Empty is fine when the body is optional. */
export function validateJsonBody(text: string, required: boolean): string | null {
  if (!text.trim()) return required ? "Treść żądania jest wymagana (JSON)." : null;
  try {
    JSON.parse(text);
    return null;
  } catch (e) {
    return `Niepoprawny JSON: ${e instanceof Error ? e.message : String(e)}`;
  }
}
