"""Aggregate the "Nadużycie wizerunku" card over every incident of a state DB: per-signal state counts, triggered count,
strength distribution and timing. Prints counts only (no names, values or file paths).

    . scripts/env.sh && HYDRA_STATE_DIR=/tmp/some-state uv run python scripts/likeness_card_stats.py
"""

from __future__ import annotations

import time
from collections import Counter

from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.config import get_settings


def main() -> None:
    with TestClient(create_app(get_settings())) as c:
        ids = [i["id"] for i in c.get("/v1/incidents", params={"limit": 1000}).json()["items"]]
        states: Counter[tuple[str, str]] = Counter()
        checks: Counter[tuple[str, str]] = Counter()
        strength: Counter[str] = Counter()
        triggered = 0
        t0 = time.perf_counter()
        for iid in ids:
            card = c.get(f"/v1/incidents/{iid}/likeness").json()
            triggered += bool(card["triggered"])
            strength[card["strength"]] += 1
            for s in card["signals"]:
                states[(s["key"], s["state"])] += 1
                for ch in s["checks"]:
                    checks[(ch["key"], ch["state"])] += 1
        dt = (time.perf_counter() - t0) / max(1, len(ids))
    print(f"incidents={len(ids)} triggered={triggered} mean_ms_per_card={dt * 1000:.1f}")
    print("strength", dict(sorted(strength.items())))
    for (k, st), n in sorted(states.items()):
        print(f"signal {k} {st}: {n}")
    for (k, st), n in sorted(checks.items()):
        print(f"check {k} {st}: {n}")


if __name__ == "__main__":
    main()
