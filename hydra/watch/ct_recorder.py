"""CT recorder: certstream-compatible websocket (self-hosted certstream-server-go, MIT) → filtered JSONL.

The CT firehose is ~10^5 names per hour and well under 1 in 10^4 is a Polish scam (REALTIME_DISCOVERY.md §1), so
only candidates are recorded: Stage A score >= `--min-bits` (default 6, the review cut) on the name alone, one
line per certificate, de-duplicated per registrable domain per hour. Counters (names seen, kept) go to the log
every minute so the base rate is measurable.

    python -m hydra.watch.ct_recorder --url ws://127.0.0.1:8098/domains-only --out <recordings dir>

Line format (normalised by hydra.watch.events): {"rec_ts", "source": "ct", "raw": {"names": [...], "seen": ts}}.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from hydra.watch.names import parts
from hydra.watch.score import brand_hits, score_host


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def polish_signal(s) -> bool:
    """A CT name is a candidate only with a Polish-targeting signal: a brand of >= 4 letters, a Polish bait word,
    a homoglyph brand, a campaign gene, or a .pl name. A matched brand token of <= 3 letters (olx, ing, dhl, zus)
    alone is not enough: on the global CT stream such labels appear in infrastructure names."""
    codes = {r.code for r in s.reasons}
    if codes & {"bait", "homoglyph", "campaign_domain", "campaign_family", "campaign_token", "campaign_domain_typo"}:
        return True
    if not s.brands:
        return False
    # exact/prefix brand tokens of >= 4 letters; a typo alone is too weak on the global stream (pochta ≈ poczta)
    if any(len(h.token) >= 4 and h.kind != "typo" for h in brand_hits(parts(s.host))):
        return True
    return s.host.endswith(".pl")


class CtFilter:
    def __init__(self, min_bits: float = 6.0) -> None:
        self.min_bits = min_bits
        self.seen: dict[str, float] = {}
        self.names = 0
        self.kept = 0

    def keep(self, names: list[str]) -> list[str]:
        out = []
        now = time.time()
        for n in names:
            n = n.lower().removeprefix("*.")
            if not n or n in out:
                continue
            self.names += 1
            s = score_host(n)
            if s.bits < self.min_bits or not polish_signal(s):
                continue
            if now - self.seen.get(s.registrable, 0.0) < 3600:
                continue
            self.seen[s.registrable] = now
            out.append(n)
        if len(self.seen) > 200_000:
            self.seen = {k: v for k, v in self.seen.items() if now - v < 3600}
        self.kept += len(out)
        return out


async def run(url: str, out_dir: Path, min_bits: float) -> None:
    import os
    from urllib.parse import urlsplit

    # HYDRA_OFFLINE=1 (hard kill switch, audit A09) allows only the loopback certstream server.
    if os.environ.get("HYDRA_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on") and \
            urlsplit(url).hostname not in ("127.0.0.1", "localhost", "::1"):
        print(f"{_now()} ct disabled: HYDRA_OFFLINE=1 and {url} is not loopback", flush=True)
        return

    import websockets

    flt = CtFilter(min_bits)
    last_log = time.time()
    while True:
        try:
            async with websockets.connect(url, max_size=2**22, ping_interval=None, max_queue=4096) as ws:
                print(f"{_now()} ct connected {url}", flush=True)
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    data = msg.get("data")
                    names = data if isinstance(data, list) else ((data or {}).get("leaf_cert") or {}).get(
                        "all_domains", [])
                    kept = flt.keep([str(x) for x in names])
                    if kept:
                        day = datetime.now(UTC).strftime("%Y-%m-%d")
                        rec = {"rec_ts": _now(), "source": "ct", "raw": {"names": kept, "seen": time.time()}}
                        with (out_dir / f"ct-{day}.jsonl").open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    if time.time() - last_log >= 60:
                        print(f"{_now()} ct names={flt.names} kept={flt.kept}", flush=True)
                        last_log = time.time()
        except Exception as exc:  # noqa: BLE001 - reconnect forever
            print(f"{_now()} ct error {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(10)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HYDRA Watch CT recorder (filtered)")
    ap.add_argument("--url", default="ws://127.0.0.1:8098/domains-only")
    ap.add_argument("--out", default="data/watch/recordings")
    ap.add_argument("--min-bits", type=float, default=6.0)
    a = ap.parse_args(argv)
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(a.url, out, a.min_bits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
