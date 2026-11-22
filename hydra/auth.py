"""Panel authentication (AUTH-LOGIN, docs/SECURITY_AUTH.md): users, password hashes, sessions, API tokens.

- Passwords: `hashlib.scrypt` (N=2**15, r=8, p=1, 16-byte per-user salt, 32-byte key; argon2-cffi is not in uv.lock),
  stored as `scrypt$N$r$p$<salt b64>$<key b64>`, compared with `hmac.compare_digest`.
- Sessions: a random 256-bit cookie value; only its sha256 is stored (`auth_sessions`), with a per-session CSRF token
  (double-submit: cookie `hydra_csrf` + header `X-CSRF-Token`), idle timeout (default 8 h; "remember me" raises it to
  the absolute limit) and absolute lifetime (default 24 h).
- Personal access tokens: `hnx_` + 256 random bits, stored as sha256 (high-entropy secret: no slow hash needed),
  shown once, revocable, optional expiry; accepted as `Authorization: Bearer`.
- Login throttling: per client address (20 failed attempts / 60 s -> 429) and per username (5 failures -> locked 5 min,
  423). Unknown usernames are locked the same way in memory, so a lock never confirms that an account exists.
- Every write goes through the single `Writer` (ADR-002) together with its audit event (hydra.events): LOGIN_OK,
  LOGIN_FAIL, LOGOUT, PASSWORD_CHANGED, USER_CREATED, USER_UPDATED, TOKEN_CREATED, TOKEN_REVOKED. No password, hash,
  session id or token is ever written to an event.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hydra import events

log = logging.getLogger("hydra.auth")

ROLES = ("admin", "analyst", "viewer")
ROLE_RANK = {"viewer": 0, "analyst": 1, "admin": 2}
ROLE_PL = {"admin": "Administrator", "analyst": "Analityk", "viewer": "Obserwator"}

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2**15, 8, 1, 32
SCRYPT_MAXMEM = 64 * 1024 * 1024

MAX_FAILURES = 5
LOCK_S = 5 * 60
RATE_WINDOW_S = 60.0
RATE_MAX = 20  # failed logins per client address per window
MIN_PASSWORD_LEN = 12
TOUCH_EVERY_S = 60.0  # last_seen / last_used_at are written at most once a minute per session/token
CACHE_TTL_S = 5.0

SESSION_COOKIE = "hydra_session"
CSRF_COOKIE = "hydra_csrf"
CSRF_HEADER = "x-csrf-token"
TOKEN_PREFIX = "hnx_"
ADMIN_USERNAME = "admin"
DOCKER_HINT = "docker compose logs api | grep -A3 HYDRA"

MSG_INVALID = "Nieprawidłowy login lub hasło."


class AuthError(Exception):
    """An expected refusal: HTTP status + machine code + Polish message (+ retry_after_s)."""

    def __init__(self, status: int, code: str, message: str, retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after_s = status, code, message, retry_after_s

    def detail(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after_s is not None:
            d["retry_after_s"] = self.retry_after_s
        return d


# ------------------------------------------------------------------------------------------------ passwords
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _nfc(password: str) -> str:
    """Unicode NFC: the same Polish password typed or pasted as decomposed characters (macOS) hashes the same."""
    return unicodedata.normalize("NFC", password)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(_nfc(password).encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                         maxmem=SCRYPT_MAXMEM, dklen=SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, key = stored.split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(key)

        def derive(text: str) -> bytes:
            return hashlib.scrypt(text.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                  maxmem=SCRYPT_MAXMEM, dklen=len(want))

        if hmac.compare_digest(derive(_nfc(password)), want):
            return True
        # Hashes written before NFC normalisation (2026-11-22 03:40) used the raw string.
        return _nfc(password) != password and hmac.compare_digest(derive(password), want)
    except (ValueError, TypeError):
        return False


# Verified against when the user is unknown or disabled, so the response time does not reveal which one it was.
_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


def generate_password() -> str:
    """20 URL-safe characters (~120 bits)."""
    return secrets.token_urlsafe(15)[:20]


def password_problem(password: str, username: str, current: str | None = None) -> str | None:
    """Polish reason why `password` is refused, or None."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"Hasło musi mieć co najmniej {MIN_PASSWORD_LEN} znaków."
    if len(password) > 256:
        return "Hasło może mieć najwyżej 256 znaków."
    if username and username.lower() in password.lower():
        return "Hasło nie może zawierać nazwy użytkownika."
    if current is not None and hmac.compare_digest(password.encode(), current.encode()):
        return "Nowe hasło musi różnić się od obecnego."
    if len(set(password)) < 5:
        return "Hasło jest zbyt przewidywalne (za mało różnych znaków)."
    return None


_USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{3,64}")


def check_username(username: str) -> str:
    if not _USERNAME_RE.fullmatch(username or ""):
        raise AuthError(400, "invalid_username", "Nazwa użytkownika: 3–64 znaki (litery, cyfry, . _ -).")
    return username


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------ schema
def ensure_schema(conn: sqlite3.Connection) -> None:
    """Run the auth DDL (same statements as migration 7, all IF NOT EXISTS) inside the caller's transaction."""
    from hydra.db import MIGRATIONS, _split

    sql = dict(MIGRATIONS)[7]
    for statement in _split(sql):
        conn.execute(statement)


# ------------------------------------------------------------------------------------------------ time
def utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _system_now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------------------ model
@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    role: str
    must_change_password: bool
    via: str                         # "session" | "token"
    csrf_token: str | None = None
    session_hash: str | None = None
    token_id: int | None = None
    expires_at: str | None = None
    idle_expires_at: str | None = None

    def has_role(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK[role]


def user_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    return {"id": r["id"], "username": r["username"], "role": r["role"],
            "must_change_password": bool(r["must_change_password"]), "disabled": bool(r["disabled"]),
            "created_at": r["created_at"], "last_login": r["last_login"], "locked_until": r["locked_until"],
            "failed_attempts": r["failed_attempts"]}


def token_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    return {"id": r["id"], "name": r["name"], "prefix": r["prefix"], "username": r.get("username"),
            "created_at": r["created_at"], "last_used_at": r["last_used_at"], "expires_at": r["expires_at"],
            "revoked_at": r["revoked_at"]}


@dataclass
class _Cached:
    value: Any
    at: float


@dataclass
class _Throttle:
    hits: deque[float] = field(default_factory=deque)


# ------------------------------------------------------------------------------------------------ service
class AuthService:
    """All auth state transitions. Reads use short read-only connections; writes go through the single Writer."""

    def __init__(self, writer, settings, reader: Callable[[], Any], clock: Callable[[], datetime] = _system_now):
        self.writer = writer
        self.settings = settings
        self.reader = reader
        self.clock = clock
        self.enabled = bool(settings.auth)
        self.idle_s = int(settings.session_idle_h * 3600)
        self.abs_s = int(settings.session_abs_h * 3600)
        self._lock = threading.Lock()
        self._cache: dict[str, _Cached] = {}
        self._ip_hits: dict[str, _Throttle] = {}
        self._ghost: dict[str, tuple[int, float]] = {}  # unknown username -> (failures, locked_until epoch)
        writer.run(ensure_schema)

    # -------------------------------------------------------------- helpers
    def now(self) -> datetime:
        return self.clock()

    def _forget(self) -> None:
        with self._lock:
            self._cache.clear()

    def credentials_hint(self) -> dict[str, str]:
        return {"file": str(self.settings.admin_credentials_path), "docker": DOCKER_HINT,
                "env": "HYDRA_ADMIN_PASSWORD", "cli": "uv run hydra user reset-password admin"}

    def _user_by_name(self, username: str) -> dict[str, Any] | None:
        with self.reader() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def _user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.reader() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------- bootstrap
    def user_count(self) -> int:
        with self.reader() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def bootstrap_admin(self, *, announce: bool = True) -> dict[str, Any] | None:
        """First start: no users -> create `admin`. Returns {username, password|None, file|None} or None."""
        secret = self.settings.admin_password
        env_pw = (secret.get_secret_value() if secret is not None else "") or None  # empty variable = not set
        password = env_pw or generate_password()
        pw_hash = hash_password(password)
        now = utc(self.now())

        def tx(conn: sqlite3.Connection) -> bool:
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                return False
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at, must_change_password,"
                " password_changed_at) VALUES (?, ?, 'admin', ?, ?, ?)",
                (ADMIN_USERNAME, pw_hash, now, 0 if env_pw else 1, now))
            events.append(conn, "USER_CREATED", {"user_id": cur.lastrowid, "username": ADMIN_USERNAME,
                                                  "role": "admin", "by": "bootstrap",
                                                  "source": "env" if env_pw else "generated"})
            return True

        if not self.writer.run(tx):
            return None
        if env_pw:
            if len(env_pw) < MIN_PASSWORD_LEN:
                log.warning("HYDRA_ADMIN_PASSWORD is shorter than %d characters", MIN_PASSWORD_LEN)
            if announce:
                _banner([f"HYDRA NEXUS: konto administratora '{ADMIN_USERNAME}' z hasłem z HYDRA_ADMIN_PASSWORD",
                         f"  admin account '{ADMIN_USERNAME}' created from HYDRA_ADMIN_PASSWORD (no credentials file)"])
            return {"username": ADMIN_USERNAME, "password": None, "file": None}
        path = write_credentials_file(self.settings.admin_credentials_path, ADMIN_USERNAME, password)
        if announce:
            _banner([
                "HYDRA NEXUS: utworzono pierwsze konto administratora / first administrator account created",
                f"  login / user:      {ADMIN_USERNAME}",
                f"  hasło / password:  {password}   (zmiana przy 1. logowaniu / change at first login)",
                f"  plik / file:       {path} (0600)",
            ])
        return {"username": ADMIN_USERNAME, "password": password, "file": str(path)}

    # -------------------------------------------------------------- throttling
    def _rate_check(self, client: str) -> None:
        """429 when this client address produced RATE_MAX failed logins within RATE_WINDOW_S (successes are free)."""
        at = self.now().timestamp()
        with self._lock:
            t = self._ip_hits.setdefault(client, _Throttle())
            while t.hits and at - t.hits[0] > RATE_WINDOW_S:
                t.hits.popleft()
            if len(t.hits) >= RATE_MAX:
                retry = int(RATE_WINDOW_S - (at - t.hits[0])) + 1
                raise AuthError(429, "rate_limited", "Zbyt wiele prób logowania. Spróbuj ponownie za chwilę.", retry)

    def _rate_fail(self, client: str) -> None:
        with self._lock:
            if len(self._ip_hits) > 10_000:
                self._ip_hits.clear()
            self._ip_hits.setdefault(client, _Throttle()).hits.append(self.now().timestamp())

    def _locked_error(self, until: datetime) -> AuthError:
        retry = max(1, int((until - self.now()).total_seconds()) + 1)
        return AuthError(423, "locked", "Konto tymczasowo zablokowane po wielu nieudanych próbach logowania.", retry)

    # -------------------------------------------------------------- login / logout
    def login(self, username: str, password: str, *, remember: bool = False,
              client: str = "?") -> tuple[str, Principal, dict[str, Any]]:
        """Returns (cookie value, principal, public user). Raises AuthError."""
        if not self.enabled:
            raise AuthError(409, "auth_disabled", "Logowanie jest wyłączone (HYDRA_AUTH=0).")
        self._rate_check(client)
        username = (username or "").strip()[:64]
        now = self.now()
        user = self._user_by_name(username) if username else None
        key = username.lower()
        if user is None:
            failures, until = self._ghost.get(key, (0, 0.0))
            if until > now.timestamp():
                raise self._locked_error(datetime.fromtimestamp(until, UTC))
            verify_password(password or "", _dummy_hash())
            failures += 1
            locked = failures >= MAX_FAILURES
            with self._lock:
                self._ghost[key] = (0, now.timestamp() + LOCK_S) if locked else (failures, 0.0)
                if len(self._ghost) > 10_000:
                    self._ghost.clear()
            self._rate_fail(client)
            self._audit("LOGIN_FAIL", {"username": username, "reason": "unknown_user", "locked": locked})
            if locked:
                raise self._locked_error(now + timedelta(seconds=LOCK_S))
            raise AuthError(401, "invalid_credentials", MSG_INVALID)
        locked_until = parse(user["locked_until"])
        if locked_until and locked_until > now:
            self._rate_fail(client)
            self._audit("LOGIN_FAIL", {"user_id": user["id"], "username": user["username"], "reason": "locked"})
            raise self._locked_error(locked_until)
        ok = verify_password(password or "", user["password_hash"] if not user["disabled"] else _dummy_hash())
        if not ok or user["disabled"]:
            reason = "disabled" if user["disabled"] else "bad_password"

            def fail(conn: sqlite3.Connection) -> bool:
                row = conn.execute("SELECT failed_attempts FROM users WHERE id=?", (user["id"],)).fetchone()
                n = (row[0] if row else 0) + 1
                lock = n >= MAX_FAILURES
                conn.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                             (0 if lock else n, utc(now + timedelta(seconds=LOCK_S)) if lock else None, user["id"]))
                events.append(conn, "LOGIN_FAIL", {"user_id": user["id"], "username": user["username"],
                                                    "reason": reason, "failures": n, "locked": lock})
                return lock

            self._rate_fail(client)
            if self.writer.run(fail):
                raise self._locked_error(now + timedelta(seconds=LOCK_S))
            raise AuthError(401, "invalid_credentials", MSG_INVALID)

        cookie = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        idle = self.abs_s if remember else min(self.idle_s, self.abs_s)
        expires = now + timedelta(seconds=self.abs_s)
        id_hash = _sha(cookie)

        def ok_tx(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE users SET failed_attempts=0, locked_until=NULL, last_login=? WHERE id=?",
                         (utc(now), user["id"]))
            conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (utc(now),))
            conn.execute("INSERT INTO auth_sessions (id_hash, user_id, csrf_token, created_at, last_seen, expires_at,"
                         " idle_s, remember) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (id_hash, user["id"], csrf, utc(now), utc(now), utc(expires), idle, int(remember)))
            events.append(conn, "LOGIN_OK", {"user_id": user["id"], "username": user["username"],
                                              "role": user["role"], "remember": bool(remember),
                                              "session": id_hash[:12]})

        self.writer.run(ok_tx)
        user["last_login"] = utc(now)
        user["failed_attempts"], user["locked_until"] = 0, None
        principal = Principal(user["id"], user["username"], user["role"], bool(user["must_change_password"]),
                              "session", csrf, id_hash, None, utc(expires),
                              utc(min(expires, now + timedelta(seconds=idle))))
        return cookie, principal, user_public(user)

    def logout(self, principal: Principal) -> None:
        if principal.session_hash is None:
            return

        def tx(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM auth_sessions WHERE id_hash=?", (principal.session_hash,))
            events.append(conn, "LOGOUT", {"user_id": principal.user_id, "username": principal.username,
                                            "session": principal.session_hash[:12]})

        self.writer.run(tx)
        self._forget()

    def _audit(self, type_: str, payload: dict[str, Any]) -> None:
        self.writer.run(lambda conn: events.append(conn, type_, payload))

    # -------------------------------------------------------------- request authentication
    def authenticate(self, cookie: str | None, bearer: str | None) -> Principal | None:
        if bearer:
            return self._from_token(bearer)
        if cookie:
            return self._from_session(cookie)
        return None

    def _cached(self, key: str) -> Any:
        with self._lock:
            c = self._cache.get(key)
            if c is not None and time.monotonic() - c.at < CACHE_TTL_S:
                return c.value
        return None

    def _store(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._cache) > 5000:
                self._cache.clear()
            self._cache[key] = _Cached(value, time.monotonic())

    def _from_session(self, cookie: str) -> Principal | None:
        if len(cookie) > 200:
            return None
        id_hash = _sha(cookie)
        row = self._cached("s:" + id_hash)
        if row is None:
            with self.reader() as conn:
                r = conn.execute(
                    "SELECT s.*, u.username, u.role, u.must_change_password, u.disabled FROM auth_sessions s"
                    " JOIN users u ON u.id = s.user_id WHERE s.id_hash=?", (id_hash,)).fetchone()
            if r is None:
                return None
            row = dict(r)
            self._store("s:" + id_hash, row)
        now = self.now()
        expires, last_seen = parse(row["expires_at"]), parse(row["last_seen"])
        if row["disabled"] or expires is None or last_seen is None or now >= expires \
                or (now - last_seen).total_seconds() > row["idle_s"]:
            return None
        if (now - last_seen).total_seconds() >= TOUCH_EVERY_S:
            row["last_seen"] = utc(now)  # cached copy too, so the idle clock restarts for the next request
            self._touch("UPDATE auth_sessions SET last_seen=? WHERE id_hash=?", (utc(now), id_hash))
            last_seen = now
        idle_exp = min(expires, last_seen + timedelta(seconds=row["idle_s"]))
        return Principal(row["user_id"], row["username"], row["role"], bool(row["must_change_password"]), "session",
                         row["csrf_token"], id_hash, None, row["expires_at"], utc(idle_exp))

    def _from_token(self, token: str) -> Principal | None:
        if not token.startswith(TOKEN_PREFIX) or len(token) > 200:
            return None
        h = _sha(token)
        row = self._cached("t:" + h)
        if row is None:
            with self.reader() as conn:
                r = conn.execute(
                    "SELECT t.*, u.username, u.role, u.disabled FROM api_tokens t JOIN users u ON u.id = t.user_id"
                    " WHERE t.token_hash=?", (h,)).fetchone()
            if r is None:
                return None
            row = dict(r)
            self._store("t:" + h, row)
        now = self.now()
        exp = parse(row["expires_at"])
        if row["revoked_at"] or row["disabled"] or (exp is not None and now >= exp):
            return None
        used = parse(row["last_used_at"])
        if used is None or (now - used).total_seconds() >= TOUCH_EVERY_S:
            row["last_used_at"] = utc(now)
            self._touch("UPDATE api_tokens SET last_used_at=? WHERE id=?", (utc(now), row["id"]))
        # A token never carries the forced password change: automation must keep working (the token was issued by
        # a user who could already log in); the browser session still enforces it.
        return Principal(row["user_id"], row["username"], row["role"], False, "token", None, None, row["id"],
                         row["expires_at"], None)

    def _touch(self, sql: str, args: tuple[Any, ...]) -> None:
        try:
            self.writer.submit(lambda conn: conn.execute(sql, args))  # fire and forget, never blocks a request
        except RuntimeError:
            pass

    # -------------------------------------------------------------- password
    def change_password(self, principal: Principal, current: str, new: str) -> dict[str, Any]:
        user = self._user_by_id(principal.user_id)
        if user is None or user["disabled"]:
            raise AuthError(401, "auth_required", "Wymagane logowanie.")
        if not verify_password(current or "", user["password_hash"]):
            self._audit("LOGIN_FAIL", {"user_id": user["id"], "username": user["username"],
                                       "reason": "bad_current_password"})
            raise AuthError(401, "invalid_credentials", "Obecne hasło jest nieprawidłowe.")
        problem = password_problem(new or "", user["username"], current)
        if problem:
            raise AuthError(400, "weak_password", problem)
        pw_hash = hash_password(new)
        now = utc(self.now())

        def tx(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE users SET password_hash=?, must_change_password=0, password_changed_at=? WHERE id=?",
                         (pw_hash, now, user["id"]))
            # Every other session of this user ends; the current one stays.
            conn.execute("DELETE FROM auth_sessions WHERE user_id=? AND id_hash IS NOT ?",
                         (user["id"], principal.session_hash))
            events.append(conn, "PASSWORD_CHANGED", {"user_id": user["id"], "username": user["username"],
                                                      "by": principal.username})

        self.writer.run(tx)
        self._forget()
        user.update(must_change_password=0, password_hash=pw_hash)
        return user_public(user)

    # -------------------------------------------------------------- users (admin)
    def list_users(self) -> list[dict[str, Any]]:
        with self.reader() as conn:
            return [user_public(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]

    def create_user(self, actor: str, username: str, role: str, password: str | None = None,
                    must_change: bool = True) -> tuple[dict[str, Any], str]:
        check_username(username)
        if role not in ROLES:
            raise AuthError(400, "invalid_role", "Nieznana rola.")
        pw = password or generate_password()
        if password is not None and (problem := password_problem(password, username)):
            raise AuthError(400, "weak_password", problem)
        pw_hash = hash_password(pw)
        now = utc(self.now())

        def tx(conn: sqlite3.Connection) -> int:
            if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise AuthError(409, "exists", "Użytkownik o tej nazwie już istnieje.")
            cur = conn.execute("INSERT INTO users (username, password_hash, role, created_at, must_change_password,"
                               " password_changed_at) VALUES (?, ?, ?, ?, ?, ?)",
                               (username, pw_hash, role, now, int(must_change), now))
            events.append(conn, "USER_CREATED", {"user_id": cur.lastrowid, "username": username, "role": role,
                                                  "by": actor})
            return int(cur.lastrowid or 0)

        uid = self.writer.run(tx)
        user = self._user_by_id(uid)
        assert user is not None
        return user_public(user), pw

    def _find(self, ref: int | str) -> dict[str, Any]:
        user = self._user_by_id(ref) if isinstance(ref, int) else self._user_by_name(ref)
        if user is None:
            raise AuthError(404, "not_found", "Nie znaleziono użytkownika.")
        return user

    def _admins_left(self, conn: sqlite3.Connection, excluding: int) -> int:
        return conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND id<>?",
                            (excluding,)).fetchone()[0]

    def update_user(self, actor: str, ref: int | str, *, role: str | None = None,
                    disabled: bool | None = None) -> dict[str, Any]:
        user = self._find(ref)
        if role is not None and role not in ROLES:
            raise AuthError(400, "invalid_role", "Nieznana rola.")

        def tx(conn: sqlite3.Connection) -> None:
            demote = (role is not None and role != "admin") or disabled is True
            if user["role"] == "admin" and demote and not self._admins_left(conn, user["id"]):
                raise AuthError(409, "last_admin", "Nie można odebrać uprawnień ostatniemu aktywnemu administratorowi.")
            changes: dict[str, Any] = {}
            if role is not None and role != user["role"]:
                conn.execute("UPDATE users SET role=? WHERE id=?", (role, user["id"]))
                changes["role"] = role
            if disabled is not None and bool(disabled) != bool(user["disabled"]):
                conn.execute("UPDATE users SET disabled=? WHERE id=?", (int(disabled), user["id"]))
                if disabled:
                    conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user["id"],))
                changes["disabled"] = bool(disabled)
            if changes:
                events.append(conn, "USER_UPDATED", {"user_id": user["id"], "username": user["username"],
                                                      "by": actor, **changes})

        self.writer.run(tx)
        self._forget()
        return user_public(self._find(user["id"]))

    def reset_password(self, actor: str, ref: int | str, password: str | None = None) -> tuple[dict[str, Any], str]:
        user = self._find(ref)
        pw = password or generate_password()
        pw_hash = hash_password(pw)
        now = utc(self.now())

        def tx(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE users SET password_hash=?, must_change_password=?, failed_attempts=0,"
                         " locked_until=NULL, password_changed_at=? WHERE id=?",
                         (pw_hash, 0 if password else 1, now, user["id"]))
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user["id"],))
            events.append(conn, "PASSWORD_CHANGED", {"user_id": user["id"], "username": user["username"],
                                                      "by": actor, "reset": True})

        self.writer.run(tx)
        self._forget()
        return user_public(self._find(user["id"])), pw

    # -------------------------------------------------------------- tokens
    def list_tokens(self, principal: Principal, all_users: bool = False) -> list[dict[str, Any]]:
        sql = ("SELECT t.*, u.username FROM api_tokens t JOIN users u ON u.id=t.user_id"
               + ("" if all_users else " WHERE t.user_id=?") + " ORDER BY t.id DESC")
        with self.reader() as conn:
            rows = conn.execute(sql, () if all_users else (principal.user_id,)).fetchall()
        return [token_public(r) for r in rows]

    def create_token(self, principal: Principal, name: str,
                     expires_days: int | None = None) -> tuple[str, dict[str, Any]]:
        name = (name or "").strip()[:80]
        if not name:
            raise AuthError(400, "invalid_name", "Podaj nazwę tokenu.")
        if expires_days is not None and not 1 <= expires_days <= 3650:
            raise AuthError(400, "invalid_expiry", "Ważność tokenu: od 1 do 3650 dni.")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = self.now()
        exp = utc(now + timedelta(days=expires_days)) if expires_days else None

        def tx(conn: sqlite3.Connection) -> int:
            cur = conn.execute("INSERT INTO api_tokens (user_id, name, prefix, token_hash, created_at, expires_at)"
                               " VALUES (?, ?, ?, ?, ?, ?)",
                               (principal.user_id, name, token[:10], _sha(token), utc(now), exp))
            events.append(conn, "TOKEN_CREATED", {"token_id": cur.lastrowid, "name": name, "prefix": token[:10],
                                                   "user_id": principal.user_id, "username": principal.username,
                                                   "expires_at": exp})
            return int(cur.lastrowid or 0)

        tid = self.writer.run(tx)
        item = next(t for t in self.list_tokens(principal) if t["id"] == tid)
        return token, item

    def revoke_token(self, principal: Principal, token_id: int) -> None:
        with self.reader() as conn:
            row = conn.execute("SELECT user_id, revoked_at, prefix FROM api_tokens WHERE id=?", (token_id,)).fetchone()
        if row is None or (row["user_id"] != principal.user_id and not principal.has_role("admin")):
            raise AuthError(404, "not_found", "Nie znaleziono tokenu.")
        now = utc(self.now())

        def tx(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE api_tokens SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?", (now, token_id))
            events.append(conn, "TOKEN_REVOKED", {"token_id": token_id, "prefix": row["prefix"],
                                                   "by": principal.username})

        self.writer.run(tx)
        self._forget()


# ------------------------------------------------------------------------------------------------ bootstrap io
def write_credentials_file(path: Path, username: str, password: str) -> Path:
    """Write the generated admin credentials once, 0600 from the first byte (umask-proof)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                "HYDRA NEXUS - dane pierwszego administratora / first administrator credentials\n"
                f"utworzono / created: {utc(_system_now())}\n"
                f"login / user: {username}\n"
                f"hasło / password: {password}\n"
                "Przy pierwszym logowaniu system wymusi zmianę hasła; potem ten plik można usunąć.\n"
                "The first login forces a password change; afterwards this file can be deleted.\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def _banner(lines: list[str]) -> None:
    bar = "=" * 96
    text = "\n".join([bar, *lines, bar])
    print(text, flush=True)  # noqa: T201 — the operator reads it in the server log / `docker compose logs api`
    log.info("admin bootstrap: %s", lines[0])
