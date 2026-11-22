"""CERT Polska tail: 416 with an unchanged file is "Brak nowych wpisów", a shrunk/rotated file or a 206 from another
offset resyncs at once, and the source card / "Odpytaj teraz" texts say so in Polish. Live path through
hydra.integrations.http with an injected transport that serves a simulated actions log with real Range semantics
(no network)."""

from __future__ import annotations

import dataclasses
import json
import re

import pytest

from hydra.integrations import http
from hydra.watch.rt import engine as E
from hydra.watch.rt import sources as S
from tests.watch_rt.conftest import count, source_row

LM = "Sun, 22 Nov 2026 03:00:31 GMT"


def lines(hosts: list[str], t: str = "2026-11-22T02:01:55") -> bytes:
    return "".join(json.dumps({"RegisterPositionId": f"r-{h}", "DomainAddress": h, "ActionType": "block",
                               "ActionTime": t}) + "\n" for h in hosts).encode()


class CertFile:
    """Transport stub with Range semantics: 206 + Content-Range for a satisfiable range, 416 `bytes */N` past the
    end. `shift_once` answers the next GET from another start offset (a misbehaving cache)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls: list[tuple[str, str | None]] = []
        self.shift_once: int | None = None

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        rng = headers.get("Range")
        self.calls.append((method, rng))
        n = len(self.data)
        if method == "HEAD":
            return 200, {"Content-Length": str(n), "Last-Modified": LM}, b""
        m = re.match(r"bytes=(\d+)-(\d*)", rng) if rng else None
        start = int(m.group(1)) if m else 0
        end = int(m.group(2)) if m and m.group(2) else None
        if self.shift_once is not None:
            start, self.shift_once = self.shift_once, None
        if start >= n:
            return 416, {"Content-Range": f"bytes */{n}", "Last-Modified": LM}, b""
        last = min(n - 1, end) if end is not None else n - 1
        return 206, {"Content-Range": f"bytes {start}-{last}/{n}", "Last-Modified": LM}, self.data[start:last + 1]


@pytest.fixture
def certfile(public_dns):
    f = CertFile(lines(["a1.example", "a2.example", "a3.example"]))
    prev = http.set_transport(f)
    try:
        yield f
    finally:
        http.set_transport(prev)


def _engine(make_engine, online):
    eng = make_engine(online)
    eng.set_source("cert-pl", enabled=True)
    return eng


def test_content_range_parser():
    assert S.content_range({"content-range": "bytes */31602124"}) == (None, 31602124)
    assert S.content_range({"content-range": "bytes 100-199/31602124"}) == (100, 31602124)
    assert S.content_range({"content-range": "bytes 0-9/*"}) == (0, None)
    assert S.content_range({}) == (None, None)


def test_unchanged_file_is_no_new_entries(make_engine, online, certfile):
    eng = _engine(make_engine, online)
    first = eng.poll_source("cert-pl")
    assert first["new"] == 3 and first["error_detail"] is None
    assert first["result_pl"].startswith("Sprawdzono: 3 nowe wpisy")
    size = len(certfile.data)
    second = eng.poll_source("cert-pl", force=True)
    assert certfile.calls[-1] == ("GET", f"bytes={size}-{size + S.CERT_CHUNK_BYTES - 1}")
    assert second["new"] == 0 and second["not_modified"] and second["error_pl"] is None
    assert second["result_pl"] == f"Sprawdzono: brak nowych wpisów (plik bez zmian, {size / 1e3:.1f} kB)."\
        .replace(".", ",", 1)
    row = source_row(eng, "cert-pl")
    assert row["state"] == "no_new" and row["consecutive_errors"] == 0
    assert row["cursor"]["file_size"] == size and row["cursor"]["server_last_modified"] == LM
    view = eng.status()["sources"][0]
    assert view["state_pl"] == "Brak nowych wpisów"
    assert view["file_size"] == size and view["newest_entry"] == "2026-11-22T02:01:55+00:00"
    assert view["new_24h"] == 3 and len(view["hourly_new"]) == 24 and sum(h["new"] for h in view["hourly_new"]) == 3
    assert view["summary_pl"].startswith("Lista CERT bez nowych wpisów od ")
    assert "ostatni wpis" in view["summary_pl"] and "03:01:55 czasu PL" in view["summary_pl"]
    assert view["summary_pl"].endswith("Plik sprawdzany co minutę.")


def test_shrunk_file_resyncs_immediately_and_idempotently(make_engine, online, certfile):
    eng = _engine(make_engine, online)
    eng.poll_source("cert-pl")
    certfile.data = lines(["a2.example", "b1.example"])  # regenerated shorter than our offset
    n_calls = len(certfile.calls)
    run = eng.poll_source("cert-pl", force=True)
    gets = [c for c in certfile.calls[n_calls:] if c[0] == "GET"]
    assert len(gets) == 2 and gets[1] == ("GET", f"bytes=0-{S.CERT_CHUNK_BYTES - 1}")
    assert run["error_detail"] == S.RESYNC_SHRUNK_PL and run["error_pl"] is None
    assert run["new"] == 1  # b1 is new; a2 is deduplicated by external_id
    assert count(eng, "SELECT COUNT(*) FROM watch_items WHERE source='cert-pl'") == 4
    assert count(eng, "SELECT COUNT(*) FROM events WHERE type='WATCH_SOURCE_RESYNC'") == 1
    row = source_row(eng, "cert-pl")
    assert row["state_detail"] == S.RESYNC_SHRUNK_PL and row["cursor"]["offset"] == len(certfile.data)
    assert S.RESYNC_SHRUNK_PL in run["result_pl"]
    again = eng.poll_source("cert-pl", force=True)  # now in sync: plain 416, no further resync
    assert again["error_detail"] is None and again["new"] == 0
    assert count(eng, "SELECT COUNT(*) FROM events WHERE type='WATCH_SOURCE_RESYNC'") == 1


def test_206_from_another_offset_resyncs(make_engine, online, certfile):
    eng = _engine(make_engine, online)
    eng.poll_source("cert-pl")
    certfile.data += lines(["c1.example"])
    certfile.shift_once = 5  # server answers from byte 5 instead of our offset
    run = eng.poll_source("cert-pl", force=True)
    assert run["error_detail"] == S.RESYNC_SHIFT_PL
    assert [h for h in (r[0] for r in certfile.calls)].count("GET") == 3
    assert run["new"] == 1  # c1 only; the backfilled lines dedup
    assert count(eng, "SELECT COUNT(*) FROM watch_items WHERE source='cert-pl'") == 4


def test_resync_that_keeps_failing_is_an_error(make_engine, online, certfile, monkeypatch):
    eng = _engine(make_engine, online)
    eng.poll_source("cert-pl")

    def always_shifted(method, url, headers, body, timeout, max_bytes):
        n = len(certfile.data)
        return 206, {"Content-Range": f"bytes 7-{n - 1}/{n}"}, certfile.data[7:]

    http.set_transport(always_shifted)
    run = eng.poll_source("cert-pl", force=True)
    assert run["error_status"] == "http" and "resynchronizacja" in run["error_pl"]
    assert source_row(eng, "cert-pl")["consecutive_errors"] == 1


def test_polish_helpers():
    assert E._entries_pl(1) == "1 nowy wpis" and E._entries_pl(3) == "3 nowe wpisy"
    assert E._entries_pl(5) == "5 nowych wpisów" and E._entries_pl(12) == "12 nowych wpisów"
    assert E._entries_pl(22) == "22 nowe wpisy"
    assert E.size_pl(31602124) == "31,6 MB"
    assert E._every_pl(60) == "co minutę" and E._every_pl(3600) == "co 1 h"
    assert E.result_pl("cert-pl", {"new": 0, "not_modified": True}, {"file_size": 31602124}) == \
        "Sprawdzono: brak nowych wpisów (plik bez zmian, 31,6 MB)."


def _spy_on_real_poller(monkeypatch) -> list[int]:
    """Wrap the registered CERT adapter with a counter that delegates to the real sources.poll_cert, so a test proves
    the engine actually ran the production poller (CR-ASTRA-RT-03)."""
    assert S.ADAPTERS["cert-pl"].poll is S.poll_cert
    calls: list[int] = []

    def spy(ctx):
        calls.append(1)
        return S.poll_cert(ctx)

    monkeypatch.setitem(S.ADAPTERS, "cert-pl", dataclasses.replace(S.ADAPTERS["cert-pl"], poll=spy))
    return calls


def test_large_gap_skips_to_the_tail_and_never_exceeds_the_cap(make_engine, online, certfile, monkeypatch):
    """2026-11-22: cursor reset to 9 564 B made `bytes=9564-` return 31.6 MB > 20 MB cap; now bounded + gap skip.
    CR-ASTRA-RT-03: drives the real poller (engine -> sources.poll_cert) over a synthetic backlog > CERT_MAX_GAP_BYTES
    instead of asserting over an empty call list."""
    polled = _spy_on_real_poller(monkeypatch)
    eng = _engine(make_engine, online)
    assert eng.poll_source("cert-pl")["new"] == 3
    start_call = len(certfile.calls)
    old = lines(["synthetic-old.example"])
    certfile.data += old * (S.CERT_MAX_GAP_BYTES // len(old) + 100) + lines(["synthetic-latest.example"])
    run = eng.poll_source("cert-pl", force=True)
    gets = [rng for method, rng in certfile.calls[start_call:] if method == "GET"]
    assert len(gets) == 2  # discover the backlog, then read the 16 KB tail; never the backlog itself
    for rng in gets:
        m = re.fullmatch(r"bytes=(\d+)-(\d+)", rng or "")
        assert m is not None, rng  # every GET is bounded
        lo, hi = map(int, m.groups())
        assert hi - lo + 1 == S.CERT_CHUNK_BYTES
    assert int(re.match(r"bytes=(\d+)-", gets[1]).group(1)) == len(certfile.data) - S.CERT_BACKFILL_BYTES
    assert run["error_pl"] is None and run["error_detail"] == S.RESYNC_GAP_PL
    assert S.RESYNC_GAP_PL in run["result_pl"] and "pominięto starsze wpisy" in run["result_pl"]
    assert run["new"] == 2  # the repeated old id once (dedup by external_id) + the newest entry
    assert count(eng, "SELECT COUNT(*) FROM watch_items WHERE source='cert-pl' AND external_id LIKE "
                      "'%synthetic-latest.example%'") == 1
    row = source_row(eng, "cert-pl")
    assert row["state_detail"] == S.RESYNC_GAP_PL and row["cursor"]["offset"] == len(certfile.data)
    assert eng.status()["sources"][0]["file_size"] == len(certfile.data)
    again = eng.poll_source("cert-pl", force=True)  # caught up: idempotent 416, no second gap skip
    assert again["new"] == 0 and again["not_modified"] and again["error_detail"] is None
    assert len(polled) == 3  # every poll went through the real poller via the engine


def test_server_ignoring_range_reads_only_the_new_bytes(make_engine, online, certfile, monkeypatch):
    """CR-ASTRA-RT-03: a server/proxy that ignores Range answers 200 with the whole file; the poller slices from its
    offset (append-only log), takes only new entries and keeps its position."""
    eng = _engine(make_engine, online)
    eng.poll_source("cert-pl")
    certfile.data += lines(["d1.example"])
    calls_before = len(certfile.calls)

    def ignores_range(method, url, headers, body, timeout, max_bytes):
        certfile.calls.append((method, headers.get("Range")))
        return 200, {"Content-Length": str(len(certfile.data)), "Last-Modified": LM}, certfile.data

    http.set_transport(ignores_range)
    polled = _spy_on_real_poller(monkeypatch)
    run = eng.poll_source("cert-pl", force=True)
    assert polled == [1]
    assert [c for c in certfile.calls[calls_before:] if c[0] == "GET"][0][1].startswith("bytes=")
    assert run["error_pl"] is None and run["new"] == 1
    assert count(eng, "SELECT COUNT(*) FROM watch_items WHERE source='cert-pl'") == 4
    assert source_row(eng, "cert-pl")["cursor"]["offset"] == len(certfile.data)
