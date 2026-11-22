# Threat model (hackathon demonstrator)

Scope: HYDRA NEXUS as shipped for evaluation: one container or one local process, one operator, loopback-only API,
organiser material imported from disk. State: integration `c4f7ba8`, 2026-11-21. Not a penetration test: no
dependency vulnerability scan was run, so no claim of "no vulnerabilities" is made (the external audit, §8, says
the same).

## 1. Assets

| Asset | Why it matters |
|---|---|
| Organiser material (screenshots, recordings, saved pages, HAR, metadata) | NASK-only, no public distribution (task §8); contains personal data and leaked secrets of third parties |
| Originals in the content store (sha256-named, read-only) | evidence: must stay byte-identical |
| Analysis state (SQLite: genes, pairs, campaigns, reviews, audit log) | the basis of every explanation and decision |
| Enrichment and LLM caches | replayable evidence of what public sources and the optional LLM said, and when |
| Credentials: Meta Ad Library token, AWS credentials (optional), Neo4j password | never in the repository |

## 2. Trust boundaries and assumptions

1. **Local API, one trusted operator.** The API binds to loopback only (`Settings.api_host` refuses other
   addresses); on the server it is reached through an SSH tunnel. Anyone who can reach the loopback port is
   treated as the operator. Since 2026-11-22 (AUTH-LOGIN, `docs/SECURITY_AUTH.md`) the panel and API require a
   login (`HYDRA_AUTH=1`): server-side sessions, CSRF token, personal access tokens and three roles (viewer, analyst,
   admin). There is still no case isolation and no SSO (production path: `docs/ARCHITECTURE_PRODUCTION.md`).
2. **Browser-based attacks on the local API** are in scope: `_LocalOnlyGuard` in `hydra/api.py` rejects foreign
   `Host` headers (DNS rebinding) and cross-site or cross-origin writes (`Origin`, `Sec-Fetch-Site`).
3. **All imported material is hostile.** Saved pages, MHTML, HAR files, images, videos and archives come from scam
   operators.
4. **Public metadata services are untrusted for integrity** (answers can be wrong or change), trusted only as
   "what the service said at `fetched_at`".

## 3. Threats and mitigations

| Threat | Mitigation in the code | Residual risk |
|---|---|---|
| Malicious archive: path traversal, symlinks, zip bomb | `hydra/ingest.py`: traversal and symlink checks, entry-count limit, compression-ratio limit, per-file size limits, MIME sniffed from bytes | no global limit on total unpacked bytes yet (audit §8); disk can fill on a crafted archive within the ratio limit |
| Saved HTML/SVG executing in the application origin | `GET /artifacts/{id}/content` serves HTML, SVG, XML and text as `text/plain` with `Content-Security-Policy: sandbox` and `X-Content-Type-Options: nosniff`; pages are parsed (lxml/BeautifulSoup), never rendered by the backend | any new UI view that embeds stored HTML must keep this rule |
| HAR or page content steering the parser (huge files, malformed JSON) | size caps per input (`max_file_mb`, video exception), parser errors become gene status `failed`, never a crash | a handled extractor failure is cached and not retried (audit A06, open) |
| Live URL fetch hitting internal services (SSRF) or running scam code | `hydra/enrich/fetch.py`: opt-in only, no JavaScript, GET only, at most 10 redirects, 15 s timeout, size caps, only public (global) IP addresses contacted, no cookies kept | the fetcher still downloads hostile HTML; it is stored as evidence and parsed, never rendered |
| Leaking organiser data through enrichment | only hostnames and IP addresses extracted from the material are sent to RDAP, DNS, Team Cymru and crt.sh (ADR-007); scam pages are not fetched in the default run | the hostnames themselves reach those services (that is the purpose) |
| Leaking organiser data through the optional LLM layer | ADR-008: off by default; text passes `hydra.redact` before sending; images are downscaled copies (max side 1024 px); calls cached and replayable | **redaction is text-only: downscaling an image is not anonymisation**; faces, names and numbers visible in a creative are sent as they are when the layer is enabled. The operator authorised cloud processing of organiser data (internal notes, not published) |
| Secrets in the material (access tokens, API keys, user names in file paths) shown or exported | `hydra/redact.py` redacts them in everything persisted for display or export; matching uses `secret_fingerprint` (salted, truncated sha256, per-installation salt) | originals in the content store stay unredacted by design (evidence) |
| Tampering with the analysis history | `hydra/events.py`: append-only events with a hash chain, written in the same transaction as the change; `hydra verify` re-hashes originals and checks the chain | the chain detects inconsistent edits inside the log; it does not prove that the source material is genuine, and an administrator who rewrites the whole database can rebuild a consistent chain. No external anchoring or signature (signed evidence ZIP from the spec is not implemented) |
| Malicious model files | `scripts/fetch_models.py`: pinned revisions, safetensors/ONNX only, no pickle weights, no `trust_remote_code`, sha256 per file (`models.lock.json`) | fusion models are pickled; loading is checked against a neighbouring JSON hash, which detects accidental change but not a deliberate swap of both files (audit §7). Load only from the own store |
| Credentials in the repository | `.env` and `.env.*` gitignored; Neo4j password generated into `.env` (mode 600); Meta token and AWS credentials only from the environment; the guard hook blocks staging secrets | no secret scanning on the GitHub organisation |
| Graph store exposure | optional Neo4j profile listens on 127.0.0.1 only (7474/7687), password from `.env` | Neo4j Browser is reachable by anyone with loopback access, like the API |

## 4. Network policy (audit A09)

| Setting | Effect |
|---|---|
| `HYDRA_OFFLINE=1` (default) | hard kill switch for live enrichment: `live_enabled` requires `enrich and not offline` (fixed in `c4f7ba8`); cached answers are still replayed |
| `HYDRA_ENRICH=1` and `HYDRA_OFFLINE=0` | live lookups to the public sources in `docs/DATA_SOURCES.md`, polite rate limits, every answer cached with `fetched_at` |
| `HYDRA_LLM` unset / `replay` / `1` | optional LLM layer off / cache only / live with cache first (ADR-008). Making `HYDRA_OFFLINE=1` also force the LLM to replay is assigned to the LLM stream and is **not yet confirmed** in this document |
| Model downloads | `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`; models fetched once with `just models-fetch` |
| Tests | a network guard fails any non-loopback connection or DNS lookup in the test suite (`tests/conftest.py`) |

A demo without network must also work with an empty cache: lookups then become `unavailable`, never errors, and
never count as evidence against a link (robustness case `enrich_unreachable`, internal notes, not published).

## 5. Out of scope for the demonstrator

Multi-user access control, case isolation, encryption at rest, external timestamping of the audit log, and a
dependency vulnerability scan. They are listed as production steps in `docs/ARCHITECTURE_PRODUCTION.md`.
