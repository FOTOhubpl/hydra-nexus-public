"""Outbound webhooks API (`/v1/integrations/webhooks`, tag "webhooks"). Logic: hydra.integrations.webhooks.

Signed (HMAC-SHA256) JSON deliveries of HYDRA events (delta, job, watch, review, export, ping) to operator-configured
endpoints, with retry/backoff/dead letter, a delivery log and a built-in loopback receiver (`/sink`) so a complete
signed delivery can be demonstrated offline. The signing secret is write-only: responses carry `{configured, last4}`
and the URL only redacted.
"""

from __future__ import annotations

import threading
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from hydra.integrations import webhooks as wh
from hydra.integrations.secrets import SecretError

router = APIRouter(prefix="/v1/integrations/webhooks", tags=["webhooks"])
_LOCK = threading.Lock()

EventId = Literal["ping", "delta", "job", "watch", "review", "export"]
DeliveryStatus = Literal["pending", "delivered", "retrying", "dead", "blocked_offline"]


def manager(request: Request) -> wh.WebhookManager:
    return _manager_for(request.app)


def start_if_configured(app: Any) -> None:
    """App startup: resume the dispatcher when webhooks.json exists, so enabled endpoints get events after a
    restart without waiting for a first webhooks API call. No config file = nothing started."""
    if wh.config_path().is_file():
        _manager_for(app)


def _manager_for(app: Any) -> wh.WebhookManager:
    with _LOCK:
        m = getattr(app.state, "webhooks", None)
        if m is None:
            st = app.state.hydra
            m = wh.WebhookManager(st.settings, st.writer, watch_getter=lambda: getattr(app.state, "watch", None))
            app.state.webhooks = m
            m.sync_dispatcher()
        return m


# ------------------------------------------------------------------------------------------------ models

_SECRET_EX = "wSx9c2V0LWJ5LXRoZS1jbGllbnQtZ2VuZXJ1ago"


class SecretStatus(BaseModel):
    configured: bool = Field(description="Czy klucz podpisu jest zapisany (0600). / Whether a signing key is stored.")
    last4: str | None = Field(description="Ostatnie 4 znaki klucza (tylko dla kluczy >= 12 znaków). / Last 4 chars.")


class Delivery(BaseModel):
    id: str = Field(examples=["dlv-5e0c1a9b3f2d4e61"])
    endpoint_id: str = Field(examples=["wh-3f9a1c2b7d"])
    event: EventId
    status: DeliveryStatus = Field(description="pending | delivered | retrying | dead (dead letter) | "
                                               "blocked_offline (zablokowane polityką sieci)")
    attempts: int = Field(examples=[1])
    max_attempts: int = Field(examples=[wh.MAX_ATTEMPTS])
    last_status_code: int | None = Field(examples=[200])
    last_error: str | None = Field(description="Komunikat po polsku (bez adresu i klucza). / Polish error message.")
    next_retry_at: str | None = Field(description="ISO 8601 UTC kolejnej próby (status retrying).")
    payload_sha256: str
    created_at: str
    delivered_at: str | None


class Endpoint(BaseModel):
    id: str = Field(examples=["wh-3f9a1c2b7d"])
    name: str = Field(examples=["SIEM zespołu CERT"])
    url_redacted: str = Field(description="scheme://host/… + ostatnie 4 znaki ścieżki; pełny adres nigdy nie jest "
                                          "zwracany. / Redacted URL.", examples=["https://hooks.example.org/…a1b2"])
    events: list[EventId]
    enabled: bool
    loopback: bool = Field(description="Adres pętli zwrotnej (127.0.0.1/::1/localhost): dozwolony także offline.")
    secret: SecretStatus
    created_at: str
    last_delivery: Delivery | None


class EventType(BaseModel):
    id: EventId
    label_pl: str
    description_pl: str
    example: dict[str, Any] = Field(description="Przykładowe pole `data` ciała żądania.")


class SignatureInfo(BaseModel):
    header: str = "X-Hydra-Signature"
    algo: str = "HMAC-SHA256"
    scheme_pl: str
    headers: list[str] = Field(description="Nagłówki każdego żądania.")
    tolerance_s: int = Field(description="Okno czasowe znacznika (anty-replay).")


class SinkInfo(BaseModel):
    path: str = Field(description="Ścieżka wbudowanego odbiornika testowego (dopisz ?endpoint_id=<id>).")
    note_pl: str


class WebhooksOverview(BaseModel):
    delivery_possible: bool = Field(description="Czy wysyłka poza ten komputer jest możliwa (HYDRA_OFFLINE=0).")
    offline: bool
    event_types: list[EventType]
    endpoints: list[Endpoint]
    signature: SignatureInfo
    sink: SinkInfo
    dispatcher_running: bool = Field(description="Czy wątek dyspozytora zdarzeń działa (tylko gdy jest włączony "
                                                 "co najmniej jeden punkt końcowy).")


class EndpointCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "name": "Odbiornik testowy (loopback)",
        "url": "http://127.0.0.1:8000/api/v1/integrations/webhooks/sink?endpoint_id=<id>",
        "events": ["ping", "delta", "job"], "enabled": True, "secret": _SECRET_EX}]})
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=2048, description="http(s) URL odbiorcy; przechowywany w "
                     "~/.config/hydra/webhooks.json (0600), zwracany tylko w postaci zredagowanej.")
    events: list[EventId] = Field(default_factory=lambda: ["delta"])
    enabled: bool = False
    secret: str = Field(min_length=wh.MIN_SECRET_LEN, max_length=wh.MAX_SECRET_LEN,
                        description="Klucz podpisu HMAC generowany po stronie klienta; serwer nigdy go nie zwraca.")


class EndpointPatch(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"enabled": True, "events": ["delta", "watch"]}]})
    name: str | None = Field(None, min_length=1, max_length=80)
    url: str | None = Field(None, min_length=8, max_length=2048)
    events: list[EventId] | None = None
    enabled: bool | None = None
    secret: str | None = Field(None, min_length=wh.MIN_SECRET_LEN, max_length=wh.MAX_SECRET_LEN)


class Deleted(BaseModel):
    deleted: bool = True


class DeliveryList(BaseModel):
    deliveries: list[Delivery]


class SinkReceipt(BaseModel):
    at: str
    endpoint_id: str | None
    event: str | None
    delivery_id: str | None
    signature_ok: bool
    bytes: int


class SinkReceipts(BaseModel):
    receipts: list[SinkReceipt]


class SinkAck(BaseModel):
    ok: bool
    signature_ok: bool
    receipt: SinkReceipt


# ------------------------------------------------------------------------------------------------ helpers


def _endpoint_out(m: wh.WebhookManager, ep: dict[str, Any]) -> Endpoint:
    return Endpoint(id=ep["id"], name=ep["name"], url_redacted=wh.redact_url(ep["url"]),
                    events=[e for e in ep.get("events", []) if e in wh.EVENT_IDS], enabled=bool(ep.get("enabled")),
                    loopback=wh.is_loopback_url(ep["url"]), secret=SecretStatus(**wh.EndpointStore.secret_status(
                        ep["id"])), created_at=ep["created_at"], last_delivery=m.last_delivery(ep["id"]))


def _bad(exc: Exception) -> HTTPException:
    msg = str(exc) if isinstance(exc, wh.WebhookError | SecretError) else "Nieprawidłowe dane."
    return HTTPException(status_code=422, detail=msg)


_NOT_FOUND = "Nie znaleziono punktu końcowego webhooka."


# ------------------------------------------------------------------------------------------------ routes


@router.get("", response_model=WebhooksOverview,
            summary="Webhooki: przegląd / Webhooks overview",
            description="Typy zdarzeń (z przykładowym ciałem), skonfigurowane punkty końcowe (adres zredagowany, "
                        "klucz tylko {configured, last4}), schemat podpisu i stan trybu offline.\n\n"
                        "Event catalogue with example payloads, configured endpoints (redacted URL, secret status "
                        "only), signature scheme and offline state.")
def overview(request: Request) -> WebhooksOverview:
    m = manager(request)
    offline = m.offline()
    return WebhooksOverview(
        delivery_possible=not offline, offline=offline,
        event_types=[EventType(**e) for e in wh.EVENT_TYPES],
        endpoints=[_endpoint_out(m, e) for e in m.store.list()],
        signature=SignatureInfo(scheme_pl=wh.SCHEME_PL, tolerance_s=wh.SIGNATURE_TOLERANCE_S,
                                headers=["X-Hydra-Event", "X-Hydra-Delivery", "X-Hydra-Timestamp",
                                         "X-Hydra-Signature"]),
        sink=SinkInfo(path="/v1/integrations/webhooks/sink",
                      note_pl="Wbudowany odbiornik pętli zwrotnej: weryfikuje podpis i zapamiętuje 50 ostatnich "
                              "odbiorów. Adres: http://127.0.0.1:<port>/api/v1/integrations/webhooks/sink"
                              "?endpoint_id=<id>. Dane nie opuszczają komputera."),
        dispatcher_running=m.dispatcher_running())


@router.post("", response_model=Endpoint, status_code=201,
             summary="Dodaj punkt końcowy / Create endpoint",
             description="Zapisuje punkt końcowy w ~/.config/hydra/webhooks.json (0600) i klucz podpisu w "
                         "webhook-<id>.env (0600). Klucz nie jest nigdy zwracany. Włączenie punktu uruchamia "
                         "dyspozytora zdarzeń.\n\nStores the endpoint (0600) and its signing key (0600, write-only).",
             responses={422: {"description": "Nieprawidłowe dane (URL, zdarzenia, klucz)."}})
def create_endpoint(request: Request, body: EndpointCreate) -> Endpoint:
    m = manager(request)
    try:
        ep = m.store.create(body.name, body.url, list(body.events), body.enabled, body.secret)
    except (wh.WebhookError, SecretError) as exc:
        raise _bad(exc) from None
    m.sync_dispatcher()
    return _endpoint_out(m, ep)


@router.get("/deliveries", response_model=DeliveryList,
            summary="Dziennik dostarczeń / Delivery log",
            description="Ostatnie dostarczenia (najnowsze pierwsze), opcjonalnie dla jednego punktu końcowego. Bez "
                        "treści i bez klucza.\n\nRecent deliveries, newest first; no body, no secret.")
def list_deliveries(request: Request, endpoint_id: str | None = Query(None, max_length=40),
                    limit: int = Query(100, ge=1, le=1000)) -> DeliveryList:
    return DeliveryList(deliveries=manager(request).deliveries(endpoint_id, limit))


@router.post("/deliveries/{delivery_id}/retry", response_model=Delivery,
             summary="Ponów dostarczenie / Retry a delivery",
             description="Przywraca dostarczenie z dead letter (lub zablokowane) do kolejki: licznik prób od zera, "
                         "pierwsza próba od razu.\n\nRequeues a dead-letter or blocked delivery (attempts reset).",
             responses={404: {"description": "Nie znaleziono dostarczenia."},
                        409: {"description": "Dostarczenie już zakończone sukcesem lub w trakcie wysyłki."}})
def retry_delivery(request: Request, delivery_id: str) -> Delivery:
    try:
        return Delivery(**manager(request).retry(delivery_id))
    except wh.NotFound:
        raise HTTPException(404, "Nie znaleziono dostarczenia.") from None
    except wh.Conflict as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/sink", response_model=SinkAck,
             summary="Odbiornik testowy (loopback) / Built-in test receiver",
             description="Wbudowany odbiornik: weryfikuje podpis HMAC kluczem punktu wskazanego w ?endpoint_id= "
                         "(czas stały, okno 300 s) i zapisuje potwierdzenie w pamięci (50 ostatnich). Zły podpis: "
                         "401.\n\nVerifies the HMAC signature for ?endpoint_id= and keeps the last 50 receipts.",
             responses={401: {"description": "Nieprawidłowy podpis lub znacznik czasu."},
                        413: {"description": "Ciało większe niż 1 MB."}})
async def sink(request: Request, endpoint_id: str | None = Query(None, max_length=40)) -> Any:
    raw = await request.body()
    if len(raw) > wh.SINK_BODY_CAP:
        raise HTTPException(413, "Ciało żądania przekracza 1 MB.")
    rec = manager(request).sink_receive(endpoint_id, {k.lower(): v for k, v in request.headers.items()}, raw)
    ack = SinkAck(ok=rec["signature_ok"], signature_ok=rec["signature_ok"], receipt=SinkReceipt(**rec))
    return JSONResponse(ack.model_dump(), status_code=200 if rec["signature_ok"] else 401)


@router.get("/sink", response_model=SinkReceipts,
            summary="Odbiory odbiornika testowego / Test receiver receipts",
            description="50 ostatnich żądań odebranych przez wbudowany odbiornik (bez treści).\n\n"
                        "Last 50 receipts of the built-in receiver (no body).")
def sink_receipts(request: Request) -> SinkReceipts:
    return SinkReceipts(receipts=manager(request).sink_receipts())


@router.patch("/{endpoint_id}", response_model=Endpoint,
              summary="Zmień punkt końcowy / Update endpoint",
              description="Częściowa zmiana: nazwa, adres, zdarzenia, włączenie, nowy klucz (zastępuje stary).\n\n"
                          "Partial update; a new secret replaces the old one.",
              responses={404: {"description": _NOT_FOUND}, 422: {"description": "Nieprawidłowe dane."}})
def patch_endpoint(request: Request, endpoint_id: str, body: EndpointPatch) -> Endpoint:
    m = manager(request)
    try:
        ep = m.store.update(endpoint_id, body.model_dump(exclude_unset=True))
    except wh.NotFound:
        raise HTTPException(404, _NOT_FOUND) from None
    except (wh.WebhookError, SecretError) as exc:
        raise _bad(exc) from None
    m.sync_dispatcher()
    return _endpoint_out(m, ep)


@router.delete("/{endpoint_id}", response_model=Deleted,
               summary="Usuń punkt końcowy / Delete endpoint",
               description="Usuwa konfigurację i plik klucza. Dziennik dostarczeń zostaje.\n\n"
                           "Removes the config and the key file; the delivery log is kept.",
               responses={404: {"description": _NOT_FOUND}})
def delete_endpoint(request: Request, endpoint_id: str) -> Deleted:
    m = manager(request)
    try:
        m.store.delete(endpoint_id)
    except wh.NotFound:
        raise HTTPException(404, _NOT_FOUND) from None
    m.sync_dispatcher()
    return Deleted()


@router.post("/{endpoint_id}/test", response_model=Delivery,
             summary="Wyślij ping / Send a test ping",
             description="Wysyła podpisane zdarzenie `ping` (pierwsza próba synchronicznie, kolejne w tle). Adres "
                         "spoza tego komputera w trybie offline: status blocked_offline, bez połączenia.\n\n"
                         "Sends a signed `ping`; offline + non-loopback URL -> blocked_offline, no connection.",
             responses={404: {"description": _NOT_FOUND}})
def test_endpoint(request: Request, endpoint_id: str) -> Delivery:
    try:
        return Delivery(**manager(request).send_test(endpoint_id))
    except wh.NotFound:
        raise HTTPException(404, _NOT_FOUND) from None
