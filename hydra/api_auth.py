"""Auth API (`/v1/auth/*`) and the request gate (AUTH-LOGIN, docs/SECURITY_AUTH.md).

The gate (`AuthGate`, pure ASGI, installed inside the /api prefix strip and the loopback Host/Origin guard) runs
before every request when HYDRA_AUTH=1:

- public: static UI files, `GET /health` (minimal body without a principal), `/v1/auth/login`, `/v1/auth/session`,
  `/v1/auth/logout`, and the HMAC-verified webhook test receiver `POST /v1/integrations/webhooks/sink`;
- everything else under `/v1/*` (and `/docs`, `/redoc`, `/openapi.json`) needs a session cookie or a
  `Authorization: Bearer hnx_...` personal access token -> 401 `auth_required`;
- a session on a state-changing method must send `X-CSRF-Token` equal to its server-side CSRF token -> 403 `csrf`
  (tokens are not ambient credentials and need no CSRF header);
- a session with `must_change_password` reaches only `/v1/auth/*` -> 403 `password_change_required`;
- roles: viewer = read-only (safe methods + own password/tokens + two read-only POSTs), analyst = every
  analysis/review/import write, admin = users, connector secrets and webhook configuration -> 403 `forbidden`.
"""

from __future__ import annotations

import hmac
import json
import re
from http.cookies import SimpleCookie
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hydra.auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    ROLE_PL,
    SESSION_COOKIE,
    AuthError,
    AuthService,
    Principal,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_PUBLIC = {("POST", "/v1/auth/login"), ("GET", "/v1/auth/session"), ("POST", "/v1/auth/logout"),
           ("POST", "/v1/integrations/webhooks/sink")}
_DOC_PATHS = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
# Reachable with must_change_password (and for a viewer, the only writes besides the read-only POSTs below).
_SELF_SERVICE = re.compile(r"/v1/auth/(session|logout|password|tokens(/\d+)?)")
_VIEWER_POST = {"/v1/graph/export", "/v1/exports/verify"}
# Admin-only writes: user management, connector secrets/configuration, webhook endpoint configuration.
_ADMIN_WRITE = re.compile(
    r"/v1/auth/users(/.*)?"
    r"|/v1/integrations/(?!webhooks/)[^/]+/(secret|connect|enable)"
    r"|/v1/integrations/presets/[^/]+/apply"
    r"|/v1/integrations/webhooks(/(?!deliveries/|sink$)[^/]+)?"
    # WATCH-RT (feat/watch-rt): enabling a source also enables its integration connector; engine start/stop
    r"|/v1/watch/rt/sources/[^/]+(/.*)?"
    r"|/v1/watch/rt/engine(/.*)?"
    # META-COLLECT: collector configuration, diagnostics (spends the Meta budget) and start/pause/poll-now
    r"|/v1/watch/rt/meta/(config|diagnose|collector)"
    # GALLERY: building the local face/logo reference base from imported material
    r"|/v1/reference/build")
_MSG_CSRF = "Brak lub nieprawidłowy token CSRF. Odśwież stronę i spróbuj ponownie."
_ADMIN_READ = re.compile(r"/v1/auth/users(/.*)?")


def is_protected(path: str, scope: dict[str, Any]) -> bool:
    """API paths need a principal; static UI files do not."""
    return path == "/v1" or path.startswith("/v1/") or path in _DOC_PATHS or bool(scope.get("hydra.api"))


def required_role(method: str, path: str) -> str:
    if method in _UNSAFE:
        if _ADMIN_WRITE.fullmatch(path):
            return "admin"
        if _SELF_SERVICE.fullmatch(path) or path in _VIEWER_POST:
            return "viewer"
        return "analyst"
    if _ADMIN_READ.fullmatch(path):
        return "admin"
    return "viewer"


def _deny(status: int, code: str, message: str, extra: list[tuple[bytes, bytes]] | None = None):
    body = json.dumps({"detail": {"code": code, "message": message}}, ensure_ascii=False).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store"), *(extra or [])]

    async def send_it(send):
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return send_it


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in scope.get("headers", []):
        key = k.decode("latin-1").lower()
        val = v.decode("latin-1")
        out[key] = f"{out[key]}; {val}" if key == "cookie" and key in out else val
    return out


def _cookie(headers: dict[str, str], name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    try:
        jar = SimpleCookie()
        jar.load(raw)
    except Exception:  # noqa: BLE001 — a malformed cookie header is "no cookie"
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def principal_from_scope(scope: dict[str, Any], svc: AuthService) -> Principal | None:
    headers = _headers(scope)
    auth = headers.get("authorization", "")
    bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else None
    return svc.authenticate(_cookie(headers, SESSION_COOKIE), bearer)


class AuthGate:
    """ASGI middleware enforcing the rules in the module docstring."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        hydra_state = getattr(scope.get("app").state, "hydra", None) if scope.get("app") is not None else None
        svc: AuthService | None = getattr(hydra_state, "auth", None)
        if svc is None or not svc.enabled:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if not is_protected(path, scope) and path != "/health":
            return await self.app(scope, receive, send)
        try:
            principal = principal_from_scope(scope, svc)
        except Exception:  # noqa: BLE001 — never a 500 from a hostile header
            principal = None
        scope["hydra.principal"] = principal
        if path == "/health" or (method, path) in _PUBLIC:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            if principal is None:
                await send({"type": "websocket.close", "code": 4401})
                return None
            return await self.app(scope, receive, send)
        if principal is None:
            return await _deny(401, "auth_required", "Wymagane logowanie.",
                               [(b"www-authenticate", b'Bearer realm="hydra"')])(send)
        if principal.must_change_password and not _SELF_SERVICE.fullmatch(path):
            return await _deny(403, "password_change_required",
                               "Najpierw zmień hasło (wymagane przy pierwszym logowaniu).")(send)
        if method in _UNSAFE and principal.via == "session":
            sent = _headers(scope).get(CSRF_HEADER, "")
            if not sent or not principal.csrf_token or not hmac.compare_digest(sent, principal.csrf_token):
                return await _deny(403, "csrf", _MSG_CSRF)(send)
        need = required_role(method, path)
        if not principal.has_role(need):
            return await _deny(403, "forbidden", f"Brak uprawnień: wymagana rola {ROLE_PL[need]}.")(send)
        return await self.app(scope, receive, send)


# --------------------------------------------------------------------------------------------- routes
def _svc(request: Request) -> AuthService:
    return request.app.state.hydra.auth


def acting_analyst(request: Request, claimed: str | None = None) -> str:
    """Who an analyst decision is recorded as (audit B2, CR-ASTRA-SW-10). With the auth gate on, always the
    authenticated principal (session or API token owner); a name in the request body is ignored, so nobody can
    write another user's name into the reviews table or the hash-chained audit log. Without a principal
    (HYDRA_AUTH=0, local single-user mode) the old behaviour stays: the body name, else "operator"."""
    p = request.scope.get("hydra.principal")
    name = getattr(p, "username", None)
    if name:
        return str(name)
    svc = getattr(getattr(request.app.state, "hydra", None), "auth", None)
    if svc is not None and getattr(svc, "enabled", False):
        return "operator"  # unreachable behind the gate (401); never trust the body when auth is on
    return claimed or "operator"


def _principal(request: Request) -> Principal:
    p = request.scope.get("hydra.principal")
    if p is None:
        raise AuthError(401, "auth_required", "Wymagane logowanie.")
    return p


def _error(exc: AuthError) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if exc.retry_after_s is not None:
        headers["Retry-After"] = str(exc.retry_after_s)
    return JSONResponse({"detail": exc.detail()}, status_code=exc.status, headers=headers)


def install_auth_errors(app) -> None:
    app.add_exception_handler(AuthError, lambda _req, exc: _error(exc))


def _set_cookies(response: Response, svc: AuthService, cookie: str, p: Principal, remember: bool) -> None:
    max_age = svc.abs_s if remember else None  # without "remember me": a browser-session cookie
    response.set_cookie(SESSION_COOKIE, cookie, max_age=max_age, httponly=True, samesite="strict", path="/")
    response.set_cookie(CSRF_COOKIE, p.csrf_token or "", max_age=max_age, httponly=False, samesite="strict",
                        path="/")


def _clear_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", samesite="strict")


def _me(svc: AuthService, p: Principal) -> dict[str, Any]:
    from hydra.auth import user_public

    user = svc._user_by_id(p.user_id)
    return user_public(user) if user else {"id": p.user_id, "username": p.username, "role": p.role}


class LoginBody(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1024)
    remember: bool = False


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


class UserCreateBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    role: str = "analyst"


class UserPatchBody(BaseModel):
    role: str | None = None
    disabled: bool | None = None


@router.get("/session", summary="Stan sesji / Session state")
def session(request: Request, response: Response) -> dict[str, Any]:
    svc = _svc(request)
    response.headers["Cache-Control"] = "no-store"
    out: dict[str, Any] = {"auth_enabled": svc.enabled, "authenticated": False,
                           "credentials_hint": svc.credentials_hint()}
    p = request.scope.get("hydra.principal")
    if svc.enabled and p is not None:
        out.update(authenticated=True, user=_me(svc, p), via=p.via, csrf_token=p.csrf_token,
                   expires_at=p.expires_at, idle_expires_at=p.idle_expires_at)
    return out


@router.post("/login", summary="Logowanie / Log in")
def login(request: Request, body: LoginBody) -> JSONResponse:
    svc = _svc(request)
    client = request.client.host if request.client else "?"
    try:
        cookie, p, user = svc.login(body.username, body.password, remember=body.remember, client=client)
    except AuthError as exc:
        return _error(exc)
    resp = JSONResponse({"user": user, "csrf_token": p.csrf_token, "expires_at": p.expires_at,
                         "idle_expires_at": p.idle_expires_at}, headers={"Cache-Control": "no-store"})
    _set_cookies(resp, svc, cookie, p, body.remember)
    return resp


@router.post("/logout", status_code=204, summary="Wylogowanie / Log out")
def logout(request: Request) -> Response:
    p = request.scope.get("hydra.principal")
    if p is not None:
        _svc(request).logout(p)
    resp = Response(status_code=204)
    _clear_cookies(resp)
    return resp


@router.post("/password", summary="Zmiana hasła / Change password")
def change_password(request: Request, body: PasswordBody) -> dict[str, Any]:
    p = _principal(request)
    return {"user": _svc(request).change_password(p, body.current_password, body.new_password)}


@router.get("/tokens", summary="Tokeny API / Personal access tokens")
def list_tokens(request: Request, all: bool = Query(False)) -> dict[str, Any]:  # noqa: A002
    p = _principal(request)
    return {"items": _svc(request).list_tokens(p, all_users=all and p.has_role("admin"))}


@router.post("/tokens", status_code=201, summary="Nowy token API (pokazywany raz) / New token (shown once)")
def create_token(request: Request, body: TokenBody) -> dict[str, Any]:
    token, item = _svc(request).create_token(_principal(request), body.name, body.expires_days)
    return {"token": token, "item": item}


@router.delete("/tokens/{token_id}", status_code=204, summary="Unieważnij token / Revoke token")
def revoke_token(request: Request, token_id: int) -> Response:
    _svc(request).revoke_token(_principal(request), token_id)
    return Response(status_code=204)


@router.get("/users", summary="Użytkownicy (admin) / Users")
def list_users(request: Request) -> dict[str, Any]:
    return {"items": _svc(request).list_users()}


@router.post("/users", status_code=201, summary="Nowy użytkownik (hasło pokazywane raz) / New user")
def create_user(request: Request, body: UserCreateBody) -> dict[str, Any]:
    user, password = _svc(request).create_user(_principal(request).username, body.username, body.role)
    return {"user": user, "password": password}


@router.patch("/users/{user_id}", summary="Zmień rolę / wyłącz konto / Update user")
def update_user(request: Request, user_id: int, body: UserPatchBody) -> dict[str, Any]:
    p = _principal(request)
    if body.disabled and user_id == p.user_id:
        raise AuthError(409, "self_disable", "Nie można wyłączyć własnego konta.")
    return {"user": _svc(request).update_user(p.username, user_id, role=body.role, disabled=body.disabled)}


@router.post("/users/{user_id}/reset-password", summary="Nowe hasło (pokazywane raz) / Reset password")
def reset_password(request: Request, user_id: int) -> dict[str, Any]:
    user, password = _svc(request).reset_password(_principal(request).username, user_id)
    return {"user": user, "password": password}
