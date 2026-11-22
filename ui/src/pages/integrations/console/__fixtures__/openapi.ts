/** Small OpenAPI 3.1 document shaped like FastAPI's output (hydra/api.py), for console/docs tests. */
import type { OpenApiDoc } from "../../../../lib/openapi";

export const MINI_OPENAPI: OpenApiDoc = {
  openapi: "3.1.0",
  info: { title: "HYDRA NEXUS", version: "0.9.0" },
  paths: {
    "/health": { get: { summary: "Health", operationId: "health_health_get", responses: { "200": { description: "Successful Response", content: { "application/json": { schema: { type: "object" } } } } } } },
    "/v1/campaigns": {
      get: {
        tags: ["task"],
        summary: "List Campaigns",
        operationId: "list_campaigns_v1_campaigns_get",
        parameters: [
          { name: "limit", in: "query", required: false, schema: { type: "integer", minimum: 1, maximum: 500, default: 50 } },
          { name: "run_id", in: "query", required: false, schema: { anyOf: [{ type: "string" }, { type: "null" }] } },
          { name: "order", in: "query", schema: { type: "string", enum: ["bits", "size"] } },
        ],
        responses: { "200": { description: "OK" } },
      },
    },
    "/v1/campaigns/{campaign_id}/stix": {
      get: {
        tags: ["exports"],
        summary: "Campaign Stix",
        operationId: "campaign_stix",
        parameters: [{ name: "campaign_id", in: "path", required: true, schema: { type: "string" } }],
        responses: { "200": { description: "OK" } },
      },
    },
    "/v1/watchlist": {
      post: {
        tags: ["task"],
        summary: "Change Watchlist",
        operationId: "change_watchlist_v1_watchlist_post",
        requestBody: { required: true, content: { "application/json": { schema: { $ref: "#/components/schemas/WatchlistChange" } } } },
        responses: { "200": { description: "OK" } },
      },
    },
    "/v1/stream": {
      get: {
        tags: ["stream"],
        summary: "Stream",
        description: "Server-Sent Events of materialised deltas.",
        operationId: "stream_v1_stream_get",
        parameters: [{ name: "last_event_id", in: "query", schema: { anyOf: [{ type: "integer", minimum: 0 }, { type: "null" }] } }],
        responses: { "200": { description: "OK", content: { "application/json": { schema: {} } } } },
      },
    },
    "/v1/old": { delete: { tags: ["task"], summary: "Old thing", operationId: "old_delete", deprecated: true, responses: { "200": { description: "OK" } } } },
  },
  components: {
    schemas: {
      WatchlistChange: {
        type: "object",
        properties: {
          add: { type: "array", items: { $ref: "#/components/schemas/WatchItem" } },
          remove: { type: "array", items: { $ref: "#/components/schemas/WatchItem" } },
          note: { anyOf: [{ type: "string" }, { type: "null" }], title: "Note" },
        },
        required: ["add"],
      },
      WatchItem: {
        type: "object",
        properties: { kind: { type: "string", enum: ["keyword", "domain"] }, value: { type: "string", minLength: 2 } },
        required: ["kind", "value"],
      },
      Node: { type: "object", properties: { child: { $ref: "#/components/schemas/Node" }, name: { type: "string", default: "n" } } },
    },
  },
};
