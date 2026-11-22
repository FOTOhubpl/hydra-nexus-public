"""Analyst-triggered live enrichment extras: DNS consensus (3 resolvers + wildcard probe) and the TLS probe.

Usage (live mode only, never in the pipeline):
    HYDRA_ENRICH=1 HYDRA_OFFLINE=0 uv run python scripts/enrich_plus.py dns  host1 host2 ...
    HYDRA_ENRICH=1 HYDRA_OFFLINE=0 uv run python scripts/enrich_plus.py tls  host1 host2 ...
    uv run python scripts/enrich_plus.py replay dns|tls host ...      # cache only, works offline

Results go to the replay cache (Settings.enrich_cache, HYDRA_ENRICH_CACHE); one JSON summary per host is printed.
The TLS probe contacts the host itself (TLS handshakes, no HTTP request): run it only for hosts an analyst chose.
"""

from __future__ import annotations

import json
import sys

from hydra.config import Settings
from hydra.enrich import dns_consensus, tlsprobe
from hydra.enrich.client import Enricher, live_enabled


def main(argv: list[str]) -> int:
    replay = bool(argv) and argv[0] == "replay"
    args = argv[1:] if replay else argv
    if len(args) < 2 or args[0] not in ("dns", "tls"):
        print(__doc__)
        return 2
    s = Settings()
    if not replay and not live_enabled(s):
        print("live mode is off (HYDRA_OFFLINE=1 or HYDRA_ENRICH=0): nothing queried; use `replay`", file=sys.stderr)
        return 3
    enr = Enricher(settings=s, live=False if replay else None)
    for host in args[1:]:
        if args[0] == "dns":
            r, summ = dns_consensus.check(enr, host)
            out = {k: summ.get(k) for k in ("class", "apex", "wildcard_zone", "addrs", "cnames", "agreement")}
        else:
            r, summ = (tlsprobe.cached if replay else tlsprobe.probe)(enr, host)
            out = summ
        print(json.dumps({"host": host, "status": r.status, "fetched_at": r.fetched_at, **out}, ensure_ascii=False))
    print(json.dumps({"stats": enr.stats}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
