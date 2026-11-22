from __future__ import annotations

import json

from hydra.watch import recorder


def test_cert_tail_range_resume(tmp_path, monkeypatch):
    ts = "2026-11-21T10:00:00+00:00"
    lines = [json.dumps({"RegisterPositionId": i, "DomainAddress": f"d{i}.com", "ActionTime": ts,
                         "ActionType": "block"}) for i in range(5)]
    body = ("\n".join(lines[:3]) + "\n").encode()
    state = {"body": body}

    def fake(url, *, start=None, head=False):
        b = state["body"]
        if head:
            return 200, {"Content-Length": str(len(b))}, b""
        if start is not None and start >= len(b):
            return 416, {}, b""
        return 206, {"ETag": "x"}, b[start or 0:]

    monkeypatch.setattr(recorder, "_request", fake)
    tail = recorder.CertTail(tmp_path)          # no backfill: start at the end
    assert tail.poll() == 0
    state["body"] = body + (lines[3] + "\n" + lines[4]).encode()  # last line without newline
    assert tail.poll() == 2
    tail2 = recorder.CertTail(tmp_path)          # restart resumes from the persisted offset
    assert tail2.poll() == 0
    out = list(tmp_path.glob("certpl-2*.jsonl"))
    assert len(out) == 1
    recs = [json.loads(x) for x in out[0].read_text().splitlines()]
    assert [r["raw"]["RegisterPositionId"] for r in recs] == [3, 4] and recs[0]["source"] == "certpl"


def test_backfill_goes_to_separate_file(tmp_path, monkeypatch):
    lines = [json.dumps({"RegisterPositionId": i, "DomainAddress": f"d{i}.com"}) for i in range(4)]
    body = ("\n".join(lines) + "\n").encode()
    monkeypatch.setattr(recorder, "_request", lambda url, *, start=None, head=False: (
        (200, {"Content-Length": str(len(body))}, b"") if head else
        ((416, {}, b"") if (start or 0) >= len(body) else (206, {}, body[start or 0:]))))
    tail = recorder.CertTail(tmp_path, backfill_bytes=len(lines[3]) + len(lines[2]) + 4)
    n = tail.poll()
    assert n >= 1
    assert list(tmp_path.glob("certpl-backfill-*.jsonl")) and not list(tmp_path.glob("certpl-2*.jsonl"))


def test_ct_filter_keeps_candidates_once_per_hour():
    from hydra.watch.ct_recorder import CtFilter
    from hydra.watch.events import normalise

    f = CtFilter(min_bits=6.0)
    kept = f.keep(["*.allegro.zakup-oferta123456.sbs", "allegro.zakup-oferta123456.sbs", "cdn.example-shop.com",
                   "olx.zakup-oferta123456.sbs"])
    assert kept == ["allegro.zakup-oferta123456.sbs"]  # one per registrable domain per hour; benign dropped
    assert f.names == 3 and f.kept == 1
    evs = normalise({"rec_ts": "2026-11-21T15:00:00Z", "source": "ct",
                     "raw": {"names": kept, "seen": 1763737200.0}})
    assert evs[0].source == "watch:ct" and evs[0].host == kept[0]
