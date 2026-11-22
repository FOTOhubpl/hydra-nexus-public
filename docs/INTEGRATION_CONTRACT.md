# HYDRA NEXUS integration contract (v1)

Status: implemented and tested (2026-11-22). Scope: one complete path for an external
system, from submitting material to verifying a signed package and receiving an HMAC-signed notification. No new
connectors. The only new server code is `hydra/api_contract.py` (result, report, package endpoints and
`Idempotency-Key` handling) and the hook-up in `POST /v1/analyse` (`hydra/api_task.py`). Scoring code is not
touched. Reference client: `examples/client/hydra_client.py` (standard library only), walk-through in
`examples/client/README.md`. Tests: `tests/contract/` (19 tests, real analysis, real HTTP on 127.0.0.1).

## 1. Sequence

```
External client                      HYDRA API (127.0.0.1)                         Loopback receiver
      |                                     |                                              |
      |  (admin, once) POST /v1/integrations/webhooks {url, events:["job"], secret}         |
      |------------------------------------>|                                              |
      |  GET /v1/exports/signing-key         |   -> pin fingerprint out of band             |
      |------------------------------------>|                                              |
      |  POST /v1/analyse                    |                                              |
      |  Authorization: Bearer hnx_...       |                                              |
      |  Idempotency-Key: <k>                |                                              |
      |  multipart files | JSON {url|hint|path}                                              |
      |------------------------------------>|  202 {job_id, status:"queued", mode}          |
      |<------------------------------------|  (same key + same request -> same job_id,    |
      |  (timeout? retry with the same key)  |   Idempotency-Replayed: true)                |
      |                                     |                                              |
      |  GET /v1/analyse/{id}/progress       |  stage, percent (never decreases)            |
      |  GET /v1/jobs/{id}/result            |  hydra.job_result.v1, terminal=false          |
      |------------------------------------>|                                              |
      |                                     |  job finishes -> audit event JOB_DONE         |
      |                                     |  dispatcher -> POST <url>                     |
      |                                     |  X-Hydra-Signature: sha256=HMAC(ts.body)      |
      |                                     |--------------------------------------------->|
      |                                     |  2xx delivered | 429/5xx/network: retry       |
      |                                     |  (5 attempts, backoff) | other 4xx: dead      |
      |                                     |<---------------------------------------------|
      |  GET /v1/jobs/{id}/result            |  terminal=true, result + evidence             |
      |  GET /v1/jobs/{id}/report            |  Markdown                                     |
      |  GET /v1/jobs/{id}/package           |  signed ZIP (Ed25519)                         |
      |------------------------------------>|                                              |
      |  POST /v1/exports/verify?fingerprint=<pinned>   (or `hydra verify-export`)            |
      |------------------------------------>|  {ok, errors[], files_checked, key_fingerprint}|
```

## 2. Endpoints

All paths are under `/v1` (the UI proxy adds `/api`). OpenAPI: `GET /openapi.json`, Swagger UI `GET /docs`, ReDoc
`GET /redoc`, all behind the same authentication. The contract operations are tagged `contract` (new), `task`
(submit), `exports` (signing key, verify) and `webhooks`.

| Step | Method and path | Role | Source | Notes |
|---|---|---|---|---|
| token | `POST /v1/auth/tokens` | any (session) | `hydra/api_auth.py` | personal access token `hnx_...`, shown once |
| submit | `POST /v1/analyse` | analyst | `hydra/api_task.py` | multipart `files` (files or one ZIP) or JSON `{url}`/`{hint}`/`{path}`; optional `Idempotency-Key` |
| progress | `GET /v1/analyse/{id}/progress` (`/stream` = SSE) | viewer | `hydra/api_progress.py` | stage, percent, import counters |
| result | `GET /v1/jobs/{id}/result` | viewer | `hydra/api_contract.py` | schema `hydra.job_result.v1`, `schema_version` "1" |
| report | `GET /v1/jobs/{id}/report` | viewer | `hydra/api_contract.py` | `text/markdown`; 409 until done |
| package | `GET /v1/jobs/{id}/package` | viewer | `hydra/api_contract.py` | ZIP `hydra-job-<id>/` with `result.json`, `evidence.json`, `report.md`, `SIGNED_MANIFEST.json`, `SIGNED_MANIFEST.sig` |
| key | `GET /v1/exports/signing-key` | viewer | `hydra/api_exports.py` | `{fingerprint: "SHA256:..."}` |
| verify | `POST /v1/exports/verify?fingerprint=` | viewer | `hydra/api_exports.py` | raw ZIP body |
| webhook | `POST /v1/integrations/webhooks`, `GET .../deliveries`, `POST .../deliveries/{id}/retry` | admin | `hydra/api_webhooks.py` | event `job` = job finished |
| graph | `GET /v1/export/neo4j?format=zip\|cypher\|nodes_csv\|edges_csv` | viewer | `hydra/api_task.py` | see section 7 |

## 3. Authentication and roles

`Authorization: Bearer hnx_...` (personal access token). Tokens are not ambient credentials, so no CSRF header is
needed. Roles (`hydra/api_auth.py`): viewer = reads + `POST /v1/exports/verify`; analyst = submit; admin = webhook
configuration and users. Tested: no token and an unknown token -> 401 on every contract path including
`/openapi.json`; viewer submit -> 403; analyst webhook configuration -> 403.

## 4. Idempotency

- Header `Idempotency-Key`: 1-128 characters of `A-Z a-z 0-9 . _ : -`; anything else -> 400
  `invalid_idempotency_key`.
- Scope: the authenticated user. Two users may use the same key independently.
- Stored: sha256(user id, key) and sha256 of the request (canonical JSON body, or path + sha256 of every uploaded
  file plus the `kind` field) inside the job's `params` JSON. The raw key is not stored. No DB migration.
- Same key, same request: 202 with the original `job_id`, `replayed: true`, header `Idempotency-Replayed: true` and
  `Location: /v1/jobs/{id}/result`. No second job, no second spool.
- Same key, different request: 409 `idempotency_conflict` with the original `job_id`.
- A retry that arrives while the first request is still being processed waits on a per-key lock and then replays.
  Tested by delaying the first answer past the client timeout: the client retries and gets the same job; exactly
  one job row exists.
- Keys do not expire (they live as long as the job row). Clients should use a fresh random key per logical submit.
- Without the header the behaviour is unchanged, and the reference client does not retry a POST without a key.

## 5. Result schema `hydra.job_result.v1`

```json
{
  "schema": "hydra.job_result.v1", "schema_version": "1", "product_version": "0.1.0",
  "job_id": "7a5d7efc0cfa4a9f", "kind": "analyse", "status": "done", "terminal": true,
  "progress": {"total": 0, "done": 0, "failed": 0},
  "created_at": "...", "started_at": "...", "finished_at": "...",
  "error": null,
  "result": {"mode": "files", "run_id": "assign-...", "incident_id": "new-47896fe7", "decision": "new_campaign",
             "campaign_id": null, "group_id": null, "reason_pl": "...", "model": "assign_open_set@1",
             "candidates": [{"campaign_id": "syn-c1", "probability": 0.001, "support": 0, "best_pair_bits": 0.0}],
             "metrics": null},
  "evidence": {"genes_total": 20, "genes_by_family": {"VIS": {"observed": 1, "unavailable": 2}},
               "artifacts_by_role": {"feed_screenshot": 1}, "pairs_scored": 5,
               "top_pairs": [{"other": "syn-1", "probability": 0.001, "total_bits": 0.0, "hypothesis": "independent"}]},
  "links": {"self": "...", "progress": "...", "report": "...", "package": "...", "verify": "...", "signing_key": "..."},
  "disclaimer": {"pl": "...", "en": "..."}
}
```

(Values from the contract test run on the synthetic corpus, 2026-11-22.) Rules:

- 200 for every known job; poll until `terminal` is true. `result` and `evidence` are null until an analyse job is
  `done`; `error {code, message}` is set when it `failed`.
- `decision` is one of `assign`, `new_campaign`, `abstain`. `probability` is a calibrated score, `bits`
  (`best_pair_bits`, `total_bits`) measure evidence strength and are not probabilities. A result is a heuristic
  assessment for analyst verification, not attribution.
- Versioning: within version 1 fields are only added, never removed or retyped; the test suite pins the exact key
  set of the envelope and of `result`. A breaking change publishes `hydra.job_result.v2` and keeps v1 selectable.
  Clients may pin with `?schema_version=1` or `Accept-Version: 1`; an unsupported version -> 406
  `unsupported_schema_version` with `supported: ["1"]`. Response header `X-Hydra-Schema: hydra.job_result.v1`.

## 6. Signed package and verification

`GET /v1/jobs/{id}/package` signs `result.json` (the v1 envelope, canonical JSON), `evidence.json` (up to 500
non-embedding genes of the incident, redacted by `hydra.redact`) and `report.md` with the installation's Ed25519 key
(`hydra/evidence_sign.py`). The manifest lists every file with sha256 and size, the run id, the product version and
commit, and the audit chain head.

Verification (`POST /v1/exports/verify?fingerprint=<pinned>` or `hydra verify-export <zip> --fingerprint ...`):
signature over the canonical manifest, every hash and size, no missing or extra files, no unsafe member names.
The fingerprint must be pinned: a package re-signed by another key is internally consistent and passes an unpinned
check; only the pinned fingerprint rejects it (tested). The reference client refuses to verify without a pin. Tested
rejections: edited `report.md` (sha256 mismatch), removed `evidence.json` (missing file), edited manifest (signature
invalid), foreign key (key differs from expected). The signature proves integrity since signing, not truth of the
material.

## 7. Webhook delivery (HMAC)

Configured by an admin (`POST /v1/integrations/webhooks`, event `job`). Each delivery is a JSON body
`{id, event, created_at, hydra_version, data}` with `data = {job_id, kind, status, counts}` (ids and counts only,
no file names). Headers: `X-Hydra-Event`, `X-Hydra-Delivery`, `X-Hydra-Timestamp` (unix seconds),
`X-Hydra-Signature: sha256=<hex HMAC-SHA256(secret, "{timestamp}.{raw body}")>`. Receivers compare in constant
time and reject `|now - timestamp| > 300 s`. A loopback URL is allowed in offline mode; any other URL needs
`HYDRA_OFFLINE=0` and passes `netguard`. Policy (`hydra/integrations/webhooks.py`): 2xx delivered; network error,
429 or 5xx retried with backoff up to 5 attempts, then `dead`; any other status `dead` at once; redirects are not
followed; `POST .../deliveries/{id}/retry` re-sends a dead delivery. Tested with the client's `Receiver`
(127.0.0.1): 503, 503, 200 -> delivered after 3 attempts; 400 -> dead after 1 attempt, then manual retry
delivered; 503 forever -> dead after 5; wrong sender key -> receiver 401 -> dead.

## 8. Graph export round trip

`hydra_client.py roundtrip` (and `test_csv_cypher_roundtrip_counts_equal`) downloads the Neo4j export in four
renderings and re-imports them into a temporary in-memory SQLite database: the LOAD CSV ZIP tables become
`node(nid, label)` and `edge(type, src, dst)` with foreign keys enforced; node and edge counts per label and type
must be equal across the ZIP CSV, the `neo4j-admin` nodes/edges CSV and the rows of every `UNWIND` in the
self-contained `graph.cypher`, and no edge may point to a missing node. A removed edge row is detected (tested).

## 9. Error codes

Error bodies are `{"detail": {"code": "...", "message": "..."}}` (input validation errors of the older endpoints
may carry a plain string `detail`).

| HTTP | code | When |
|---|---|---|
| 400 | `invalid_idempotency_key` | header present but malformed |
| 400 | (string detail) | no files, wrong JSON shape, path not allowed, bad URL |
| 401 | `auth_required` | no or unknown/revoked/expired token (`WWW-Authenticate: Bearer`) |
| 403 | `forbidden` | role too low |
| 403 | `csrf`, `password_change_required` | session clients only |
| 404 | `job_not_found` | unknown job id (contract endpoints) |
| 406 | `unsupported_schema_version` | `schema_version` / `Accept-Version` not supported |
| 409 | `idempotency_conflict` | key reused with a different request (`job_id` of the original) |
| 409 | `job_not_finished` | report/package before the job is `done` (`job_status`) |
| 409 | `not_an_analyse_job` | report/package for an import job |
| 413 | (string detail) | upload above `HYDRA_MAX_UPLOAD_MB` |
| 415 | (string detail) | neither multipart nor JSON |

Client retry rule: retry GET and keyed POST on timeouts, connection errors, 429 and 5xx with exponential backoff;
never retry 4xx other than 429; never retry an unkeyed POST.

## 10. Known limitations

- One installation key; no key rotation endpoint and no timestamp authority (offline by design).
- Idempotency keys are kept for the lifetime of the job row; there is no TTL.
- The per-key lock is in-process: correct for the single API process HYDRA runs, not for several workers.
- The webhook carries a notification, not the package; the receiver fetches the package with its own token.
- Contract tests run the real pipeline on the synthetic corpus (`tests/ui_api/synthetic.py`); results on that corpus
  are not quality metrics.
