"""AUTH-LOGIN: hashing, bootstrap, lockout, sessions, CSRF, roles, tokens, audit, SSE (docs/SECURITY_AUTH.md)."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from hydra import auth, events
from hydra.api import create_app
from hydra.api_auth import required_role
from hydra.db import connect, schema_version
from tests.auth.conftest import admin_password, auth_settings


def _db(h) -> sqlite3.Connection:
    return connect(h.settings.db_path, readonly=True)


# ------------------------------------------------------------------------------------------------ hashing
def test_scrypt_hash_format_salt_and_verify():
    a, b = auth.hash_password("correct horse battery"), auth.hash_password("correct horse battery")
    assert a != b  # per-user random salt
    algo, n, r, p, salt, key = a.split("$")
    assert (algo, int(n), int(r), int(p)) == ("scrypt", 2**15, 8, 1)
    assert len(salt) >= 20 and len(key) >= 40
    assert auth.verify_password("correct horse battery", a)
    assert not auth.verify_password("correct horse batterz", a)
    assert not auth.verify_password("x", "garbage")
    assert not auth.verify_password("x", "bcrypt$1$2$3$aa$bb")


def test_password_policy():
    assert auth.password_problem("short", "admin")
    assert auth.password_problem("admin-is-my-password", "admin")
    assert auth.password_problem("aaaaaaaaaaaaaaa", "jan")
    assert auth.password_problem("Same-Passw0rd-123", "jan", current="Same-Passw0rd-123")
    assert auth.password_problem("Dobre-Haslo-2026!", "jan") is None
    assert len(auth.generate_password()) == 20


# ------------------------------------------------------------------------------------------------ schema
def test_migration_adds_tables_and_is_idempotent(h):
    with contextlib.closing(_db(h)) as conn:
        assert schema_version(conn) >= 7  # another branch may add more migrations
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "auth_sessions", "api_tokens"} <= names
    h.svc.writer.run(auth.ensure_schema)  # IF NOT EXISTS: running the DDL again is harmless


# ------------------------------------------------------------------------------------------------ bootstrap
def test_bootstrap_creates_exactly_one_admin_and_0600_file(h, capsys):
    users = h.svc.list_users()
    assert [(u["username"], u["role"], u["must_change_password"]) for u in users] == [("admin", "admin", True)]
    path = h.settings.admin_credentials_path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    pw = admin_password(h.settings)
    assert len(pw) == 20
    before = path.read_text()
    assert h.svc.bootstrap_admin() is None  # users exist: nothing happens, the file is not rewritten
    assert path.read_text() == before and len(h.svc.list_users()) == 1


def test_bootstrap_banner_names_the_file(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HYDRA_ADMIN_PASSWORD", raising=False)
    s = auth_settings(tmp_path)
    with TestClient(create_app(s)):
        pass
    out = capsys.readouterr().out
    assert "HYDRA NEXUS: utworzono pierwsze konto administratora / first administrator account created" in out
    assert str(s.admin_credentials_path) in out and admin_password(s) in out
    with TestClient(create_app(s)):  # second start: no second banner, no second admin
        pass
    assert "first administrator" not in capsys.readouterr().out


def test_env_override_no_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HYDRA_ADMIN_PASSWORD", "Env-Admin-Passw0rd-1")
    s = auth_settings(tmp_path)
    with TestClient(create_app(s)) as c:
        assert not s.admin_credentials_path.exists()
        r = c.post("/v1/auth/login", json={"username": "admin", "password": "Env-Admin-Passw0rd-1"})
        assert r.status_code == 200 and r.json()["user"]["must_change_password"] is False
        assert c.get("/v1/jobs").status_code == 200
    out = capsys.readouterr().out
    assert "HYDRA_ADMIN_PASSWORD" in out and "Env-Admin-Passw0rd-1" not in out
    assert "SecretStr" in repr(s.admin_password) or "**" in repr(s.admin_password)


def test_empty_env_password_counts_as_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_ADMIN_PASSWORD", "")  # docker compose passes "${HYDRA_ADMIN_PASSWORD:-}"
    s = auth_settings(tmp_path)
    with TestClient(create_app(s)) as c:
        assert s.admin_credentials_path.exists()
        assert c.post("/v1/auth/login", json={"username": "admin", "password": ""}).status_code == 401


# ------------------------------------------------------------------------------------------------ gate basics
def test_public_and_protected_paths(h, tmp_path):
    anon = h.client()
    assert anon.get("/health").json() == {"status": "ok", "version": anon.get("/health").json()["version"],
                                          "auth": True}
    for path in ("/v1/jobs", "/api/v1/jobs", "/v1/campaigns", "/openapi.json", "/docs", "/v1/audit/verify",
                 "/v1/stream", "/v1/auth/tokens", "/v1/auth/users"):
        r = anon.get(path)
        assert r.status_code == 401, path
        assert r.json()["detail"]["code"] == "auth_required"
    s = anon.get("/v1/auth/session").json()
    assert s["auth_enabled"] is True and s["authenticated"] is False
    assert s["credentials_hint"]["file"].endswith("ADMIN_CREDENTIALS.txt")
    assert "docker compose logs api" in s["credentials_hint"]["docker"]
    assert anon.post("/v1/import/paths", json={"paths": []}).status_code == 401
    # health is full once logged in
    h.admin()
    assert "artifacts" in h.c.get("/health").json()


def test_static_ui_is_public_but_api_paths_are_not(tmp_path, monkeypatch):
    monkeypatch.delenv("HYDRA_ADMIN_PASSWORD", raising=False)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>HYDRA</html>")
    (dist / "assets" / "app.js").write_text("1")
    s = auth_settings(tmp_path, ui_dist=dist)
    with TestClient(create_app(s)) as c:
        assert c.get("/").status_code == 200 and c.get("/login").status_code == 200
        assert c.get("/assets/app.js").status_code == 200
        assert c.get("/api/v1/jobs").status_code == 401
        assert c.get("/api/openapi.json").status_code == 401


def test_login_logout_and_must_change(h):
    pw = admin_password(h.settings)
    assert h.login(h.c, "admin", "nope").json()["detail"] == {"code": "invalid_credentials",
                                                             "message": "Nieprawidłowy login lub hasło."}
    assert h.login(h.c, "nobody", "nope").json()["detail"]["code"] == "invalid_credentials"
    r = h.login(h.c, "admin", pw)
    assert r.status_code == 200
    cookies = r.headers.get_list("set-cookie")
    sess = next(c for c in cookies if c.startswith("hydra_session="))
    assert "HttpOnly" in sess and "samesite=strict" in sess.lower() and "Max-Age" not in sess
    assert "HttpOnly" not in next(c for c in cookies if c.startswith("hydra_csrf="))
    assert h.c.get("/v1/jobs").json()["detail"]["code"] == "password_change_required"
    assert h.c.get("/v1/auth/session").json()["user"]["must_change_password"] is True
    r = h.c.post("/v1/auth/password", json={"current_password": pw, "new_password": "short"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "weak_password"
    r = h.c.post("/v1/auth/password", json={"current_password": "wrong", "new_password": "Long-Enough-Pass1!"})
    assert r.status_code == 401
    assert h.c.post("/v1/auth/password", json={"current_password": pw,
                                               "new_password": "Long-Enough-Pass1!"}).status_code == 200
    assert h.c.get("/v1/jobs").status_code == 200
    assert h.c.post("/v1/auth/logout").status_code == 204
    assert h.c.get("/v1/jobs").status_code == 401
    assert h.login(h.c, "admin", pw).status_code == 401  # the old password is gone
    assert h.login(h.c, "Admin", "Long-Enough-Pass1!").status_code == 200  # usernames are case-insensitive


def test_remember_me_sets_persistent_cookie(h):
    h.admin()
    r = h.login(h.client(), "admin", "Root-Test-Passw0rd!", remember=True)
    sess = next(c for c in r.headers.get_list("set-cookie") if c.startswith("hydra_session="))
    assert "Max-Age=86400" in sess


# ------------------------------------------------------------------------------------------------ lockout / rate
def test_lockout_after_five_failures_for_five_minutes(h):
    pw = admin_password(h.settings)
    for _ in range(4):
        assert h.login(h.client(), "admin", "bad").status_code == 401
    r = h.login(h.client(), "admin", "bad")
    assert r.status_code == 423 and r.json()["detail"]["code"] == "locked"
    assert 290 <= r.json()["detail"]["retry_after_s"] <= 301 and r.headers["Retry-After"]
    r = h.login(h.client(), "admin", pw)  # even the right password is refused while locked
    assert r.status_code == 423
    h.clock.advance(minutes=5, seconds=1)
    assert h.login(h.client(), "admin", pw).status_code == 200
    with contextlib.closing(_db(h)) as conn:
        row = conn.execute("SELECT failed_attempts, locked_until FROM users WHERE username='admin'").fetchone()
    assert tuple(row) == (0, None)


def test_unknown_user_locks_the_same_way(h):
    for _ in range(4):
        assert h.login(h.client(), "ghost", "bad").status_code == 401
    assert h.login(h.client(), "ghost", "bad").status_code == 423


def test_rate_limit_per_client_address(h):
    for i in range(auth.RATE_MAX):
        assert h.login(h.client(), f"spray{i}", "bad").status_code == 401
    r = h.login(h.client(), "admin", admin_password(h.settings))
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"
    h.clock.advance(seconds=auth.RATE_WINDOW_S + 1)
    assert h.login(h.client(), "admin", admin_password(h.settings)).status_code == 200


def test_successful_logins_are_not_rate_limited(h):
    h.admin()
    for _ in range(auth.RATE_MAX + 5):
        assert h.login(h.client(), "admin", "Root-Test-Passw0rd!").status_code == 200


# ------------------------------------------------------------------------------------------------ sessions
def test_idle_timeout_8h(h):
    h.admin()
    h.clock.advance(hours=7, minutes=59)
    assert h.c.get("/v1/jobs").status_code == 200  # activity restarts the idle clock
    h.clock.advance(hours=7, minutes=59)
    assert h.c.get("/v1/jobs").status_code == 200
    h.clock.advance(hours=8, seconds=1)
    assert h.c.get("/v1/jobs").status_code == 401


def test_absolute_timeout_24h_even_when_active(h):
    h.admin()
    for _ in range(23):
        h.clock.advance(hours=1)
        assert h.c.get("/v1/jobs").status_code == 200
    h.clock.advance(hours=1, seconds=1)
    assert h.c.get("/v1/jobs").status_code == 401


def test_remember_me_extends_idle_only(h):
    h.admin()
    cl = h.client()
    assert h.login(cl, "admin", "Root-Test-Passw0rd!", remember=True).status_code == 200
    h.clock.advance(hours=12)
    assert cl.get("/v1/jobs").status_code == 200  # idle 12 h: fine with "remember me"
    h.clock.advance(hours=12, seconds=1)
    assert cl.get("/v1/jobs").status_code == 401  # absolute 24 h still holds


def test_forged_or_malformed_cookie(h):
    anon = h.client()
    anon.cookies.set("hydra_session", "forged")
    assert anon.get("/v1/jobs").status_code == 401
    r = anon.get("/v1/jobs", headers={"Cookie": 'hydra_session="\\x00;;;=='})
    assert r.status_code == 401


def test_session_id_is_stored_hashed(h):
    h.admin()
    cookie = h.c.cookies["hydra_session"]
    with contextlib.closing(_db(h)) as conn:
        ids = [r[0] for r in conn.execute("SELECT id_hash FROM auth_sessions")]
    assert cookie not in ids and auth._sha(cookie) in ids


# ------------------------------------------------------------------------------------------------ CSRF
def test_csrf_double_submit(h):
    h.admin()
    csrf = h.c.headers.pop("X-CSRF-Token")
    assert h.c.cookies["hydra_csrf"] == csrf
    r = h.c.post("/v1/auth/tokens", json={"name": "t"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf"
    assert h.c.post("/v1/auth/tokens", json={"name": "t"}, headers={"X-CSRF-Token": "x" * 32}).status_code == 403
    assert h.c.post("/v1/auth/tokens", json={"name": "t"}, headers={"X-CSRF-Token": csrf}).status_code == 201
    assert h.c.get("/v1/jobs").status_code == 200  # safe methods need no token


def test_loopback_origin_guard_still_applies(h):
    h.admin()
    r = h.c.post("/v1/auth/tokens", json={"name": "t"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "Origin" in r.json()["detail"]
    assert h.client().get("/v1/auth/session", headers={"Host": "evil.example"}).status_code == 403
    r = h.client().post("/v1/auth/login", json={"username": "admin", "password": "x"},
                        headers={"Origin": "https://evil.example"})
    assert r.status_code == 403  # no login CSRF from another site


# ------------------------------------------------------------------------------------------------ roles
@pytest.mark.parametrize(("method", "path", "role"), [
    ("GET", "/v1/campaigns", "viewer"),
    ("GET", "/v1/stream", "viewer"),
    ("POST", "/v1/graph/export", "viewer"),
    ("POST", "/v1/auth/password", "viewer"),
    ("POST", "/v1/auth/tokens", "viewer"),
    ("DELETE", "/v1/auth/tokens/3", "viewer"),
    ("POST", "/v1/reviews", "analyst"),
    ("POST", "/v1/analyse", "analyst"),
    ("POST", "/v1/import", "analyst"),
    ("POST", "/v1/assign-batch", "analyst"),
    ("POST", "/v1/integrations/urlhaus/run", "analyst"),
    ("POST", "/v1/integrations/urlhaus/test", "analyst"),
    ("POST", "/v1/integrations/webhooks/abc/test", "analyst"),
    ("POST", "/v1/integrations/webhooks/deliveries/abc/retry", "analyst"),
    ("PUT", "/v1/integrations/urlhaus/secret", "admin"),
    ("DELETE", "/v1/integrations/urlhaus/secret", "admin"),
    ("POST", "/v1/integrations/urlhaus/connect", "admin"),
    ("POST", "/v1/integrations/urlhaus/enable", "admin"),
    ("POST", "/v1/integrations/presets/p1/apply", "admin"),
    ("POST", "/v1/integrations/webhooks", "admin"),
    ("PATCH", "/v1/integrations/webhooks/abc", "admin"),
    ("DELETE", "/v1/integrations/webhooks/abc", "admin"),
    ("POST", "/v1/watch/rt/sources/meta", "admin"),
    ("PATCH", "/v1/watch/rt/sources/meta/enable", "admin"),
    ("POST", "/v1/watch/rt/engine", "admin"),
    ("POST", "/v1/watch/rt/engine/start", "admin"),
    ("POST", "/v1/watch/rt/matches/m1/ack", "analyst"),
    ("POST", "/v1/watch/rt/matches/m1/dismiss", "analyst"),
    ("GET", "/v1/watch/rt/sources", "viewer"),
    ("GET", "/v1/auth/users", "admin"),
    ("POST", "/v1/auth/users", "admin"),
    ("PATCH", "/v1/auth/users/2", "admin"),
])
def test_required_role_table(method, path, role):
    assert required_role(method, path) == role


def test_role_enforcement_per_endpoint_class(h):
    h.admin()
    viewer, analyst = h.user("viewer"), h.user("analyst")

    def code(cl, method, path, **kw):
        r = cl.request(method, path, **kw)
        return r.status_code, (r.json().get("detail") or {}).get("code") if r.headers.get(
            "content-type", "").startswith("application/json") and isinstance(r.json(), dict) and isinstance(
            r.json().get("detail"), dict) else None

    # read: everyone
    for cl in (viewer, analyst, h.c):
        assert cl.get("/v1/jobs").status_code == 200
    # analysis/review writes: analyst and admin, never viewer
    assert code(viewer, "POST", "/v1/import/paths", json={"paths": []}) == (403, "forbidden")
    assert code(viewer, "POST", "/v1/reviews", json={}) == (403, "forbidden")
    assert code(analyst, "POST", "/v1/import/paths", json={"paths": []})[1] != "forbidden"
    assert code(analyst, "POST", "/v1/reviews", json={})[1] != "forbidden"
    # read-only POST allowed to a viewer
    assert code(viewer, "POST", "/v1/graph/export", json={"ids": []})[1] != "forbidden"
    # admin class: integration secrets, users
    assert code(analyst, "PUT", "/v1/integrations/urlhaus/secret", json={"value": "x"}) == (403, "forbidden")
    assert code(h.c, "PUT", "/v1/integrations/urlhaus/secret", json={"value": "x"})[1] != "forbidden"
    assert code(analyst, "GET", "/v1/auth/users") == (403, "forbidden")
    assert code(analyst, "POST", "/v1/auth/users", json={"username": "zz1", "role": "admin"}) == (403, "forbidden")
    assert h.c.get("/v1/auth/users").status_code == 200
    # self-service for everyone
    assert viewer.post("/v1/auth/tokens", json={"name": "mine"}).status_code == 201


def test_admin_user_management(h):
    h.admin()
    r = h.c.post("/v1/auth/users", json={"username": "jan.k", "role": "analyst"})
    assert r.status_code == 201 and len(r.json()["password"]) == 20
    uid = r.json()["user"]["id"]
    assert h.c.post("/v1/auth/users", json={"username": "JAN.K", "role": "viewer"}).json()["detail"]["code"] == "exists"
    assert h.c.post("/v1/auth/users", json={"username": "a b", "role": "viewer"}).status_code == 400
    assert h.c.patch(f"/v1/auth/users/{uid}", json={"role": "viewer"}).json()["user"]["role"] == "viewer"
    jan = h.client()
    pw = h.c.post(f"/v1/auth/users/{uid}/reset-password").json()["password"]
    assert h.login(jan, "jan.k", pw).json()["user"]["must_change_password"] is True
    assert h.c.patch(f"/v1/auth/users/{uid}", json={"disabled": True}).json()["user"]["disabled"] is True
    assert jan.get("/v1/auth/session").json()["authenticated"] is False  # sessions of a disabled user end
    assert h.login(h.client(), "jan.k", pw).status_code == 401
    admin_id = h.c.get("/v1/auth/session").json()["user"]["id"]
    assert h.c.patch(f"/v1/auth/users/{admin_id}", json={"disabled": True}).json()["detail"]["code"] == "self_disable"
    assert h.c.patch(f"/v1/auth/users/{admin_id}", json={"role": "viewer"}).json()["detail"]["code"] == "last_admin"


# ------------------------------------------------------------------------------------------------ tokens
def test_personal_access_token_lifecycle(h):
    h.admin()
    r = h.c.post("/v1/auth/tokens", json={"name": "ci", "expires_days": 1})
    assert r.status_code == 201
    token, item = r.json()["token"], r.json()["item"]
    assert token.startswith("hnx_") and item["prefix"] == token[:10] and "token" not in item
    bearer = {"Authorization": f"Bearer {token}"}
    anon = h.client()
    assert anon.get("/v1/jobs", headers=bearer).status_code == 200
    assert anon.post("/v1/import/paths", json={"paths": []}, headers=bearer).status_code != 403  # no CSRF for bearer
    assert anon.get("/v1/jobs", headers={"Authorization": "Bearer hnx_wrong"}).status_code == 401
    listed = h.c.get("/v1/auth/tokens").json()["items"]
    assert [t["name"] for t in listed] == ["ci"] and listed[0]["last_used_at"]
    with contextlib.closing(_db(h)) as conn:
        dump = "\n".join(str(tuple(r)) for r in conn.execute("SELECT * FROM api_tokens"))
    assert token not in dump and auth._sha(token) in dump
    r2 = h.c.post("/v1/auth/tokens", json={"name": "ci2"}).json()
    h.clock.advance(days=1, seconds=1)
    assert anon.get("/v1/jobs", headers=bearer).status_code == 401  # expired
    h.admin_login()
    bearer2 = {"Authorization": f"Bearer {r2['token']}"}
    assert anon.get("/v1/jobs", headers=bearer2).status_code == 200
    assert h.c.delete(f"/v1/auth/tokens/{r2['item']['id']}").status_code == 204
    assert anon.get("/v1/jobs", headers=bearer2).status_code == 401  # revoked
    assert h.c.get("/v1/auth/tokens").json()["items"][0]["revoked_at"]


def test_token_role_follows_its_owner_and_owner_scope(h):
    h.admin()
    viewer = h.user("viewer")
    tok = viewer.post("/v1/auth/tokens", json={"name": "v"}).json()
    bearer = {"Authorization": f"Bearer {tok['token']}"}
    assert h.client().post("/v1/reviews", json={}, headers=bearer).status_code == 403
    analyst = h.user("analyst")
    assert analyst.delete(f"/v1/auth/tokens/{tok['item']['id']}").status_code == 404  # not theirs
    assert len(h.c.get("/v1/auth/tokens", params={"all": 1}).json()["items"]) == 1  # admin sees all
    assert analyst.get("/v1/auth/tokens", params={"all": 1}).json()["items"] == []


# ------------------------------------------------------------------------------------------------ audit
def test_audit_events_without_secrets(h):
    pw = admin_password(h.settings)
    h.login(h.client(), "admin", "Wrong-Password-1")
    h.admin("Root-Test-Passw0rd!")
    r = h.c.post("/v1/auth/users", json={"username": "ewa", "role": "viewer"}).json()
    tok = h.c.post("/v1/auth/tokens", json={"name": "ci"}).json()["token"]
    h.c.post("/v1/auth/logout")
    with contextlib.closing(_db(h)) as conn:
        rows = conn.execute("SELECT type, payload FROM events ORDER BY seq").fetchall()
        assert events.verify_chain(conn)[0] is True
    types = [t for t, _ in rows]
    for t in ("USER_CREATED", "LOGIN_FAIL", "LOGIN_OK", "PASSWORD_CHANGED", "TOKEN_CREATED", "LOGOUT"):
        assert t in types, t
    blob = "\n".join(p for _, p in rows)
    cookie_vals = [c for c in h.c.cookies.values()]
    for secret in (pw, "Wrong-Password-1", "Root-Test-Passw0rd!", r["password"], tok, *cookie_vals):
        assert secret not in blob
    assert "password_hash" not in blob and "scrypt$" not in blob
    fail = json.loads(next(p for t, p in rows if t == "LOGIN_FAIL"))
    assert fail["reason"] == "bad_password" and fail["username"] == "admin"


# ------------------------------------------------------------------------------------------------ SSE
def test_sse_requires_the_session_cookie(tmp_path, monkeypatch):
    from tests.ui_api.test_stream import Server

    monkeypatch.setenv("HYDRA_ADMIN_PASSWORD", "Stream-Admin-Passw0rd")
    srv = Server(auth_settings(tmp_path))
    try:
        with httpx.Client(base_url=srv.base, timeout=10) as c:
            assert c.get("/v1/stream").status_code == 401
            r = c.post("/v1/auth/login", json={"username": "admin", "password": "Stream-Admin-Passw0rd"})
            assert r.status_code == 200
            with c.stream("GET", "/v1/stream") as s:
                assert s.status_code == 200 and s.headers["content-type"].startswith("text/event-stream")
                first = next(s.iter_lines())
                assert first.startswith(("event:", "id:", ":", "retry:"))
    finally:
        srv.stop()


# ------------------------------------------------------------------------------------------------ switch off
def test_auth_off_keeps_the_api_open(settings):
    assert settings.auth is False  # tests/conftest.py: HYDRA_AUTH=0 for the existing suite
    with TestClient(create_app(settings)) as c:
        assert c.get("/v1/jobs").status_code == 200
        s = c.get("/v1/auth/session").json()
        assert s["auth_enabled"] is False and s["authenticated"] is False
        assert c.post("/v1/auth/login", json={"username": "a", "password": "b"}).status_code == 409
        assert not settings.admin_credentials_path.exists()
