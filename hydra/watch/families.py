"""Watch-only linking: name-pattern families and "live campaigns" built from the stream itself.

- `FamilyIndex` counts distinct registrable domains per digit template (`oferta-#.sbs`) and url_label_skeleton
  frequencies over a sliding window (default 7 days of source time). Seeded from the recorder's backfill.
- `LiveCampaigns` is an incremental union-find over events. Two events join when they share
  (1) a registrable domain (allegro./olx./vinted. under one `innpozycja543412.sbs`),
  (2) a digit template that has >= 2 distinct registrable domains (a generated burst), or
  (3) an operator-tier enrichment value (non-shared NS set, registrar + registration day).
  A component with >= 2 distinct registrable domains, or one registrable domain with >= 3 listed hosts (a
  brand-subdomain burst), is a live campaign `W-<hash>`.
These are watch-level hypotheses (name/infra pattern), shown next to, never instead of, the open-set
assignment against analysed campaigns.
"""

from __future__ import annotations

import hashlib
import threading
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

WINDOW = timedelta(days=7)
HOST_CAP = 200
MIN_HOSTS = 3  # one registrable domain with >= 3 listed hosts (brand-subdomain burst) is a live campaign too


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class FamilyIndex:
    def __init__(self, window: timedelta = WINDOW) -> None:
        self.window = window
        self._lock = threading.Lock()
        self._q: deque[tuple[datetime, str, str, str | None, tuple[str, ...]]] = deque()
        self._fam: dict[str, Counter[str]] = defaultdict(Counter)
        self._sk: Counter[str] = Counter()
        self._lab: dict[str, Counter[str]] = defaultdict(Counter)  # subdomain label -> registrable counts
        self._n = 0

    def add(self, ts: str, family: str, registrable: str, skeleton: str | None,
            sub_labels: tuple[str, ...] = ()) -> None:
        t = _dt(ts)
        with self._lock:
            self._q.append((t, family, registrable, skeleton, sub_labels))
            self._fam[family][registrable] += 1
            for lab in sub_labels:
                self._lab[lab][registrable] += 1
            if skeleton:
                self._sk[skeleton] += 1
            self._n += 1
            self._expire(t)

    def _expire(self, now: datetime) -> None:
        while self._q and now - self._q[0][0] > self.window:
            _t, fam, reg, sk, labs = self._q.popleft()
            for lab in labs:
                lc = self._lab[lab]
                lc[reg] -= 1
                if lc[reg] <= 0:
                    del lc[reg]
                if not lc:
                    del self._lab[lab]
            c = self._fam[fam]
            c[reg] -= 1
            if c[reg] <= 0:
                del c[reg]
            if not c:
                del self._fam[fam]
            if sk:
                self._sk[sk] -= 1
                if self._sk[sk] <= 0:
                    del self._sk[sk]
            self._n -= 1

    def family_size(self, key: str) -> int:
        with self._lock:
            return len(self._fam.get(key, ()))

    def label_size(self, label: str) -> int:
        """Distinct registrable domains that carried `label` as a subdomain label in the window."""
        with self._lock:
            return len(self._lab.get(label, ()))

    def skeleton_freq(self, skeleton: str) -> tuple[int, int]:
        with self._lock:
            return self._sk.get(skeleton, 0), self._n

    def top(self, n: int = 10, min_size: int = 3) -> list[dict[str, Any]]:
        with self._lock:
            items = [(k, len(v)) for k, v in self._fam.items() if "#" in k and len(v) >= min_size]
        items.sort(key=lambda t: (-t[1], t[0]))
        return [{"family": k, "domains": c} for k, c in items[:n]]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            fams = [len(v) for k, v in self._fam.items() if "#" in k]
            return {"names": self._n, "families_ge3": sum(1 for x in fams if x >= 3),
                    "names_in_families_ge3": sum(x for x in fams if x >= 3)}


class LiveCampaigns:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._parent: dict[str, str] = {}
        self._first: dict[str, str] = {}          # root -> earliest key (stable id)
        self._regs: dict[str, set[str]] = defaultdict(set)   # root -> registrable domains
        self._hosts: dict[str, set[str]] = defaultdict(set)  # root -> hosts (capped)
        self._keys: dict[str, set[str]] = defaultdict(set)   # root -> linking keys
        self._fam_regs: dict[str, set[str]] = defaultdict(set)
        self._order: dict[str, int] = {}

    def _find(self, x: str) -> str:
        p = self._parent.setdefault(x, x)
        if p != x:
            self._parent[x] = p = self._find(p)
        return p

    def _union(self, a: str, b: str) -> str:
        ra, rb = self._find(a), self._find(b)
        if ra == rb:
            return ra
        # keep the older root (first seen) so ids stay stable as campaigns grow
        inf = float("inf")
        if self._order.get(rb, inf) < self._order.get(ra, inf):
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._regs[ra] |= self._regs.pop(rb, set())
        hb = self._hosts.pop(rb, set())
        if len(self._hosts[ra]) < HOST_CAP:
            self._hosts[ra] |= hb
        self._keys[ra] |= self._keys.pop(rb, set())
        self._first.setdefault(ra, self._first.pop(rb, rb))
        return ra

    def add(self, registrable: str, family: str, infra_keys: list[str] | None = None,
            host: str | None = None) -> str | None:
        """Link one event; returns its live campaign id (None while it is a singleton)."""
        with self._lock:
            node = f"reg:{registrable}"
            if node not in self._parent:
                self._order[node] = len(self._order)
            root = self._find(node)
            self._first.setdefault(root, node)
            self._regs[root].add(registrable)
            if host and len(self._hosts[root]) < HOST_CAP:
                self._hosts[root].add(host)
            keys = []
            if "#" in family:
                regs = self._fam_regs[family]
                regs.add(registrable)
                if len(regs) == 2:  # the family just became a pattern: link its earlier member too
                    for r in regs:
                        if r != registrable:
                            self._union(f"reg:{r}", f"fam:{family}")
                    root = self._find(node)
                if len(regs) >= 2:
                    keys.append(f"fam:{family}")
            keys += [f"inf:{k}" for k in infra_keys or []]
            for k in keys:
                root = self._union(root, k)
                self._keys[self._find(root)].add(k)
            return self._id(self._find(node))

    def _is_campaign(self, root: str) -> bool:
        return len(self._regs.get(root, ())) >= 2 or len(self._hosts.get(root, ())) >= MIN_HOSTS

    def _id(self, root: str) -> str | None:
        if not self._is_campaign(root):
            return None
        first = self._first.get(root, root)
        return "W-" + hashlib.sha256(first.encode()).hexdigest()[:6]

    def campaign_of(self, registrable: str) -> str | None:
        with self._lock:
            return self._id(self._find(f"reg:{registrable}"))

    def summary(self, n: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for root, regs in self._regs.items():
                if self._find(root) != root or not self._is_campaign(root):
                    continue
                keys = sorted(self._keys.get(root, ()))
                out.append({"id": self._id(root), "domains": len(regs), "hosts": len(self._hosts.get(root, ())),
                            "sample": sorted(regs)[:5] if len(regs) > 1 else sorted(self._hosts.get(root, ()))[:5],
                            "keys": [k.split(":", 1)[1] for k in keys][:5]})
        out.sort(key=lambda d: (-d["domains"], d["id"]))
        return out[:n]

    def count(self) -> int:
        with self._lock:
            return sum(1 for r in list(self._regs) if self._find(r) == r and self._is_campaign(r))
