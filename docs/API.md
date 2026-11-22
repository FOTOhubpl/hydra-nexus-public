# HYDRA NEXUS REST API

Status: 2026-11-22 (Europe/Warsaw). This file documents the integrations and webhooks
routers in full and indexes the other groups. The complete, always-current reference is the OpenAPI document the
app serves at `/openapi.json` (`/docs` for Swagger UI); the in-app screen Integracje & API → Konsola API and
→ Dokumentacja render it. Polish user guide: `docs/INTEGRATIONS.md`.

## 1. Access model

- The API binds to loopback only (`HYDRA_API_HOST`, default `127.0.0.1:8000`; `0.0.0.0` is accepted only inside the
  compose `gui` container with `HYDRA_IN_CONTAINER=1`, whose port is published on the host's 127.0.0.1).
- An outermost ASGI guard in `hydra/api.py` refuses with 403, before routing:
  - a `Host` header that is not `127.0.0.1`, `localhost` or `::1` (DNS rebinding);
  - for unsafe methods (POST/PUT/PATCH/DELETE): an `Origin` whose host is not local, `Origin: null`, or
    `Sec-Fetch-Site: cross-site` (CSRF from a page open in the operator's browser).
- Authentication (AUTH-LOGIN, 2026-11-22, details [SECURITY_AUTH.md](SECURITY_AUTH.md)): with `HYDRA_AUTH=1`
  (default) every `/v1/*` path, `/docs`, `/redoc` and `/openapi.json` needs either the panel session cookie
  `hydra_session` (set by `POST /v1/auth/login`) or a personal access token `Authorization: Bearer hnx_...`
  (UI "Tokeny API", `POST /v1/auth/tokens`, or `hydra user token <user>`). Public: `GET /health` (reduced body),
  `POST /v1/auth/login`, `GET /v1/auth/session`, `POST /v1/auth/logout`, `POST /v1/integrations/webhooks/sink`
  (HMAC-verified). Session writes send `X-CSRF-Token` (value of the `hydra_csrf` cookie); bearer requests do not.
  Roles: viewer read-only, analyst analysis/import/review/connector runs, admin users, connector secrets/connect/
  enable/presets and webhook endpoint configuration. Errors: 401 `auth_required`, 403 `csrf` / `forbidden` /
  `password_change_required` in `{"detail": {"code", "message"}}`. Provider keys are never accepted in a query
  string and never returned.

  ```bash
  export HYDRA_API_TOKEN=$(uv run hydra user token admin --name automation --days 30)
  curl -s -H "Authorization: Bearer $HYDRA_API_TOKEN" http://127.0.0.1:8000/v1/integrations | head
  ```
- The UI dev server (`just ui`, 127.0.0.1:5173) proxies `/api/*` to the API; the UI therefore calls `/api/v1/...`.
  Direct clients call `/v1/...`.
- Errors: FastAPI `{"detail": ...}`. Polish messages are meant for display.

## 2. Integrations — `/v1/integrations` (tag `integrations`)

Router `hydra/api_integrations.py`. Connector descriptors come from `hydra/integrations/registry.py`.

Status codes used by this group: 404 unknown connector or preset; 409 action on a `planned` connector (connect, test,
run, export, secret); 422 invalid input (unknown secret name, bad format, missing run/campaign); 503 outbound action
while offline, body `{"detail": "<Polish message>", "status": "offline"}`. `/test` and `/connect` answer 200 with
`ok: false, status: "offline"` instead of 503.

<!-- BEGIN INTEGRATIONS ROUTES -->
| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/v1/integrations` | — | `{offline, enrich, generated_at, categories[], levels[], presets[], connectors[ConnectorOut]}` |
| GET | `/v1/integrations/presets` | — | `[Preset]` |
| POST | `/v1/integrations/presets/{preset_id}/apply` | — | `{preset, results: [{connector, ok, status, message_pl}]}` |
| GET | `/v1/integrations/{id}` | — | `ConnectorOut` + `{audit: [last 20 INTEGRATION_* events {seq, ts, type, payload}], runs: [...]}` |
| PUT | `/v1/integrations/{id}/secret` | `{name, value}` | `{name, configured: true, source: "file", last4}` |
| DELETE | `/v1/integrations/{id}/secret?name=X` | — | `{name, configured: false}` |
| POST | `/v1/integrations/{id}/enable` | `{enabled: bool}` | `ConnectorOut` |
| POST | `/v1/integrations/{id}/test` | `{}` | `{ok, status: ok/offline/not_configured/error/disabled, message_pl, ms, at}` |
| POST | `/v1/integrations/{id}/connect` | `{secrets?: {NAME: value}}` | `{ok, steps: [{id: secret/test/enable/export, ok, message_pl}], connector}` |
| POST | `/v1/integrations/{id}/preview` | `{run_id?, campaign_id?, format?, limit?}` | dry run, nothing sent (see below) |
| GET | `/v1/integrations/{id}/export?format=&run_id=&campaign_id=` | — | file download (`Content-Disposition`), audited |
| POST | `/v1/integrations/{id}/run` | `{run_id?, campaign_id?, indicators?: [str]}` | lookup or push result; live connectors and push-capable exports only |
<!-- END INTEGRATIONS ROUTES -->

`ConnectorOut`: `id, name, category, direction, auth, level, formats, description_pl, setup_pl[], notes_pl,
docs_url, licence_pl, rate_limit_pl, rate_limit_per_min, data_out[], data_out_pl, never_sent_pl,
secrets: [{name, configured, source: "env"|"file"|null, last4}], status, enabled, can_connect, can_test, can_run,
can_export, last_test, last_run, usage: {requests_60m, limit_per_min}|null`.

- `category`: `enrichment`, `threat_intel`, `ad_transparency`, `outbound_sharing`, `database_sink`, `notification`.
- `direction`: `inbound`, `outbound`, `both`. `auth`: `none`, `api_key`, `oauth_token`, `basic`, `hmac`.
- `level`: `live` (implemented; tested only against fixtures hand-written from the provider documentation, no
  production calls — UI note: "zaimplementowany; testowany na fiksturach z dokumentacji, bez wywołań produkcyjnych"),
  `export-format`, `planned`. `status`: `configured`, `not_configured`, `offline`, `disabled`,
  `planned` (`offline` = live connector, configured, while `HYDRA_OFFLINE=1`; export-only connectors without secrets
  are `configured` because they work offline).
- `data_out` field ids: `domain, url, ip, sha256, phash, campaign_id, decision, score_bits, first_seen, last_seen,
  count, keyword, ad_id, page_id, asn, cert_fingerprint`. Never: artefact bytes, OCR text, organiser files, file
  names, secrets.

Preview response: `{connector, format, method, destination ("scheme://host/path" or "plik"), headers (secrets shown
as "•••• + last4"), content_type, bytes, sha256, fields_sent[], redactions[], payload (JSON or text, truncated at
200 KB with truncated: true)}`.

`Preset`: `{id, name_pl, description_pl, connectors: [id]}`. Presets: `cert-misp`, `siem`, `ads-meta`, `reputation`.

Behaviour as implemented (2026-11-22):

1. Status codes: 404 unknown connector; 409 for connect/test/run/export/secret on a `planned` connector, for `run`
   on a connector that is not enabled, and for test/connect on `webhook-generic` (managed by `/v1/integrations/webhooks`);
   503 with a Polish `detail` for `run` while `HYDRA_OFFLINE=1`. `test` and `connect` answer 200 with
   `ok: false, status: "offline"` instead. `test` does not require the connector to be enabled.
2. `cert-pl` matches against the local snapshot of the CERT Polska warning list, so its test and run work offline.
3. Preview of a lookup connector (no export format) returns `format: "requests"` with the planned requests and masked
   headers; the body may carry `indicators` (max 100). Lookups taken from a run are capped at 25 indicators.
4. `ConnectorOut` also carries `base_url`. `run` returns `{ok, status, message_pl, connector, ms, results: [verdict],
   summary, rejected, sent}`; a verdict is `{indicator, kind, provider, verdict: malicious|suspicious|clean|unknown,
   details, fetched_at, cached}`. `export` accepts `limit` and returns an `X-Hydra-Sha256` header.
5. Export-format connectors with optional push secrets (misp, elasticsearch, splunk-hec, slack) report
   `not_configured` until the secrets are set; file export works regardless, also offline.
6. Audit event types: `INTEGRATION_REQUEST`, `INTEGRATION_TEST`, `INTEGRATION_RUN`, `INTEGRATION_SECRET` (secret
   name only), `INTEGRATION_ENABLE`, `INTEGRATION_CONNECT`, `INTEGRATION_PRESET`, `INTEGRATION_EXPORT` (plus the
   generic `EXPORT`), `INTEGRATION_DELIVERY`.
7. crtsh, rdap, dns and the Meta Ad Library scheduler pull keep their existing `hydra.enrich` HTTP path (same
   netguard checks); they are audited per run, not per request.

Outbound requests go only through `hydra/integrations/http.py`: offline check, enabled check, `netguard.check_url`
(public addresses only), per-connector rate limit, 20 s timeout, 5 MB response cap, no redirects. Every attempt is
audited as `INTEGRATION_REQUEST {connector, method, host, path (no query), status, ms, bytes_out, bytes_in,
payload_sha256}`.

## 3. Webhooks — `/v1/integrations/webhooks` (tag `webhooks`)

Router `hydra/api_webhooks.py`, included before the integrations router so `/webhooks` is not taken as a connector
id.

<!-- BEGIN WEBHOOK ROUTES -->
| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/v1/integrations/webhooks` | — | `{delivery_possible, offline, event_types[], endpoints[Endpoint], signature: {header, algo, scheme_pl}}` |
| POST | `/v1/integrations/webhooks` | `{name, url, events[], enabled, secret}` | `Endpoint` (201) |
| PATCH | `/v1/integrations/webhooks/{endpoint_id}` | `{name?, url?, events?, enabled?, secret?}` | `Endpoint` |
| DELETE | `/v1/integrations/webhooks/{endpoint_id}` | — | `{deleted: true}` |
| POST | `/v1/integrations/webhooks/{endpoint_id}/test` | — | `Delivery` (event `ping`) |
| GET | `/v1/integrations/webhooks/deliveries?endpoint_id=&limit=` | — | `{deliveries: [Delivery]}` |
| POST | `/v1/integrations/webhooks/deliveries/{delivery_id}/retry` | — | `Delivery` (dead letter requeued) |
| POST | `/v1/integrations/webhooks/sink?endpoint_id=` | signed webhook body | built-in loopback receiver; checks the HMAC |
| GET | `/v1/integrations/webhooks/sink` | — | `{receipts: [{at, event, delivery_id, signature_ok, bytes}]}` (last 50) |
<!-- END WEBHOOK ROUTES -->

`Endpoint`: `{id, name, url_redacted (scheme://host/… + last 4 characters of the path), events[], enabled, loopback,
secret: {configured, last4}, created_at, last_delivery}`. The secret is generated by the client and never echoed.

`Delivery`: `{id, endpoint_id, event, status: pending|delivered|retrying|dead|blocked_offline, attempts,
max_attempts, last_status_code, last_error, next_retry_at, payload_sha256, created_at, delivered_at}`.

Event types: `ping`, `delta`, `job`, `watch`, `review`, `export`. Request headers: `Content-Type: application/json`,
`X-Hydra-Event`, `X-Hydra-Delivery`, `X-Hydra-Timestamp` (unix seconds), `X-Hydra-Signature: sha256=<hex
HMAC-SHA256(secret, "{timestamp}.{raw body}")>`. Body: `{"id", "event", "created_at", "hydra_version", "data"}`.
Receivers compare in constant time and reject `|now - timestamp| > 300 s` (code samples: `docs/INTEGRATIONS.md` §7).

Delivery: up to 5 attempts, backoff base `HYDRA_WEBHOOK_BACKOFF_S` (default 2 s: +2, +4, +8, +16 s), retry on
network errors, 429 and 5xx; other non-2xx answers are dead at once; redirects are not followed. Loopback URLs are
delivered even offline; other URLs need `HYDRA_OFFLINE=0`, an enabled endpoint and a passing `netguard` check,
otherwise `blocked_offline`. Each attempt is audited as `INTEGRATION_DELIVERY` (host only). Storage:
`~/.config/hydra/webhooks.json` and `webhook-<id>.env` (0600); log `<state_dir>/integrations/deliveries.jsonl`.

## 4. Other API groups (index)

Paths below are the route decorators in the named files (2026-11-22); request and response schemas are in
`/openapi.json`.

| Group | File | Paths (prefix `/v1` unless shown) |
|---|---|---|
| Health, import, jobs, artefacts, audit | `hydra/api.py` | `GET /health`; `POST /import`, `POST /import/paths`; `GET /jobs`, `/jobs/{job_id}`; `GET /artifacts`, `/artifacts/{id}`, `/artifacts/{id}/content`; `GET /audit/verify` |
| Analysis results | `hydra/api_task.py` | `/runs`, `/runs/latest`, `/runs/{run_id}`, `/status`, `/incidents`, `/campaigns`, `/campaigns/{id}`, `/campaigns/{id}/export`, `/campaigns/{id}/brief`, `/graph`, `/pairs`, `/pairs/{a}/{b}`, `/pairs/{a}/{b}/explain`, `/assignments`, `POST /analyse`, `/analyse/{job_id}`, `/reviews`, `/search`, `/watchlist`, `/scheduler/status`, `POST /scheduler/run`, `/export/neo4j`, `/campaign-groups`, `/dashboard`, `/analytics/*`, `/corpora`, `/demo/*` |
| Graph explorer | `hydra/api_graph.py` | `/v1/graph/explore`, `/overview`, `/campaign/{id}`, `/neighbors/{incident_id}`, `/viewport`, `/edge/{a}/{b}`, `/path`, `/indicators`, `POST /export`, `/campaigns/{id}/summary` |
| Exports | `hydra/api_exports.py` | `/campaigns/{id}/abuse-package`, `/campaigns/{id}/stix`, `/runs/{run_id}/stix`, `/runs/{run_id}/lead-times`, `/campaigns/{id}/pivots`, `/export/dataset`, `/exports/signing-key`, `POST /exports/verify` |
| Integration contract (v1) | `hydra/api_contract.py` | `/jobs/{job_id}/result` (schema `hydra.job_result.v1`), `/jobs/{job_id}/report`, `/jobs/{job_id}/package` (signed ZIP); `Idempotency-Key` on `POST /analyse` — contract: `docs/INTEGRATION_CONTRACT.md` |
| Batch assignment | `hydra/api_batch.py` | `POST /assign-batch`, `/assign-batch/{key}`, `/assign-batches` |
| Live stream (SSE) | `hydra/api_stream.py` | `GET /stream`, `/stream/stats` — contract: `docs/ui/STREAM_API.md` |
| HYDRA Watch | `hydra/api_watch.py` | `/v1/watch/status`, `/events`, `/alerts`, `/stream`, `/recordings`, `POST /replay`, `/live`, `/stop`, `/fetch` |
| Hunt / hypotheses | `hydra/api_hunt.py` | `POST /campaigns/{id}/hunt-plan`, `/hypotheses`, `/hypotheses/{hid}`, `/next-action`, `/verify`, `/hunt/metrics`, `/hunt/replay` |
| Review | `hydra/api_review.py` | `/review/queue`, `/review/unsure` (+ `/undo`), `/incidents/{id}/similar` |
| Artefact viewer | `hydra/api_artifacts.py` | `/artifacts/{id}/raw`, `/text`, `/thumb`, `/frame`, `/info`, `/genes`, `/har`, `/transcript`; `/materials` |
| Explanations | `hydra/api_visual.py`, `api_likeness.py`, `api_lineage.py` | `/system/flow`, `/pairs/{a}/{b}/diff`, `/pairs/{a}/{b}/provenance`, `/revisions`, `/campaigns/{id}/lifecycle`, `/genes/explain`, `/corpora/stats`, `/incidents/{id}/likeness`, `/campaigns/{id}/lineage` |
| KONTRA lab | `hydra/api_kontra.py` | `/v1/lab/receiver`, `/packages/{id}` (+ `report.md`, `report.html`, `POST /send`), `/{incident_id}`, `POST /{incident_id}/run`, `POST /{incident_id}/package` |

SSE: `GET /v1/stream?run_id=` returns `text/event-stream` with events `hello`, `delta`, `reset`, `job`, `watch`; the
event `id` is the audit `seq`, so clients resume with `Last-Event-ID`. Full contract: `docs/ui/STREAM_API.md`.

## 5. Data formats produced

| Format id | Content type | Shape | Used by |
|---|---|---|---|
| `misp` | `application/json` | MISP event: `Event{info, threat_level_id, analysis, distribution: 0, Attribute[{type: domain/url/ip-dst/sha256, category, value, to_ids, comment}], Tag[tlp:amber]}` | `misp` |
| `stix21` | `application/json` | STIX 2.1 bundle (`hydra.export_stix`, `stix2` library) | `opencti-taxii` |
| `xarf` | `application/zip` | one XARF v4 report per campaign host + Polish notes (`hydra.export_abuse`) | `xarf` |
| `es_bulk` | `application/x-ndjson` | action line + document line per indicator, final `\n` | `elasticsearch` |
| `ndjson` | `application/x-ndjson` | one indicator per line | `elasticsearch`, `siem-generic` |
| `siem_json` | `application/json` | Splunk HEC events `{"time", "host": "hydra-nexus", "source": "hydra", "sourcetype": "hydra:indicator", "event": {...}}` | `splunk-hec`, `siem-generic` |
| `csv` | `text/csv` | one indicator per row, formula-injection safe | `csv-export` |
| `sql` | `application/sql` | `CREATE TABLE` + `INSERT` for `hydra_indicators`, `hydra_campaigns` (SQLite/PostgreSQL) | `sql-export` |
| `neo4j` | text | Cypher script (`hydra.export_neo4j`) | `neo4j` |
| `evidence` | `application/zip` | dataset export with `SIGNED_MANIFEST.json` + Ed25519 `.sig` | `evidence-package` |
| `webhook_json` | `application/json` | webhook body (§3) or a short Slack `{"text": ...}` summary | `webhook-generic`, `slack` |

Every builder returns the bytes plus `fields_sent`, `redactions` and `sha256`, which the preview shows.

## 6. CLI equivalents

Commands from `hydra/cli.py`, `hydra/cli_task.py`, `hydra/cli_export.py` (run after `. scripts/env.sh`, via
`uv run hydra ...`). There is no CLI for connectors or webhooks; use the API or the UI.

| Task | CLI | API |
|---|---|---|
| Start the API | `hydra serve` (`just serve`) | — |
| Import files/folders/ZIPs | `hydra import PATHS...` (`just import`) | `POST /v1/import`, `/v1/import/paths` |
| Status | `hydra status` | `GET /v1/status` |
| Re-hash originals + verify the audit chain | `hydra verify` (`just verify`) | `GET /v1/audit/verify` |
| Analyse a dataset | `hydra analyse [PATHS]` | `POST /v1/analyse` |
| Assign one new item | `hydra assign ITEM [--run] [--kind]` | — (UI upload) |
| Assign a folder of new items | `hydra assign-batch FOLDER --out DIR` | `POST /v1/assign-batch` |
| Neo4j export | `hydra export-neo4j OUT [--run]` | `GET /v1/export/neo4j`, `GET /v1/integrations/neo4j/export` |
| Signed dataset export | `hydra export-dataset [RUN] OUT_DIR` | `GET /v1/export/dataset`, `GET /v1/integrations/evidence-package/export` |
| Verify a signed export | `hydra verify-export PACKAGE [--fingerprint] [--local-key] [--json]` | `POST /v1/exports/verify` |
| Scheduler | `hydra scheduler status`, `hydra scheduler run`, `hydra scheduler watch` | `GET /v1/scheduler/status`, `POST /v1/scheduler/run` |

## 7. Environment switches

| Variable | Default | Effect on this API |
|---|---|---|
| `HYDRA_OFFLINE` | `1` (`scripts/env.sh`) | `1`: every integration network call fails closed (`offline`); file exports still work |
| `HYDRA_ENRICH` | `0` | `1` needed for live enrichment connectors (crt.sh, RDAP, DNS, Meta Ad Library) |
| `HYDRA_SECRETS_DIR` | `~/.config/hydra` | directory of `<id>.env`, `webhooks.json`, `webhook-<id>.env` |
| `HYDRA_WEBHOOK_BACKOFF_S` | `2` | webhook retry backoff base in seconds |
| `HYDRA_API_HOST`, `HYDRA_API_PORT` | `127.0.0.1`, `8000` | bind address (loopback only) |
| `HYDRA_AUTH` | `1` | `1`: login/session/token required (SECURITY_AUTH.md); `0`: open API (unit tests only) |
| `HYDRA_ADMIN_PASSWORD` | unset | first-start admin password instead of the generated `ADMIN_CREDENTIALS.txt` |
| `HYDRA_SESSION_IDLE_H`, `HYDRA_SESSION_ABS_H` | `8`, `24` | session idle and absolute lifetime in hours |
