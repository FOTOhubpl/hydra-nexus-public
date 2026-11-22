"""Layered diagnostics of the importer contract (spec §3), through the shared transport and budget:

1. token accepted; 2. `ad_reached_countries=["PL"]`, `ad_type=ALL` answered; 3. the full minimal importer field set
accepted; 4. each optional (EU) field recognised as available / unavailable; 5. one returned record passes
normalisation and storage (live data space). An empty result with HTTP 200 only proves the query is valid (layer 5 =
`skipped`, never `pass`). At most 3 + len(OPTIONAL_FIELDS) requests.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import closing
from typing import Any

from hydra.db import connect
from hydra.meta import contract, normalize, store
from hydra.meta.transport import CONFIG_STATES, MetaError, MetaTransport


def _layer(lid: str, status: str, message: str) -> dict[str, Any]:
    return {"id": lid, "label_pl": contract.LAYERS_PL[lid], "status": status, "message_pl": message}


def run(settings: Any, writer: Any, *, fetch: Callable | None = None, clock: Callable[[], float] = time.time
        ) -> dict[str, Any]:
    writer.run(store.ensure_schema)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        cfg = store.config(conn)
    term = next((q["value"] for q in cfg["queries"] if q["kind"] == "keyword" and q["enabled"]), "inwestycja")
    tr = MetaTransport(settings, writer, fetch=fetch, budget_per_h=int(cfg["budget_per_h"]), clock=clock,
                       require_enabled=False)  # explicit operator action, like the Integrations "Test" button
    base = {"ad_reached_countries": json.dumps(cfg["countries"] or ["PL"]), "ad_type": contract.AD_TYPE,
            "ad_active_status": "ALL", "search_terms": term, "limit": "1"}
    layers: list[dict[str, Any]] = []
    optional: dict[str, str] = {f: "unknown" for f in contract.OPTIONAL_FIELDS}
    out: dict[str, Any] = {"ok": False, "at": store.iso(clock()), "graph_api_version": contract.graph_version(),
                           "countries": cfg["countries"], "search_terms": term, "layers": layers,
                           "optional_fields": optional, "mode": "live"}

    def finish(rest_from: int, msg: str) -> dict[str, Any]:
        ids = list(contract.LAYERS_PL)
        for lid in ids[rest_from:]:
            if not any(x["id"] == lid for x in layers):
                layers.append(_layer(lid, "not_run", msg))
        out["requests"] = tr.requests
        writer.run(lambda c: store.set_state(c, "diagnostics", out, clock()))
        return out

    try:
        tr.preflight()
    except MetaError as e:
        layers.append(_layer("token", "fail" if e.status in CONFIG_STATES else "not_run", e.message_pl))
        return finish(1, "Nie wykonano: " + e.message_pl)
    # layers 1 + 2
    try:
        tr.get({**base, "fields": "id"})
    except MetaError as e:
        if e.status in CONFIG_STATES:
            layers.append(_layer("token", "fail", e.message_pl))
            return finish(1, "Nie wykonano: token nie został przyjęty.")
        layers.append(_layer("token", "pass" if e.status == "params" else "not_run",
                             "Token przyjęty." if e.status == "params" else e.message_pl))
        layers.append(_layer("query_pl_all", "fail" if e.status == "params" else "not_run", e.message_pl))
        return finish(2, "Nie wykonano po błędzie zapytania podstawowego.")
    layers.append(_layer("token", "pass", "Token przyjęty (odpowiedź HTTP 200)."))
    layers.append(_layer("query_pl_all", "pass", f"Zapytanie {cfg['countries']}, ad_type=ALL obsłużone."))
    # layer 3
    try:
        resp = tr.get({**base, "fields": ",".join(contract.REQUIRED_FIELDS)})
    except MetaError as e:
        layers.append(_layer("required_fields", "fail",
                             "Import niesprawny: Meta odrzuciła minimalny zestaw pól importera. " + e.message_pl))
        return finish(3, "Nie wykonano: minimalny kontrakt odrzucony.")
    rows = [r for r in resp.data.get("data") or [] if isinstance(r, dict) and r.get("id")]
    layers.append(_layer("required_fields", "pass", f"Pola importera przyjęte ({len(contract.REQUIRED_FIELDS)})."))
    # layer 4
    try:
        r4 = tr.get({**base, "fields": ",".join([*contract.REQUIRED_FIELDS, *contract.OPTIONAL_FIELDS])})
        row4 = next((r for r in r4.data.get("data") or [] if isinstance(r, dict)), None)
        for f in contract.OPTIONAL_FIELDS:
            optional[f] = "available"  # accepted by the API; per-ad presence is recorded at import (unknown)
        if row4 is not None:
            rows = rows or [row4]
        layers.append(_layer("optional_fields", "pass", "Pola opcjonalne przyjęte przez API (obecność zależy od "
                                                        "reklamy; brak wartości = nieznane)."))
    except MetaError as e:
        if e.status not in ("params", "transient", "error"):
            layers.append(_layer("optional_fields", "not_run", e.message_pl))
            return finish(4, "Nie wykonano: " + e.message_pl)
        for f in contract.OPTIONAL_FIELDS:
            try:
                tr.get({**base, "fields": f"id,{f}"})
                optional[f] = "available"
            except MetaError as e2:
                if e2.status in ("budget", "rate_limited", "offline") or e2.status in CONFIG_STATES:
                    break
                optional[f] = "unavailable"
        n_ok = sum(1 for v in optional.values() if v == "available")
        layers.append(_layer("optional_fields", "pass", f"Rozpoznano pola opcjonalne: {n_ok} dostępne, "
                                                        f"{sum(1 for v in optional.values() if v == 'unavailable')} "
                                                        "niedostępne (import działa bez nich)."))
    # layer 5
    if not rows:
        layers.append(_layer("normalise_store", "skipped",
                             "Wyszukiwanie nie zwróciło rekordu: HTTP 200 potwierdza tylko poprawność zapytania, nie "
                             "pełne działanie importu."))
    else:
        ts = store.iso(clock())
        req_opt = [f for f, v in optional.items() if v == "available"]
        norm = [normalize.normalise(rows[0], observed_at=ts, countries=cfg["countries"],
                                    graph_version=contract.graph_version(), requested_optional=req_opt)]

        def tx(c: Any) -> dict[str, Any]:
            pid = store.start_poll(c, "live", None, "diagnose", clock())
            o = store.commit_page(c, poll_id=pid, mode="live", query_id_=None, config_version=int(
                cfg["config_version"]), page=0, normalized=norm, now=clock())
            store.finish_poll(c, pid, clock(), status="ok", requests=tr.requests)
            return o

        try:
            o = writer.run(tx)
            layers.append(_layer("normalise_store", "pass",
                                 f"Rekord {norm[0]['creative']['source_ad_id']} znormalizowany i zapisany "
                                 f"({'nowa rewizja' if o['new_revisions'] else 'bez zmian'})."))
        except Exception as exc:  # noqa: BLE001
            layers.append(_layer("normalise_store", "fail", f"Zapis nieudany: {type(exc).__name__}"))
    out["ok"] = all(x["status"] == "pass" for x in layers if x["id"] in ("token", "query_pl_all", "required_fields"))
    return finish(5, "")
