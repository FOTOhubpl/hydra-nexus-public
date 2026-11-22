# Panel authentication (AUTH-LOGIN)

Date: 2026-11-22 (Europe/Warsaw). Branch from . Code: `hydra/auth.py`
(service), `hydra/api_auth.py` (gate + `/v1/auth/*`), `hydra/cli_user.py` (`hydra user ...`), DB migration 7
(`hydra/db.py`), UI `ui/src/pages/auth/*` + route guard. Tests: `tests/auth/` (61, HYDRA_AUTH=1), the e2e harness
(`tests/e2e/server.py`), `scripts/jury_smoke.py` step "2b login", Playwright `ui/e2e/auth.spec.ts`.

## 1. Quick reference (PL)

**Logowanie: dane administratora w `$HYDRA_STATE_DIR/ADMIN_CREDENTIALS.txt` (lokalnie `~/HydraNexus/state/`,
Docker `~/HydraNexus/state-docker/ADMIN_CREDENTIALS.txt`) albo `docker compose logs api | grep -A3 HYDRA`.**
Login `admin`; przy pierwszym logowaniu panel wymusza zmianę hasła. Hasło można też ustawić z góry zmienną
`HYDRA_ADMIN_PASSWORD` (wtedy plik nie powstaje). Zgubione hasło: `uv run hydra user reset-password admin`
(Docker: `docker compose --profile cli run --rm cli user reset-password admin`).

## 2. Model

| Element | Decision |
|---|---|
| Switch | `HYDRA_AUTH=1` (default, product). `HYDRA_AUTH=0` opens the API as before (unit suite only: `tests/conftest.py` forces it). |
| First start | No users -> one `admin` with a 20-character random password (`secrets.token_urlsafe`), `must_change_password=1`, written once to `$HYDRA_STATE_DIR/ADMIN_CREDENTIALS.txt` (created 0600 via `O_EXCL` temp file + rename) and printed as a banner (PL + EN) on stdout, every line greppable after `HYDRA`. Runs in one writer transaction (`SELECT COUNT(*)` + `INSERT`), so two processes cannot create two admins. |
| Env override | `HYDRA_ADMIN_PASSWORD` (pydantic `SecretStr`): admin gets that password, no forced change, no file, the banner names the variable but not the value. Ignored once any user exists. |
| Passwords | `hashlib.scrypt` N=2^15, r=8, p=1, 16-byte salt, 32-byte key; format `scrypt$N$r$p$salt$key` (argon2-cffi is not in `uv.lock`). Measured 0.04 s per hash on the M4 laptop. `hmac.compare_digest`. Unknown or disabled users are verified against a dummy hash (same timing). Policy: >= 12 characters, not containing the username, >= 5 distinct characters, different from the current one. |
| Roles | `viewer` read-only (safe methods, own password and tokens, `POST /v1/graph/export`, `POST /v1/exports/verify`); `analyst` every analysis, import, review, hunt, KONTRA, watch, connector run/test write; `admin` additionally users, connector secrets/connect/enable/presets, webhook endpoint create/update/delete, WATCH-RT source and engine control (`/v1/watch/rt/sources/*`, `/v1/watch/rt/engine*`; match acknowledge/dismiss stays analyst). Table: `hydra.api_auth.required_role`, tested in `tests/auth/test_auth.py::test_required_role_table`. |
| Sessions | Cookie `hydra_session` = 256 random bits, HttpOnly, SameSite=Strict, Path=/; only its sha256 is stored (`auth_sessions`). Idle timeout 8 h (`HYDRA_SESSION_IDLE_H`), absolute 24 h (`HYDRA_SESSION_ABS_H`). "Zapamiętaj mnie" raises the idle timeout to the absolute limit and makes the cookie persistent (Max-Age 24 h); it never extends the absolute limit. `last_seen` is written at most once a minute. Logout deletes the row; a password change ends the user's other sessions; disabling a user ends all of them. |
| CSRF | Double submit bound to the session: readable cookie `hydra_csrf` + header `X-CSRF-Token`, compared with the per-session token stored server-side (`hmac.compare_digest`). Required on POST/PUT/PATCH/DELETE with a session; bearer tokens are not ambient credentials and need none. The loopback Host/Origin guard (`hydra.api._LocalOnlyGuard`, review #6) stays outermost and still refuses foreign Host headers and cross-site writes, including cross-site login. |
| Forced change | A session with `must_change_password` reaches only `/v1/auth/{session,logout,password,tokens}`; everything else is 403 `password_change_required`. |
| Throttling | 5 failed logins per username -> locked 5 min (423 + `Retry-After`); the counter resets on success. Unknown usernames are locked the same way in memory, so a 423 does not confirm that an account exists. 20 failed logins per client address per 60 s -> 429. Successful logins are not counted. |
| API tokens | Personal access tokens `hnx_` + 256 random bits, shown once, stored as sha256 (high-entropy secret), optional expiry 1–3650 days, revocable, role of their owner, `Authorization: Bearer`. Created in the UI ("Tokeny API"), by `POST /v1/auth/tokens`, or `hydra user token <user>`. A token is not subject to the forced password change. |
| Enforcement | Pure ASGI middleware `AuthGate`, inside the `/api` prefix strip and the Host/Origin guard. Public: static UI files (the SPA, including `/login`), `GET /health` (body reduced to `{status, version, auth}` without a principal), `POST /v1/auth/login`, `GET /v1/auth/session`, `POST /v1/auth/logout`, `POST /v1/integrations/webhooks/sink` (HMAC-verified test receiver, called by our own dispatcher). Everything else under `/v1/*`, `/docs`, `/redoc`, `/openapi.json` -> 401 `auth_required`. |
| SSE | `GET /v1/stream` needs the session cookie (EventSource sends it on the same origin) or a token; checked when the stream opens. An open stream is not cut at logout (it ends at the next reconnect). |
| Audit | Hash-chained events (`hydra.events`): `USER_CREATED`, `USER_UPDATED`, `LOGIN_OK`, `LOGIN_FAIL` (reason `bad_password`/`unknown_user`/`disabled`/`locked`/`bad_current_password`), `LOGOUT`, `PASSWORD_CHANGED`, `TOKEN_CREATED`, `TOKEN_REVOKED`. Payloads carry user id/name, role, reason and the first 12 hex characters of the session hash, never a password, hash, cookie or token (test `test_audit_events_without_secrets`, e2e `test_auth_gate_end_to_end`). |

Error bodies: `{"detail": {"code", "message", "retry_after_s"?}}`, Polish messages; codes `auth_required`,
`invalid_credentials`, `locked`, `rate_limited`, `csrf`, `forbidden`, `password_change_required`, `weak_password`,
`exists`, `last_admin`, `self_disable`, `not_found`, `auth_disabled`.

## 3. Endpoints (`/v1/auth`, from the browser `/api/v1/auth`)

| Method + path | Role | Purpose |
|---|---|---|
| `GET /session` | public | `{auth_enabled, authenticated, user?, csrf_token?, expires_at?, idle_expires_at?, credentials_hint}` |
| `POST /login` `{username, password, remember}` | public | sets `hydra_session` + `hydra_csrf`; 401 / 423 / 429 |
| `POST /logout` | public | 204, deletes the session, clears cookies |
| `POST /password` `{current_password, new_password}` | any | change own password |
| `GET /tokens[?all=1]`, `POST /tokens` `{name, expires_days?}`, `DELETE /tokens/{id}` | any (all=1: admin) | personal access tokens |
| `GET /users`, `POST /users` `{username, role}`, `PATCH /users/{id}` `{role?, disabled?}`, `POST /users/{id}/reset-password` | admin | generated passwords are returned once |

CLI: `hydra user list | create <name> --role analyst [--password-stdin] | reset-password <name> [--password-stdin] |
disable <name> | enable <name> | token <name> [--name ci] [--days 30]`. The CLI works on the state database
directly (no API call), so it also recovers a lost admin password. No CLI command talks to the API; scripts that do
(`scripts/jury_smoke.py`, `tests/e2e/server.py`) log in and send a token; external automation sets
`HYDRA_API_TOKEN` and sends `Authorization: Bearer $HYDRA_API_TOKEN`.

```bash
TOKEN=$(uv run hydra user token admin --name automation --days 30)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/campaigns | head
```

## 4. Threats considered

| Threat | Mitigation | Residual |
|---|---|---|
| Another local user or a browser tab on the same machine calls the API | 401 without session/token | a local root can read the state DB and the credentials file (0600, owner only) |
| CSRF / DNS rebinding from a web page | SameSite=Strict cookies, per-session CSRF header, Host/Origin guard | none known |
| Online password guessing | 5 failures -> 5 min lock per user, 20 failures/min per address, scrypt cost | distributed guessing over loopback is not a realistic path |
| Username enumeration | same 401 text, dummy-hash timing, unknown names lock too | `LOGIN_FAIL` events record the reason for the admin |
| Session theft | HttpOnly cookie, server-side revocation, idle + absolute expiry | no `Secure` flag: the API is HTTP on loopback / SSH tunnel by design (spec §14); behind a TLS proxy add `Secure` |
| Credential leaks in logs/audit | no secrets in events, `SecretStr` for the env password, 422 redaction kept | the first-start banner prints the generated password once on stdout (required for `docker compose logs`); it must be changed at first login |
| Token leakage | shown once, sha256 at rest, expiry, revocation, owner role | a leaked token works until revoked/expired |
| Lockout of the only admin | last active admin cannot be demoted or disabled; `hydra user reset-password admin` from the shell | — |

## 5. Docker

The `api`/`gui` services keep their state volume `~/HydraNexus/${HYDRA_STATE_SUBDIR:-state-docker}` ->
`/hydra/state`; the file lands at `~/HydraNexus/state-docker/ADMIN_CREDENTIALS.txt` on the host. To read the
banner: `docker compose logs api | grep -A3 HYDRA` (service `gui`: `docker compose logs gui | grep -A3 HYDRA`).
To fix the password before the first start: `HYDRA_ADMIN_PASSWORD=... docker compose up -d api` (both services pass
`HYDRA_AUTH` and `HYDRA_ADMIN_PASSWORD` through; an empty variable counts as unset, so the default flow is the
generated file).

## 6. Tests and gates

- `tests/auth/test_auth.py` (61 tests, HYDRA_AUTH=1, fake clock for idle/absolute expiry, lockout and token expiry).
- `tests/e2e/server.py` starts the e2e API with HYDRA_AUTH=1 and `HYDRA_ADMIN_PASSWORD`, proves 401 / wrong password /
  CSRF once, issues a token used by every harness client; `test_auth_gate_end_to_end` covers SSE 401, forced change,
  viewer 403, logout and audit payloads on the real server.
- `scripts/jury_smoke.py` step "2b login (HYDRA_AUTH=1)".
- Playwright: `ui/e2e/auth.spec.ts` (login, forced change, logout; desktop and 390 px); other specs reuse a logged-in
  storage state.

Measured on the laptop at 2026-11-22 03:30 (branch head before this doc commit, 78226a9 + a3bbe3b):
`just check` exit 0 (ruff clean, pytest 1733 passed / 53 skipped, vitest 320 passed in 55 files); `just e2e` 42
passed; `E2E_OUT_DIR=<private> just e2e-ui` 136 passed / 6 skipped; `just jury-smoke` PASS (10 steps, including
"2b login"). Screenshots (login, forced change, user menu, users; light/dark x 1440/390):
`~/HydraNexus/qa/auth/` (outside git).
