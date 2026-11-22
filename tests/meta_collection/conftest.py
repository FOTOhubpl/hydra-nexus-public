"""META-COLLECT fixtures: temporary state, a fake Graph API behind `hydra.integrations.http.set_transport` (so every
request runs the real path: offline gate, budget, netguard, audit, scrubbing), fake public DNS, a test token in an
isolated HYDRA_SECRETS_DIR. No real network (root conftest socket guard), no organiser data: every value is synthetic
(.example / .test, RFC 2606). Everything here is SYNTHETIC test data, never a live Meta result."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from hydra.config import Settings
from hydra.db import Writer, connect
from hydra.integrations import http
from hydra.integrations import state as istate

TOKEN = "EAAmetaTESTtoken0123456789abcdefXYZ"
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make_settings(tmp_path: Path, *, offline: bool = False) -> Settings:
    s = Settings(root=tmp_path, data_dir=tmp_path / "data", models_dir=tmp_path / "models",
                 cache_dir=tmp_path / "cache", state_dir=tmp_path / "state", offline=offline, max_file_mb=5)
    s.ensure_dirs()
    return s


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HYDRA_SECRETS_DIR", str(home / "secrets"))
    for k in ("META_AD_LIBRARY_TOKEN", "HYDRA_META_TOKEN", "HYDRA_WATCH_RECORD", "HYDRA_META_GRAPH_VERSION",
              "HYDRA_WATCH_RT", "HYDRA_WATCH_AUTO_ASSIGN", "HYDRA_META_EXTRACTORS", "HYDRA_META_SNAPSHOTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("hydra.meta.media.SNAPSHOT_MIN_INTERVAL_S", 0.0)  # throttle tested explicitly


@pytest.fixture
def online(tmp_path: Path) -> Settings:
    s = make_settings(tmp_path)
    istate.set_enabled(s, "meta-adlibrary", True)
    return s


@pytest.fixture
def token(monkeypatch) -> str:
    monkeypatch.setenv("META_AD_LIBRARY_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def writer(online: Settings) -> Iterator[Writer]:
    w = Writer(online.db_path)
    yield w
    w.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(autouse=True)
def public_dns(monkeypatch) -> None:
    def fake(host, port, *a, **k):
        if "internal" in str(host):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr("hydra.netguard.socket.getaddrinfo", fake)


def ad(n: int, *, body: str | None = None, page: str = "100000000000001", title: str = "Platforma",
       **extra: Any) -> dict:
    row = {"id": f"8000000000{n:03d}", "page_id": page, "page_name": "Fikcyjna Strona (SYNTETYCZNE)",
           "ad_creation_time": "2026-11-20T10:00:00+0000",
           "ad_creative_bodies": [body if body is not None else f"Oferta {n}: https://lure-{n}.example/start"],
           "ad_creative_link_titles": [title], "ad_creative_link_captions": [f"lure-{n}.example"],
           "ad_creative_link_descriptions": [], "ad_delivery_start_time": "2026-11-20",
           "ad_snapshot_url": f"https://www.facebook.com/ads/archive/render_ad/?id={n}&access_token={TOKEN}",
           "publisher_platforms": ["facebook"], "languages": ["pl"]}
    row.update(extra)
    return row


class Graph:
    """Fake ads_archive (and snapshot / CDN) answering through http.request's transport hook.

    pages[term] = list of row lists (page i answers after=C<i>); fail[(term, i)] = [(status, body, headers), ...]
    makes that page fail once per entry; reject_fields = field names answered with Graph code 100."""

    def __init__(self) -> None:
        self.pages: dict[str, list[list[dict]]] = {}
        self.fail: dict[tuple[str, int], list[tuple[int, dict, dict]]] = {}
        self.reject_fields: set[str] = set()
        self.calls: list[dict[str, Any]] = []
        self.raw: dict[str, tuple[int, dict, bytes]] = {}
        self.token_error = False
        self.headers: dict[str, str] = {}

    def graph_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c.get("_path", "").endswith("/ads_archive")]

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        sp = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(sp.query).items()}
        self.calls.append({**q, "_path": sp.path, "_host": sp.hostname, "_url": url})
        for pre, resp in self.raw.items():
            if url.startswith(pre):
                return resp
        if not sp.path.endswith("/ads_archive"):
            return 404, {}, b""
        if self.token_error:
            return 400, {}, json.dumps({"error": {"code": 190, "message": f"Invalid token {q.get('access_token')}"}
                                        }).encode()
        if set((q.get("fields") or "").split(",")) & self.reject_fields:
            return 400, {}, json.dumps({"error": {"code": 100, "message": "(#100) nonexisting field"}}).encode()
        term = q.get("search_terms") or ("pages:" + q.get("search_page_ids", ""))
        i = int(q["after"][1:]) if q.get("after") else 0
        f = self.fail.get((term, i))
        if f:
            st, b, h = f.pop(0)
            if not f:
                self.fail.pop((term, i))
            return st, h, json.dumps(b).encode()
        pages = self.pages.get(term, [[]])
        rows = pages[i] if i < len(pages) else []
        if q.get("limit") == "1":
            rows = rows[:1]
        doc: dict[str, Any] = {"data": rows}
        if i + 1 < len(pages):
            doc["paging"] = {"cursors": {"after": f"C{i + 1}"},
                             "next": f"https://graph.facebook.com/v26.0/ads_archive?after=C{i + 1}&access_token="
                                     f"{q.get('access_token')}"}
        return 200, dict(self.headers), json.dumps(doc).encode()


@pytest.fixture
def graph() -> Iterator[Graph]:
    g = Graph()
    prev = http.set_transport(g)
    try:
        yield g
    finally:
        http.set_transport(prev)


def configure(writer: Writer, *queries: str, **cfg: Any) -> dict:
    from hydra.meta import store

    patch = {"queries": [{"kind": "keyword", "value": q} for q in queries], **cfg}
    return writer.run(lambda c: store.put_config(c, patch, T0))


def count(settings: Settings, sql: str, *args: Any) -> int:
    with closing(connect(settings.db_path, readonly=True)) as conn:
        return int(conn.execute(sql, args).fetchone()[0] or 0)


def rows(settings: Settings, sql: str, *args: Any) -> list[tuple]:
    with closing(connect(settings.db_path, readonly=True)) as conn:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]


def db_text(settings: Settings) -> str:
    """Every text value of the DB (proves the token never lands anywhere)."""
    out = []
    with closing(connect(settings.db_path, readonly=True)) as conn:
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for r in conn.execute(f"SELECT * FROM {t}"):
                out.extend(str(x) for x in r if isinstance(x, str))
    return "\n".join(out)
