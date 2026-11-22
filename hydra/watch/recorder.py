"""HYDRA Watch recorder: tail public scam-domain feeds into append-only JSONL.

Stdlib only so it can run as a standalone process (``python3 -m hydra.watch.recorder`` or
``python3 hydra/watch/recorder.py``) on the server, independent of the app.

Source: CERT Polska Warning List ``actions_<year>.log`` (append-only JSONL, regenerated about every
5 minutes, HTTP Range supported). Licence: "any use, including commercial" (no endorsement implied).

Each recorded line::

    {"rec_ts": "...Z", "source": "certpl", "raw": {...original action...}}

The byte offset is persisted next to the recording so a restart resumes without duplicates.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

CERT_URL = "https://hole.cert.pl/domains/v2/actions_{year}.log"
UA = "HYDRA-Watch/0.1 (HackNation 2026 research recorder; polite polling)"
_STOP = False


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _log(msg: str) -> None:
    print(f"{_now()} {msg}", flush=True)


def offline() -> bool:
    """HYDRA_OFFLINE=1 is the hard network kill switch (audit A09). Stdlib-only, so read from the environment.
    In the API process the in-process tail is gated by `live_enabled` (hydra.watch.engine) as well."""
    return os.environ.get("HYDRA_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def _request(url: str, *, start: int | None = None, head: bool = False) -> tuple[int, dict, bytes]:
    if offline():
        return 0, {}, b""  # no request: poll() logs the non-200 status and records nothing
    from hydra.netguard import require_online

    # env unset falls back to Settings.offline (default on): the shared kill switch still refuses (CR-ASTRA-SEC-8)
    require_online("watch recorder download (set HYDRA_OFFLINE=0 to record)")
    req = urllib.request.Request(url, method="HEAD" if head else "GET")
    req.add_header("User-Agent", UA)
    if start is not None:
        req.add_header("Range", f"bytes={start}-")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed https URL)
            return resp.status, dict(resp.headers), (b"" if head else resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), b""


class CertTail:
    """Range-tail of the CERT Polska actions log with a persisted byte offset."""

    def __init__(self, out_dir: Path, *, backfill_bytes: int = 0) -> None:
        self.out_dir = out_dir
        self.state_path = out_dir / "certpl.offset.json"
        self.backfill_bytes = backfill_bytes
        self.state = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.state_path)

    def poll(self) -> int:
        year = datetime.now(UTC).year
        url = CERT_URL.format(year=year)
        if self.state.get("url") != url:
            status, headers, _ = _request(url, head=True)
            if status != 200:
                _log(f"certpl HEAD {status}")
                return 0
            size = int(headers.get("Content-Length") or headers.get("content-length") or 0)
            start = max(0, size - self.backfill_bytes)
            # Without backfill the offset is at a line boundary already (file end).
            self.state = {"url": url, "offset": start, "partial": "", "aligned": start in (0, size)}
            self._save()
            _log(f"certpl start url={url} size={size} offset={self.state['offset']}")
        offset = int(self.state["offset"])
        status, headers, body = _request(url, start=offset)
        if status == 416:  # nothing new
            return 0
        if status == 200 and offset > 0:
            # Server ignored Range (file rotated/regenerated from scratch): skip to the end.
            _log("certpl: server returned full body, re-syncing offset to end")
            self.state["offset"] = len(body)
            self.state["partial"] = ""
            self._save()
            return 0
        if status not in (200, 206):
            _log(f"certpl GET {status}")
            return 0
        text = self.state.get("partial", "") + body.decode("utf-8", "replace")
        lines = text.split("\n")
        partial = lines.pop()  # trailing line without newline (may be complete JSON)
        if partial.strip():
            try:
                json.loads(partial)
                lines.append(partial)
                partial = ""
            except ValueError:
                pass
        backfill = not self.state.get("aligned")
        if backfill:
            # First chunk may start mid-line: drop the fragment. It goes to a separate backfill file
            # so the live recording keeps true receive timestamps.
            if offset > 0 and lines:
                lines = lines[1:]
            self.state["aligned"] = True
        n = 0
        rec_ts = _now()
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        name = f"certpl-backfill-{day}.jsonl" if backfill else f"certpl-{day}.jsonl"
        out = self.out_dir / name
        with out.open("a", encoding="utf-8") as fh:
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                fh.write(json.dumps({"rec_ts": rec_ts, "source": "certpl", "raw": raw}, ensure_ascii=False) + "\n")
                n += 1
        self.state["offset"] = offset + len(body)
        self.state["partial"] = partial
        self.state["etag"] = headers.get("ETag") or headers.get("etag")
        self._save()
        return n


def _stop(*_a: object) -> None:
    global _STOP
    _STOP = True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HYDRA Watch recorder (CERT Polska actions log tail)")
    ap.add_argument("--out", default=os.environ.get("HYDRA_WATCH_REC_DIR", "data/watch/recordings"))
    ap.add_argument("--interval", type=float, default=90.0, help="seconds between polls (>=60 recommended)")
    ap.add_argument("--backfill-bytes", type=int, default=0, help="on first start, also record this tail")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    tail = CertTail(out, backfill_bytes=args.backfill_bytes)
    _log(f"recorder start out={out} interval={args.interval}s")
    while not _STOP:
        try:
            n = tail.poll()
            if n:
                _log(f"certpl +{n}")
        except Exception as exc:  # keep running overnight
            _log(f"certpl error {type(exc).__name__}: {exc}")
        if args.once:
            break
        slept = 0.0
        while slept < args.interval and not _STOP:
            time.sleep(1.0)
            slept += 1.0
    _log("recorder stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
