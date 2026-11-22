"""Integrations & API hub (`/v1/integrations`): connector registry, secrets, test, one-click connect, dry-run preview,
export download, run (lookups / pushes) and presets.

Everything is off by default and fails closed offline. Secret values are accepted only by `PUT .../secret` and
`POST .../connect`; no response, audit event, state file or cache ever contains them (only `configured`, `source`
and the last 4 characters of long values). Planned connectors answer 409 for every action. The webhooks router
(`hydra.api_webhooks`, prefix `/v1/integrations/webhooks`) is included before this one.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from hydra import events
from hydra.integrations import exports, http, registry, secrets, state
from hydra.integrations.registry import Connector
from hydra.netguard import offline

router = APIRouter(prefix="/v1/integrations", tags=["integrations"])

MAX_RUN_INDICATORS = 25
MAX_BODY_INDICATORS = 100
PREVIEW_MAX = 200_000
StatusId = Literal["configured", "not_configured", "offline", "disabled", "planned"]
TestStatus = Literal["ok", "offline", "not_configured", "error", "disabled"]
OFFLINE_EXAMPLE = {"detail": http.MESSAGE_OFFLINE_PL, "status": "offline"}


# ------------------------------------------------------------------------------------------------ models


class SecretStatus(BaseModel):
    """Stan jednego sekretu (nigdy wartość) / Status of one secret (never its value)."""

    name: str = Field(description="Nazwa zmiennej, np. VT_API_KEY / Variable name.")
    configured: bool = Field(description="Czy wartość jest ustawiona / Whether a value is set.")
    source: Literal["env", "file"] | None = Field(None, description="env = zmienna środowiskowa, file = plik 0600.")
    last4: str | None = Field(None, description="Ostatnie 4 znaki (tylko dla wartości >= 12 znaków).")


class LastResult(BaseModel):
    at: str | None = None
    ok: bool = False
    status: str = ""
    message_pl: str = ""
    ms: int | None = None


class Usage(BaseModel):
    requests_60m: int = Field(description="Zapytania w ostatnich 60 min / Requests in the last 60 minutes.")
    limit_per_min: float | None = Field(None, description="Limit HYDRA na minutę / HYDRA's per-minute cap.")


class ConnectorOut(BaseModel):
    """Opis konektora i jego stan / Connector descriptor and current state."""

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "id": "virustotal", "name": "VirusTotal", "category": "enrichment", "direction": "inbound",
        "auth": "api_key", "level": "live", "formats": [], "status": "not_configured", "enabled": False,
        "can_connect": True, "can_test": True, "can_run": True, "can_export": False,
        "secrets": [{"name": "VT_API_KEY", "configured": False, "source": None, "last4": None}]}]})

    id: str
    name: str
    category: str
    direction: str
    auth: str
    level: Literal["live", "export-format", "planned"]
    formats: list[str]
    description_pl: str
    setup_pl: list[str]
    notes_pl: str
    docs_url: str
    licence_pl: str
    rate_limit_pl: str
    rate_limit_per_min: float | None
    data_out: list[str] = Field(description="Pola wysyłane do dostawcy / Fields sent to the provider.")
    data_out_pl: str
    never_sent_pl: str
    base_url: str | None = None
    secrets: list[SecretStatus]
    status: StatusId = Field(description="Skonfigurowane / Nieskonfigurowane / Offline / Wyłączone / Planowane.")
    enabled: bool
    can_connect: bool
    can_test: bool
    can_run: bool
    can_export: bool
    last_test: LastResult | None = None
    last_run: LastResult | None = None
    usage: Usage | None = None


class AuditEvent(BaseModel):
    seq: int
    ts: str
    type: str
    payload: dict[str, Any]


class ConnectorDetail(ConnectorOut):
    audit: list[AuditEvent] = Field(default_factory=list, description="Ostatnie 20 zdarzeń INTEGRATION_*.")
    runs: list[LastResult] = Field(default_factory=list, description="Ostatnie uruchomienia (do 10).")


class Option(BaseModel):
    id: str
    label_pl: str
    description_pl: str


class PresetOut(BaseModel):
    id: str
    name_pl: str
    description_pl: str
    connectors: list[str]


class IntegrationsIndex(BaseModel):
    offline: bool = Field(description="HYDRA_OFFLINE=1: wszystkie połączenia wychodzące zablokowane.")
    enrich: bool = Field(description="HYDRA_ENRICH=1: istniejące moduły wzbogacania mogą pytać na żywo.")
    generated_at: str
    categories: list[Option]
    levels: list[Option]
    presets: list[PresetOut]
    connectors: list[ConnectorOut]


class SecretIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "VT_API_KEY", "value": "<klucz API>"}]})

    name: str = Field(description="Jedna z nazw z `secrets` konektora / One of the connector's secret names.")
    value: str = Field(min_length=1, max_length=8192, description="Wartość (nigdy nie jest zwracana).")


class SecretSaved(BaseModel):
    name: str
    configured: bool
    source: Literal["env", "file"] | None = None
    last4: str | None = None


class EnableIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"enabled": True}]})

    enabled: bool


class TestOut(BaseModel):
    """Wynik testu połączenia / Connection test result."""

    model_config = ConfigDict(json_schema_extra={"examples": [
        {"ok": False, "status": "offline", "message_pl": http.MESSAGE_OFFLINE_PL, "ms": 0,
         "at": "2026-11-22T03:00:00+00:00"}]})

    ok: bool
    status: TestStatus
    message_pl: str
    ms: int
    at: str


class ConnectIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"secrets": {"URLHAUS_AUTH_KEY": "<klucz>"}}]})

    secrets: dict[str, str] | None = Field(None, description="Sekrety do zapisania przed testem (opcjonalne).")


class Step(BaseModel):
    id: Literal["secret", "test", "enable", "export"]
    ok: bool
    message_pl: str


class ConnectOut(BaseModel):
    ok: bool
    steps: list[Step]
    connector: ConnectorOut


class PreviewIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"run_id": None, "campaign_id": "c-001",
                                                               "format": "misp", "limit": 50}]})

    run_id: str | None = Field(None, description="Przebieg analizy (domyślnie najnowszy).")
    campaign_id: str | None = Field(None, description="Jedna kampania zamiast całego przebiegu.")
    format: str | None = Field(None, description="Format z listy `formats` konektora (domyślnie pierwszy).")
    limit: int | None = Field(None, ge=1, le=10_000, description="Maksymalna liczba wskaźników.")
    indicators: list[str] | None = Field(None, max_length=MAX_BODY_INDICATORS,
                                         description="Konektory wyszukujące: wskaźniki do podglądu zapytań.")


class PreviewOut(BaseModel):
    """Podgląd na sucho: nic nie jest wysyłane / Dry run: nothing is sent."""

    connector: str
    format: str
    method: str
    destination: str = Field(description="scheme://host/ścieżka albo 'plik' (pobranie).")
    headers: dict[str, str] = Field(description="Nagłówki z sekretami zastąpionymi '•••• + last4'.")
    content_type: str
    bytes: int
    sha256: str
    fields_sent: list[str]
    redactions: list[str]
    payload: Any
    truncated: bool = False


class RunIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"indicators": ["example.com", "1.1.1.1"]},
                                                              {"campaign_id": "c-001"}]})

    run_id: str | None = None
    campaign_id: str | None = None
    indicators: list[str] | None = Field(None, max_length=MAX_BODY_INDICATORS,
                                         description="Wskaźniki (domena, URL, IP, SHA-256); domyślnie z przebiegu.")


class Verdict(BaseModel):
    indicator: str
    kind: str | None = None
    provider: str
    verdict: Literal["malicious", "suspicious", "clean", "unknown"]
    details: dict[str, Any]
    fetched_at: str | None = None
    cached: bool | None = None


class RunOut(BaseModel):
    ok: bool
    status: str
    message_pl: str
    connector: str
    ms: int
    results: list[Verdict] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict, description="Liczba werdyktów wg rodzaju.")
    rejected: list[str] = Field(default_factory=list, description="Wskaźniki odrzucone (zły format / rodzaj).")
    sent: dict[str, Any] | None = Field(None, description="Wysyłka: status HTTP, bajty, sha256, pola.")


class PresetResult(BaseModel):
    connector: str
    ok: bool
    status: str
    message_pl: str
    steps_pl: list[str] = Field(default_factory=list, description="Kolejne kroki jako osobne zdania / One sentence "
                                                                  "per step (message_pl joins them with ' · ').")


class PresetApplyOut(BaseModel):
    preset: PresetOut
    results: list[PresetResult]


# ------------------------------------------------------------------------------------------------ helpers


def _app(request: Request) -> Any:
    return request.app.state.hydra


def _settings(request: Request) -> Any:
    return _app(request).settings


def _now() -> str:
    return events.utcnow()


def _conn_or_404(cid: str) -> Connector:
    c = registry.get(cid)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Nieznany konektor: {cid}")
    return c


def _not_planned(c: Connector, what: str) -> None:
    if c.level == "planned":
        raise HTTPException(status_code=409, detail=f"Konektor {c.name} jest planowany (tylko opis): {what} "
                                                    "niedostępne.")


def _managed_elsewhere(c: Connector) -> bool:
    return c.id == "webhook-generic"


def _inbound(c: Connector) -> bool:
    return c.level == "live" and c.direction in ("inbound", "both")


def _can_act(c: Connector) -> bool:
    return c.level != "planned" and not _managed_elsewhere(c) and (_inbound(c) or registry.can_push(c))


def _secrets_ok(c: Connector) -> bool:
    if c.id == "meta-adlibrary":
        from hydra.integrations.clients.meta_adlibrary import token

        return token() is not None
    return all(s["configured"] for s in secrets.secret_status(c.id, c.secrets))


def _status(c: Connector, settings: Any, enabled: bool) -> str:
    if c.level == "planned":
        return "planned"
    if c.id == "cert-pl":
        from hydra.enrich import cert_pl

        if not cert_pl.available():
            return "not_configured"
    if not _secrets_ok(c):
        return "not_configured"
    if c.level == "export-format" or _managed_elsewhere(c):
        return "configured"
    if registry.needs_network(c) and offline(settings):
        return "offline"
    return "configured" if enabled else "disabled"


def _secret_list(c: Connector) -> list[dict[str, Any]]:
    out = secrets.secret_status(c.id, c.secrets)
    if c.id == "meta-adlibrary" and out and not out[0]["configured"]:
        from hydra.integrations.clients.meta_adlibrary import token

        tok = token()  # legacy HYDRA_META_TOKEN
        if tok:
            out[0] = {"name": out[0]["name"], "configured": True, "source": "env", "last4": secrets.last4(tok)}
    return out


def connector_out(c: Connector, settings: Any) -> dict[str, Any]:
    st = state.connector_state(settings, c.id)
    enabled = bool(st.get("enabled"))
    return {
        "id": c.id, "name": c.name, "category": c.category, "direction": c.direction, "auth": c.auth,
        "level": c.level, "formats": list(c.formats), "description_pl": c.description_pl,
        "setup_pl": list(c.setup_pl), "notes_pl": c.notes_pl, "docs_url": c.docs_url, "licence_pl": c.licence_pl,
        "rate_limit_pl": c.rate_limit_pl, "rate_limit_per_min": c.rate_limit_per_min, "data_out": list(c.data_out),
        "data_out_pl": c.data_out_pl, "never_sent_pl": c.never_sent_pl, "base_url": c.base_url,
        "secrets": _secret_list(c), "status": _status(c, settings, enabled), "enabled": enabled,
        "can_connect": c.level != "planned" and not _managed_elsewhere(c), "can_test": _can_act(c),
        "can_run": _can_act(c), "can_export": c.level != "planned" and bool(c.formats),
        "last_test": st.get("last_test"), "last_run": st.get("last_run"),
        "usage": state.usage(settings, c.id, c.rate_limit_per_min) if registry.needs_network(c) else None,
    }


def _audit(request: Request, type_: str, payload: dict[str, Any]) -> None:
    from hydra.redact import redact_obj

    payload = redact_obj(payload)
    try:
        _app(request).writer.run(lambda conn: events.append(conn, type_, payload), timeout=30)
    except Exception:  # noqa: BLE001 — logging must never fail the action
        pass


def _offline_response() -> JSONResponse:
    return JSONResponse(status_code=503, content=OFFLINE_EXAMPLE)


def _map_status(s: str) -> str:
    return s if s in ("ok", "offline", "not_configured", "error", "disabled") else "error"


def _run_test(request: Request, c: Connector) -> dict[str, Any]:
    """Explicit test. Never automatic. Offline: no transport call."""
    settings = _settings(request)
    t0 = time.monotonic()
    ok, status, msg = False, "error", ""
    try:
        if registry.needs_network(c) and offline(settings):
            ok, status, msg = False, "offline", http.MESSAGE_OFFLINE_PL
        elif not _secrets_ok(c):
            names = ", ".join(s["name"] for s in _secret_list(c) if not s["configured"])
            ok, status, msg = False, "not_configured", f"Brak sekretów: {names}. Zapisz je i spróbuj ponownie."
        else:
            ok, status, msg = _dispatch_test(request, c)
    except http.IntegrationError as exc:
        ok, status, msg = False, _map_status(exc.status), exc.message_pl
    except Exception as exc:  # noqa: BLE001 — a client bug must surface as a failed test, never leak a secret
        vals = [v for v in _secret_values(c) if v]
        ok, status, msg = False, "error", f"Błąd testu: {type(exc).__name__}: {http.scrub(exc, vals)[:200]}"
    res = {"ok": ok, "status": _map_status(status), "message_pl": http.scrub(msg, _secret_values(c)),
           "ms": int((time.monotonic() - t0) * 1000), "at": _now()}
    state.record_test(settings, c.id, res)
    _audit(request, "INTEGRATION_TEST", {"connector": c.id, "ok": res["ok"], "status": res["status"],
                                         "ms": res["ms"]})
    return res


def _secret_values(c: Connector) -> list[str]:
    from hydra.integrations.clients import keys_for

    return [v for v in keys_for(c.id).values() if v]


def _dispatch_test(request: Request, c: Connector) -> tuple[bool, str, str]:
    settings, writer = _settings(request), _app(request).writer
    from hydra.integrations import clients

    if c.id == "meta-adlibrary":
        from hydra.integrations.clients import meta_adlibrary

        return meta_adlibrary.test(settings, writer)
    if c.id == "openphish":
        from hydra.integrations.clients import openphish

        m = openphish.refresh(settings, writer, require_enabled=False)
        return True, "ok", f"Feed OpenPhish dostępny: {m.get('entries', 0)} URL-i (pobrany {m.get('fetched_at')})."
    if registry.can_push(c):
        from hydra.integrations.clients import push

        return push.test(c.id, settings, writer)
    if c.live_impl == "hydra.integrations.clients.local":
        from hydra.integrations.clients import local

        return local.test(c.id, settings)
    mod = clients.module(c.id)
    if mod is None:
        return False, "error", "Brak klienta dla tego konektora."
    keys = clients.keys_for(c.id)
    req = mod.test_request(keys)
    res = http.request(c.id, req.method, req.url, headers=req.headers, body=req.body, settings=settings,
                       writer=writer, secrets=[v for v in keys.values() if v], require_enabled=False)
    if res.ok or res.status_code == 404:
        return True, "ok", f"Połączenie działa (HTTP {res.status_code}), klucz przyjęty."
    if res.status_code in (401, 403):
        return False, "error", f"Dostawca odrzucił klucz (HTTP {res.status_code})."
    if res.status_code == 429:
        return False, "error", "Dostawca zgłasza przekroczenie limitu (HTTP 429)."
    return False, "error", f"Dostawca odpowiedział HTTP {res.status_code}."


def _store_secrets(c: Connector, values: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for name, value in values.items():
        if name not in c.secrets:
            raise HTTPException(status_code=422, detail=f"Konektor {c.id} nie ma sekretu {name}.")
        try:
            out.append(secrets.set_secret(c.id, name, value))
        except secrets.SecretError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    return out


def _connect(request: Request, c: Connector, values: dict[str, str] | None) -> dict[str, Any]:
    settings = _settings(request)
    steps: list[dict[str, Any]] = []
    if values:
        saved = _store_secrets(c, values)
        _audit(request, "INTEGRATION_SECRET", {"connector": c.id, "action": "set", "names": sorted(values)})
        steps.append({"id": "secret", "ok": True, "message_pl": "Zapisano sekrety: " + ", ".join(
            s["name"] for s in saved) + " (plik 0600, wartości nie są zwracane)."})
    ok = True
    if _can_act(c):
        t = _run_test(request, c)
        steps.append({"id": "test", "ok": t["ok"], "message_pl": t["message_pl"]})
        if t["ok"] or t["status"] == "offline":
            state.set_enabled(settings, c.id, True)
            msg = ("Włączono. Tryb offline: połączenia ruszą dopiero po uruchomieniu z HYDRA_OFFLINE=0."
                   if t["status"] == "offline" else "Konektor włączony.")
            steps.append({"id": "enable", "ok": True, "message_pl": msg})
        else:
            steps.append({"id": "enable", "ok": False, "message_pl": "Nie włączono: test nieudany."})
        ok = bool(t["ok"])
    if c.formats:
        steps.append({"id": "export", "ok": True, "message_pl": "Eksport do pliku dostępny (działa offline): "
                                                                + ", ".join(c.formats) + "."})
        if not _can_act(c):
            state.set_enabled(settings, c.id, True)
            steps.append({"id": "enable", "ok": True, "message_pl": "Konektor włączony."})
    _audit(request, "INTEGRATION_CONNECT", {"connector": c.id, "ok": ok})
    return {"ok": ok, "steps": steps, "connector": connector_out(c, settings)}


def _mask(value: str, secret_vals: list[str]) -> str:
    out = value
    for v in sorted(secret_vals, key=len, reverse=True):
        if v and v in out:
            out = out.replace(v, "•••• " + (secrets.last4(v) or ""))
    return out.rstrip()


def _truncate(obj: Any) -> tuple[Any, bool]:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    if len(text.encode()) <= PREVIEW_MAX:
        return obj, False
    return text.encode()[:PREVIEW_MAX].decode("utf-8", "ignore"), True


def _key_error(exc: KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc).strip("'\""))


def _indicators(request: Request, body_ind: list[str] | None, run_id: str | None, campaign_id: str | None
                ) -> tuple[list[str], list[str]]:
    from hydra.integrations.clients import kind_of

    if body_ind is not None:
        good = [i.strip() for i in body_ind if kind_of(i)]
        return good, [i[:200] for i in body_ind if not kind_of(i)]
    try:
        col = exports.collect(_settings(request), run_id, campaign_id, limit=MAX_RUN_INDICATORS)
    except KeyError as exc:
        raise _key_error(exc) from None
    return [i["value"] for i in col.indicators], []


# ------------------------------------------------------------------------------------------------ routes
# Static paths first: `/presets` must not be captured by `/{connector_id}`.


@router.get("", response_model=IntegrationsIndex, summary="Lista konektorów / List connectors",
            description="Wszystkie konektory z poziomem implementacji (live / export-format / planned), stanem "
                        "(Skonfigurowane, Nieskonfigurowane, Offline, Wyłączone, Planowane), polami wysyłanymi do "
                        "dostawcy i stanem sekretów (bez wartości).\n\nAll connectors with implementation level, "
                        "status, the fields sent to each provider and secret status (never values). Read-only, no "
                        "network.")
def list_connectors(request: Request) -> dict[str, Any]:
    s = _settings(request)
    return {"offline": offline(s), "enrich": bool(getattr(s, "enrich", False)), "generated_at": _now(),
            "categories": [{"id": k, "label_pl": v[0], "description_pl": v[1]} for k, v in
                           registry.CATEGORIES.items()],
            "levels": [{"id": k, "label_pl": v[0], "description_pl": v[1]} for k, v in registry.LEVELS.items()],
            "presets": [_preset_out(p) for p in registry.PRESETS],
            "connectors": [connector_out(c, s) for c in registry.CONNECTORS]}


def _preset_out(p: registry.Preset) -> dict[str, Any]:
    return {"id": p.id, "name_pl": p.name_pl, "description_pl": p.description_pl, "connectors": list(p.connectors)}


@router.get("/presets", response_model=list[PresetOut], summary="Pakiety konektorów / Connector presets",
            description="Gotowe zestawy (CERT/MISP, SIEM, Meta Ad Library, reputacja). / Ready-made bundles.")
def list_presets() -> list[dict[str, Any]]:
    return [_preset_out(p) for p in registry.PRESETS]


def _sentence(text: str) -> str:
    """One step message as a clean sentence: no trailing ';' or doubled punctuation, ends with one full stop
    (or the ')' / '?' / '!' it already ends with)."""
    t = str(text).strip().rstrip(";, ").strip()
    while t.endswith(".."):
        t = t[:-1]
    if t.endswith(".") and len(t) > 1 and t[-2] == ")":
        return t
    return t if t.endswith((".", "!", "?")) else f"{t}."


@router.post("/presets/{preset_id}/apply", response_model=PresetApplyOut,
             summary="Zastosuj pakiet / Apply a preset",
             description="Dla każdego konektora pakietu: test (gdy możliwy) i włączenie, bez zapisywania sekretów. "
                         "Konektory planowane i bez sekretów są pomijane z opisem.\n\nFor each connector: test "
                         "(where possible) and enable; never stores secrets. Offline: steps report `offline`.",
             responses={404: {"description": "Nieznany pakiet / Unknown preset"}})
def apply_preset(request: Request, preset_id: str) -> dict[str, Any]:
    p = registry.preset(preset_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Nieznany pakiet: {preset_id}")
    results = []
    for cid in p.connectors:
        c = registry.get(cid)
        if c is None or c.level == "planned":
            msg = "Konektor planowany: pominięty."
            results.append({"connector": cid, "ok": False, "status": "planned", "message_pl": msg, "steps_pl": [msg]})
            continue
        if not _secrets_ok(c):
            missing = [st["name"] for st in secrets.secret_status(c.id, c.secrets) if not st["configured"]]
            if c.level == "export-format":
                parts = ["Plik eksportu działa bez klucza (także offline).",
                         f"Wysyłka (opcjonalna) wymaga: {', '.join(missing)}."]
            else:
                parts = [f"Wymaga klucza API: {', '.join(missing) or 'token'}.",
                         "Uzupełnij go w karcie pakietu („Połącz”)."]
            results.append({"connector": cid, "ok": False, "status": "not_configured",
                            "message_pl": " · ".join(parts), "steps_pl": parts})
            continue
        r = _connect(request, c, None)
        out = r["connector"]
        parts = [_sentence(s["message_pl"]) for s in r["steps"] if str(s.get("message_pl") or "").strip()]
        results.append({"connector": cid, "ok": r["ok"], "status": out["status"],
                        "message_pl": " · ".join(parts) or "Gotowe.", "steps_pl": parts or ["Gotowe."]})
    _audit(request, "INTEGRATION_PRESET", {"preset": p.id, "ok": sum(1 for r in results if r["ok"])})
    return {"preset": _preset_out(p), "results": results}


@router.get("/{connector_id}", response_model=ConnectorDetail, summary="Szczegóły konektora / Connector detail",
            description="Konektor, ostatnie 20 zdarzeń audytu INTEGRATION_* i ostatnie uruchomienia.\n\n"
                        "Connector plus the last 20 INTEGRATION_* audit events and recent runs.",
            responses={404: {"description": "Nieznany konektor / Unknown connector"}})
def connector_detail(request: Request, connector_id: str) -> dict[str, Any]:
    c = _conn_or_404(connector_id)
    s = _settings(request)
    with _app(request).reader() as conn:
        rows = conn.execute(
            "SELECT seq, ts, type, payload FROM events WHERE type LIKE 'INTEGRATION_%' AND "
            "json_extract(payload, '$.connector') = ? ORDER BY seq DESC LIMIT 20", (c.id,)).fetchall()
    audit = [{"seq": r["seq"], "ts": r["ts"], "type": r["type"], "payload": json.loads(r["payload"])} for r in rows]
    return {**connector_out(c, s), "audit": audit, "runs": state.connector_state(s, c.id).get("runs") or []}


@router.put("/{connector_id}/secret", response_model=SecretSaved, summary="Zapisz sekret / Store a secret",
            description="Zapisuje jeden sekret w `~/.config/hydra/<id>.env` (katalog 0700, plik 0600). Wartość nigdy "
                        "nie jest zwracana, logowana ani audytowana (audyt: tylko nazwa).\n\nStores one secret in a "
                        "0600 file; the value is never returned, logged or audited.",
            responses={404: {"description": "Nieznany konektor"}, 409: {"description": "Konektor planowany"},
                       422: {"description": "Zła nazwa lub wartość / Bad name or value"}})
def put_secret(request: Request, connector_id: str, body: SecretIn) -> dict[str, Any]:
    c = _conn_or_404(connector_id)
    _not_planned(c, "zapis sekretu")
    saved = _store_secrets(c, {body.name: body.value})[0]
    _audit(request, "INTEGRATION_SECRET", {"connector": c.id, "action": "set", "names": [body.name]})
    return saved


@router.delete("/{connector_id}/secret", response_model=SecretSaved, summary="Usuń sekret / Delete a secret",
               description="Usuwa sekret z pliku. Zmiennej środowiskowej nie da się usunąć z API (wtedy "
                           "`configured` zostaje true, `source: env`).\n\nRemoves the secret from the file; an "
                           "environment variable cannot be cleared here.",
               responses={404: {"description": "Nieznany konektor"}, 409: {"description": "Konektor planowany"},
                          422: {"description": "Zła nazwa"}})
def delete_secret(request: Request, connector_id: str, name: str = Query(..., description="Nazwa sekretu")
                  ) -> dict[str, Any]:
    c = _conn_or_404(connector_id)
    _not_planned(c, "usunięcie sekretu")
    if name not in c.secrets:
        raise HTTPException(status_code=422, detail=f"Konektor {c.id} nie ma sekretu {name}.")
    st = secrets.clear_secret(c.id, name)
    _audit(request, "INTEGRATION_SECRET", {"connector": c.id, "action": "clear", "names": [name]})
    return st


@router.post("/{connector_id}/enable", response_model=ConnectorOut, summary="Włącz / wyłącz / Enable or disable",
             description="Jawne włączenie konektora (domyślnie wszystko wyłączone). Włączenie offline nie otwiera "
                         "połączeń: status pokazuje `offline`.\n\nExplicit opt-in; offline the status stays "
                         "`offline`.",
             responses={404: {"description": "Nieznany konektor"}, 409: {"description": "Konektor planowany"}})
def enable(request: Request, connector_id: str, body: EnableIn) -> dict[str, Any]:
    c = _conn_or_404(connector_id)
    _not_planned(c, "włączenie")
    state.set_enabled(_settings(request), c.id, body.enabled)
    _audit(request, "INTEGRATION_ENABLE", {"connector": c.id, "enabled": body.enabled})
    return connector_out(c, _settings(request))


@router.post("/{connector_id}/test", response_model=TestOut, summary="Test połączenia / Test the connection",
             description="Jedno lekkie zapytanie do dostawcy (tylko po kliknięciu, nigdy automatycznie). Offline: "
                         "`ok: false, status: offline` bez żadnego połączenia.\n\nOne cheap request, only on "
                         "explicit request. Offline: `ok:false,status:offline`, nothing is attempted.",
             responses={404: {"description": "Nieznany konektor"},
                        409: {"description": "Konektor planowany lub zarządzany w zakładce Webhooki"}})
def test_connector(request: Request, connector_id: str) -> dict[str, Any]:
    c = _conn_or_404(connector_id)
    _not_planned(c, "test")
    if not _can_act(c):
        raise HTTPException(status_code=409, detail="Ten konektor nie ma testu połączenia (tylko eksport do pliku"
                                                    " lub zarządzanie w zakładce Webhooki).")
    return _run_test(request, c)


@router.post("/{connector_id}/connect", response_model=ConnectOut,
             summary="Połącz jednym kliknięciem / One-click connect",
             description="Zapisuje podane sekrety, testuje połączenie i włącza konektor. Offline: sekrety są "
                         "zapisane, test zwraca `offline`, flaga włączenia jest ustawiona, ale status pokazuje "
                         "`offline` (uruchom z HYDRA_OFFLINE=0).\n\nStore secrets -> test -> enable.",
             responses={404: {"description": "Nieznany konektor"}, 409: {"description": "Konektor planowany"},
                        422: {"description": "Zły sekret"}})
def connect(request: Request, connector_id: str, body: ConnectIn | None = None) -> dict[str, Any]:
    c = _conn_or_404(connector_id)
    _not_planned(c, "połączenie")
    if _managed_elsewhere(c):
        raise HTTPException(status_code=409, detail="Webhooki konfiguruje się w zakładce Webhooki "
                                                    "(/v1/integrations/webhooks).")
    return _connect(request, c, (body.secrets if body else None) or None)


@router.post("/{connector_id}/preview", response_model=PreviewOut, summary="Podgląd na sucho / Dry-run preview",
             description="Dokładnie to, co zostałoby wysłane lub zapisane: metoda, cel, nagłówki (sekrety "
                         "zamaskowane), pola, redakcje i treść (do 200 KB). Nic nie jest wysyłane, działa offline."
                         "\n\nExactly what would be sent: method, destination, masked headers, fields, redactions "
                         "and the payload (truncated to 200 KB). Nothing is sent.",
             responses={404: {"description": "Nieznany konektor, przebieg lub kampania"},
                        409: {"description": "Konektor planowany"}, 422: {"description": "Zły format"}})
def preview(request: Request, connector_id: str, body: PreviewIn | None = None) -> dict[str, Any]:
    c = _conn_or_404(connector_id)
    _not_planned(c, "podgląd")
    body = body or PreviewIn()
    settings = _settings(request)
    if not c.formats:
        return _preview_lookup(request, c, body)
    fmt = body.format or c.formats[0]
    if fmt not in c.formats:
        raise HTTPException(status_code=422, detail=f"Format {fmt} nie jest obsługiwany przez {c.id}: "
                                                    + ", ".join(c.formats))
    try:
        if registry.can_push(c):
            from hydra.integrations.clients import push

            p = push.payload_for(c.id, settings, run_id=body.run_id, campaign_id=body.campaign_id, limit=body.limit)
        else:
            p = exports.build(settings, fmt, run_id=body.run_id, campaign_id=body.campaign_id, limit=body.limit)
    except KeyError as exc:
        raise _key_error(exc) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    method, dest, headers = "GET", "plik", {"Content-Type": p.content_type}
    if registry.can_push(c):
        from hydra.integrations.clients import push

        try:
            req, vals = push.request_for(c.id, p)
            method = req.method
            keys_only = [v for v in vals if not v.lower().startswith(("http://", "https://"))]
            dest = _mask(http.redacted_url(req.url), keys_only) if c.id != "slack" else (
                http.redacted_url(req.url).split("/services/")[0] + "/…" + (secrets.last4(req.url) or ""))
            headers = {k: _mask(v, vals) for k, v in req.headers.items()}
        except push.NotConfigured as exc:
            dest = f"plik (wysyłka nieskonfigurowana: {exc})"
    payload, truncated = _truncate(p.preview)
    return {"connector": c.id, "format": fmt, "method": method, "destination": dest, "headers": headers,
            "content_type": p.content_type, "bytes": len(p.body), "sha256": p.sha256, "fields_sent": p.fields_sent,
            "redactions": p.redactions, "payload": payload, "truncated": truncated}


def _preview_lookup(request: Request, c: Connector, body: PreviewIn) -> dict[str, Any]:
    """Inbound connectors: the requests a run would make (indicator per request), secrets masked."""
    from hydra.integrations import clients

    inds, _rejected = _indicators(request, body.indicators, body.run_id, body.campaign_id)
    reqs: list[dict[str, Any]] = []
    vals = _secret_values(c)
    mod = clients.module(c.id)
    if c.id == "openphish":
        from hydra.integrations.clients import openphish

        reqs = [{"method": "GET", "destination": openphish.FEED_URL, "indicator": None,
                 "note_pl": "Pobranie publicznego pliku; dopasowanie lokalne, wskaźniki nie są wysyłane."}]
    elif mod is not None:
        keys = clients.keys_for(c.id)
        for ind in inds[: body.limit or MAX_RUN_INDICATORS]:
            k = clients.kind_of(ind)
            if k not in mod.KINDS:
                continue
            r = mod.build(clients.normalise(ind, k), k, keys)
            reqs.append({"method": r.method, "destination": _mask(http.redacted_url(r.url), vals), "indicator": ind,
                         "headers": {h: _mask(v, vals) if v else "(brak sekretu)" for h, v in r.headers.items()}})
    else:
        reqs = [{"method": "-", "destination": "lokalnie / istniejący moduł", "indicator": i} for i in inds]
    text = json.dumps(reqs, ensure_ascii=False).encode()
    return {"connector": c.id, "format": "requests", "method": reqs[0]["method"] if reqs else "-",
            "destination": (reqs[0]["destination"] if reqs else "-"), "headers": {}, "content_type":
            "application/json", "bytes": len(text), "sha256": __import__("hashlib").sha256(text).hexdigest(),
            "fields_sent": list(c.data_out), "redactions": ["Klucze API zamaskowane; zapytania bez parametru key."],
            "payload": reqs, "truncated": False}


@router.get("/{connector_id}/export", summary="Pobierz eksport / Download export",
            description="Plik w formacie importu dostawcy (MISP JSON, STIX 2.1, CSV, NDJSON, _bulk, HEC, SQL, "
                        "Cypher, XARF ZIP, pakiet dowodowy). Działa offline; audyt EXPORT + INTEGRATION_EXPORT.\n\n"
                        "Provider import file; works offline; audited.",
            responses={200: {"description": "Plik / File", "content": {"application/json": {}, "text/csv": {},
                                                                     "application/zip": {}}},
                       404: {"description": "Nieznany konektor, przebieg lub kampania"},
                       409: {"description": "Konektor planowany"}, 422: {"description": "Zły format"}})
def export_file(request: Request, connector_id: str,
                format: str | None = Query(None, description="Format z listy `formats` konektora."),  # noqa: A002
                run_id: str | None = None, campaign_id: str | None = None,
                limit: int | None = Query(None, ge=1, le=100_000)) -> Response:
    from hydra.export_log import record_export
    from hydra.export_safe import content_disposition

    c = _conn_or_404(connector_id)
    _not_planned(c, "eksport")
    if not c.formats:
        raise HTTPException(status_code=409, detail=f"Konektor {c.id} nie ma formatu eksportu.")
    fmt = format or c.formats[0]
    if fmt not in c.formats:
        raise HTTPException(status_code=422, detail=f"Format {fmt} nie jest obsługiwany przez {c.id}.")
    settings = _settings(request)
    try:
        if c.id == "slack":
            from hydra.integrations.clients import push

            p = push.payload_for(c.id, settings, run_id=run_id, campaign_id=campaign_id, limit=limit)
        else:
            p = exports.build(settings, fmt, run_id=run_id, campaign_id=campaign_id, limit=limit)
    except KeyError as exc:
        raise _key_error(exc) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    record_export(_app(request).writer, f"integration_{c.id}_{fmt}", run_id, {"bytes": len(p.body)},
                  format=fmt, **({"campaign_id": campaign_id} if campaign_id else {}))
    _audit(request, "INTEGRATION_EXPORT", {"connector": c.id, "format": fmt, "run_id": run_id,
                                           "campaign_id": campaign_id, "bytes": len(p.body), "sha256": p.sha256})
    stem = f"hydra-{c.id}-{campaign_id or run_id or 'latest'}"
    return Response(content=p.body, media_type=p.content_type,
                    headers={**content_disposition(p.filename(stem), f"hydra-export.{fmt}"), "X-Hydra-Sha256":
                             p.sha256})


@router.post("/{connector_id}/run", response_model=RunOut, summary="Uruchom / Run lookups or push",
             description="Konektory live: wyszukiwanie wskaźników (z treści żądania albo do 25 z przebiegu) z "
                         "pamięcią podręczną i odtwarzaniem; konektory z wysyłką: wysłanie ładunku z podglądu. "
                         "Offline: 503 z polskim komunikatem. Wymaga włączenia konektora.\n\nLive lookups (cached, "
                         "replayable) or a push. Offline: 503. Requires the connector to be enabled.",
             responses={404: {"description": "Nieznany konektor, przebieg lub kampania"},
                        409: {"description": "Konektor planowany, wyłączony albo bez akcji"},
                        503: {"description": "Tryb offline", "content": {"application/json": {
                            "example": OFFLINE_EXAMPLE}}}})
def run_connector(request: Request, connector_id: str, body: RunIn | None = None) -> Any:
    c = _conn_or_404(connector_id)
    _not_planned(c, "uruchomienie")
    if not _can_act(c):
        raise HTTPException(status_code=409, detail=f"Konektor {c.id} nie ma akcji uruchomienia (tylko eksport).")
    settings, writer = _settings(request), _app(request).writer
    if registry.needs_network(c) and offline(settings):
        return _offline_response()
    if not state.is_enabled(settings, c.id):
        raise HTTPException(status_code=409, detail=http.MESSAGE_DISABLED_PL)
    body = body or RunIn()
    t0 = time.monotonic()
    out: dict[str, Any] = {"ok": True, "status": "ok", "message_pl": "", "connector": c.id, "results": [],
                           "rejected": [], "sent": None}
    try:
        if registry.can_push(c):
            from hydra.integrations.clients import push

            try:
                out.update(push.push(c.id, settings, writer, run_id=body.run_id, campaign_id=body.campaign_id))
            except KeyError as exc:
                raise _key_error(exc) from None
        elif c.id == "meta-adlibrary":
            from hydra.integrations.clients import meta_adlibrary

            out.update(meta_adlibrary.run(settings, writer))
        else:
            inds, rejected = _indicators(request, body.indicators, body.run_id, body.campaign_id)
            out["rejected"] = rejected
            out["results"] = _lookups(c, inds, settings, writer)
            n = len(out["results"])
            out["message_pl"] = (f"Sprawdzono {n} wskaźników." if n else
                                 "Brak wskaźników obsługiwanych przez ten konektor.")
    except http.IntegrationError as exc:
        if exc.status == "offline":
            return _offline_response()
        out.update(ok=False, status=_map_status(exc.status), message_pl=exc.message_pl)
    out["ms"] = int((time.monotonic() - t0) * 1000)
    out["summary"] = dict(Counter(r["verdict"] for r in out["results"]))
    out["message_pl"] = http.scrub(out["message_pl"], _secret_values(c))
    state.record_run(settings, c.id, {**out, "at": _now()})
    _audit(request, "INTEGRATION_RUN", {"connector": c.id, "ok": out["ok"], "status": out["status"],
                                        "results": len(out["results"]), "summary": out["summary"], "ms": out["ms"]})
    return out


def _lookups(c: Connector, inds: list[str], settings: Any, writer: Any) -> list[dict[str, Any]]:
    from hydra.integrations import clients

    results: list[dict[str, Any]] = []
    if c.id == "openphish":
        from hydra.integrations.clients import openphish

        openphish.refresh(settings, writer)
        urls, hosts, m = openphish.load(settings)
        for i in inds:
            k = clients.kind_of(i)
            if k and (r := openphish.match(clients.normalise(i, k), k, urls, hosts, m)) is not None:
                results.append(r)
        return results
    if c.live_impl == "hydra.integrations.clients.local":
        from hydra.integrations.clients import local

        return [r for i in inds if (r := local.lookup(c.id, i, settings=settings, writer=writer)) is not None]
    from hydra.enrich.client import Enricher

    enr = Enricher(settings=settings, live=True)
    for i in inds:
        r = clients.lookup(c.id, i, settings=settings, writer=writer, enricher=enr)
        if r is not None:
            results.append(r)
    return results


def install_validation_redaction(app: Any) -> None:
    """FastAPI's default 422 body echoes the submitted value (`input`), which for these routers can be an API key,
    a webhook signing secret or a webhook URL. Under /v1/integrations the error keeps only location, type and
    message; other paths keep FastAPI's default body."""
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError

    async def handler(request: Request, exc: RequestValidationError):  # type: ignore[no-untyped-def]
        if not request.url.path.removeprefix("/api").startswith("/v1/integrations"):
            return await request_validation_exception_handler(request, exc)
        errors = [{"loc": list(e.get("loc", ())), "type": e.get("type"), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    app.add_exception_handler(RequestValidationError, handler)
