"""Campaign fingerprint alerts for HYDRA Watch (saved per-campaign query → alert on new feed items).

For every known campaign (latest analysed run, corpus flag) a fingerprint is derived from the genes of its
members that a bare domain on the feed can show (name, RDAP, DNS):

| Clause | Level | Bits | Budget group | From |
|---|---|---|---|---|
| domain | campaign | 8 | domain | member landing host / registrable domain (non-platform) |
| family | campaign | 6 | name | generated-name template with a digit run (`oferta-#.sbs`, `?-offertanr#.cyou`) |
| skeleton | campaign | rarity − 4 (≤ 4) | name | `url_label_skeleton`, only if rarer than 1/2^8 names on the feed |
| platform_token | campaign | 6 | token | invented platform/product names (`product_name`, `platform_page_name`) |
| ns_set | campaign | 6 | ns | operator-tier NS set (shared hosters/CDN NS excluded) |
| ns_host | campaign | 4 | ns | operator-tier NS host |
| registrar_day | campaign | 4 | registration | registrar + registration day |
| registrar_week | group | 2 | registration | registrar + ISO week (kit/operator habit, weak) |
| brand_placement | group | 1.5 | brand | brand + placement + TLD (`Allegro|sub|sbs`): a template habit, kit level |

Within one budget group only the strongest matched clause counts. An alert is emitted at >= `ALERT_MIN_BITS` and
only with an anchor clause (domain, family, platform_token, ns_set, ns_host, registrar_day; shape, brand placement
and registration week alone never alert);
level `campaign` when campaign-level bits >= 6, otherwise `group`. Confidence: `wysoka` (campaign bits >= 10 from
>= 2 groups), `średnia` (campaign bits >= 6), `niska` (otherwise). **An alert is a candidate, never a confirmed
member**: confirmation is the open-set assigner plus an analyst decision.

The fingerprint set is written once per load (`fingerprints-<run>.json` with sha256 and `created_at`) before any
replay, so a replay is a pre-registered test of the saved queries.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hydra.contracts import GeneStatus
from hydra.enrich.domains import is_platform, url_label_skeleton
from hydra.watch.names import family_key, parts

ALERT_MIN_BITS = 3.0
# Weak clauses (name shape, brand placement, registration week) only add bits: an alert needs an anchor clause.
# Measured: on 9,351 CERT actions, skeleton-only matches gave 49 alerts like 'L11.pro' vs NASK campaigns.
WEAK = {"skeleton", "brand_placement", "registrar_week"}
CLAUSE: dict[str, tuple[str, float, str]] = {  # kind -> (level, bits, budget group)
    "domain": ("campaign", 8.0, "domain"),
    "family": ("campaign", 6.0, "name"),
    "skeleton": ("campaign", 4.0, "name"),
    "platform_token": ("campaign", 6.0, "token"),
    "ns_set": ("campaign", 6.0, "ns"),
    "ns_host": ("campaign", 4.0, "ns"),
    "registrar_day": ("campaign", 4.0, "registration"),
    "registrar_week": ("group", 2.0, "registration"),
    "brand_placement": ("group", 1.5, "brand"),
}
LABEL_PL = {
    "domain": "ta sama domena", "family": "ta sama rodzina nazw", "skeleton": "rzadki szkielet nazwy",
    "platform_token": "nazwa platformy/produktu", "ns_set": "ten sam zestaw serwerów DNS",
    "ns_host": "ten sam serwer DNS", "registrar_day": "rejestrator + dzień rejestracji",
    "registrar_week": "rejestrator + tydzień rejestracji", "brand_placement": "ta sama marka w tym samym miejscu",
}


def _week(day: str) -> str | None:
    try:
        d = datetime.fromisoformat(day[:10])
    except ValueError:
        return None
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def brand_placements(host: str) -> list[str]:
    from hydra.watch.score import brand_hits

    p = parts(host)
    return sorted({f"{h.brand.name}|{h.where}|{p.tld}" for h in brand_hits(p) if h.kind != "typo"})


@dataclass
class Fingerprint:
    campaign_id: str
    label: str
    group_id: str | None
    members: int
    clauses: list[dict[str, Any]] = field(default_factory=list)  # {kind, value, level, bits}


@dataclass
class FingerprintSet:
    run_id: str | None
    corpus: str
    created_at: str
    fingerprints: dict[str, Fingerprint] = field(default_factory=dict)
    index: dict[tuple[str, str], list[str]] = field(default_factory=lambda: defaultdict(list))
    excluded: list[str] = field(default_factory=list)
    sha256: str | None = None

    def summary(self) -> dict[str, Any]:
        kinds: dict[str, int] = defaultdict(int)
        for fp in self.fingerprints.values():
            for c in fp.clauses:
                kinds[c["kind"]] += 1
        return {"run_id": self.run_id, "corpus": self.corpus, "created_at": self.created_at, "sha256": self.sha256,
                "fingerprints": len(self.fingerprints), "clauses": dict(sorted(kinds.items())),
                "excluded_incidents": len(self.excluded)}

    def to_json(self) -> str:
        body = {"run_id": self.run_id, "corpus": self.corpus, "created_at": self.created_at,
                "excluded": self.excluded, "fingerprints": [asdict(f) for f in self.fingerprints.values()]}
        return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _operator(g) -> bool:
    grp = (g.dependency_group or "").lower()
    return ":boilerplate:" not in grp and ":platform:" not in grp and ":label:" not in grp


def build(index, exclude: set[str] | None = None) -> FingerprintSet:
    """Fingerprints from a `link.CampaignIndex` (campaigns + member genomes). `exclude`: held-out incidents."""
    exclude = exclude or set()
    fs = FingerprintSet(run_id=index.run_id, corpus=index.corpus,
                        created_at=datetime.now(UTC).isoformat(timespec="seconds"), excluded=sorted(exclude))
    for c in index.campaigns:
        members = [m for m in c.members if m not in exclude and m in index.genomes]
        if not members:
            continue
        seen: set[tuple[str, str]] = set()
        fp = Fingerprint(c.id, c.label, c.group_id, len(members))

        def add(kind: str, value: str | None, fp=fp, seen=seen) -> None:
            if not value or (kind, value) in seen:
                return
            seen.add((kind, value))
            level, bits, _grp = CLAUSE[kind]
            fp.clauses.append({"kind": kind, "value": value, "level": level, "bits": bits})

        for m in members:
            for g in index.genomes[m].genes:
                if g.status != GeneStatus.OBSERVED or g.value is None:
                    continue
                v = str(g.value)
                if g.kind in ("landing_host", "registrable_domain") and not is_platform(v):
                    add("domain", v.lower())
                    if g.kind == "registrable_domain":
                        fk = family_key(parts(v))
                        if "#" in fk:
                            add("family", fk)
                    if g.kind == "landing_host":
                        for bp in brand_placements(v):
                            add("brand_placement", bp)
                elif g.kind == "url_label_skeleton":
                    add("skeleton", v)
                elif g.kind in ("product_name", "platform_page_name"):
                    for t in _tokens(v):
                        add("platform_token", t)
                elif g.kind == "ns_set" and _operator(g):
                    add("ns_set", v)
                elif g.kind == "ns_host" and _operator(g):
                    add("ns_host", v)
                elif g.kind == "registrar_day":
                    add("registrar_day", v)
                    reg, _, day = v.partition("|")
                    wk = _week(day)
                    if wk:
                        add("registrar_week", f"{reg}|{wk}")
        if fp.clauses:
            fs.fingerprints[c.id] = fp
            for cl in fp.clauses:
                fs.index[(cl["kind"], cl["value"])].append(c.id)
    fs.sha256 = hashlib.sha256(fs.to_json().encode()).hexdigest()
    return fs


def _tokens(v: str) -> list[str]:
    from hydra.watch.link import _STOP, _slug
    from hydra.watch.names import alpha_tokens, fold

    out = []
    whole = _slug(v)
    if len(whole) >= 6 and whole not in _STOP:
        out.append(whole)
    out += [t for t in alpha_tokens(fold(v.replace("_", " "))[0]) if len(t) >= 7 and t not in _STOP]
    return out


def save(fs: FingerprintSet, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"fingerprints-{fs.run_id or 'none'}-{fs.sha256[:12] if fs.sha256 else 'x'}.json"
    if not p.exists():
        p.write_text(fs.to_json(), encoding="utf-8")
    return p


# --------------------------------------------------------------------------------------- matching


def event_features(host: str, enrichment: dict[str, Any] | None, families=None) -> list[tuple[str, str, float]]:
    """(kind, value, bits) a watch event can show; skeleton bits depend on its rarity on the feed."""
    from hydra.genome.inf import SHARED_NS
    from hydra.watch.link import shape_is_rare

    p = parts(host)
    feats: list[tuple[str, str, float]] = [("domain", p.host, CLAUSE["domain"][1]),
                                           ("domain", p.registrable, CLAUSE["domain"][1])]
    fk = family_key(p)
    if "#" in fk:
        feats.append(("family", fk, CLAUSE["family"][1]))
    sk = url_label_skeleton(p.host)
    if sk and families is not None:
        n_sk, n_all = families.skeleton_freq(sk)
        if shape_is_rare(n_sk, n_all):
            import math

            feats.append(("skeleton", sk, min(CLAUSE["skeleton"][1], math.log2(n_all / (n_sk + 1)) - 4.0)))
    for bp in brand_placements(p.host):
        feats.append(("brand_placement", bp, CLAUSE["brand_placement"][1]))
    from hydra.watch.names import alpha_tokens

    for lab in p.f_labels:
        for t in {*alpha_tokens(lab), lab.replace("-", "")}:
            if len(t) >= 6:
                feats.append(("platform_token", t, CLAUSE["platform_token"][1]))
    e = enrichment or {}
    ns = sorted(set(e.get("ns") or (e.get("rdap") or {}).get("nameservers") or []))
    ns_op = [n for n in ns if not any(x in n for x in SHARED_NS)]
    if ns and len(ns_op) == len(ns):
        feats.append(("ns_set", ",".join(ns), CLAUSE["ns_set"][1]))
    for n in ns_op:
        feats.append(("ns_host", n, CLAUSE["ns_host"][1]))
    rd = e.get("rdap") or {}
    if rd.get("registrar") and rd.get("registration"):
        day = str(rd["registration"])[:10]
        feats.append(("registrar_day", f"{rd['registrar']}|{day}"[:150], CLAUSE["registrar_day"][1]))
        wk = _week(day)
        if wk:
            feats.append(("registrar_week", f"{rd['registrar']}|{wk}", CLAUSE["registrar_week"][1]))
    return feats


def match(fs: FingerprintSet, host: str, enrichment: dict[str, Any] | None = None, families=None,
          top: int = 3) -> list[dict[str, Any]]:
    """Alerts (candidates) for one event, strongest first."""
    hits: dict[str, dict[str, tuple[str, str, float, str]]] = defaultdict(dict)  # cid -> budget group -> best
    for kind, value, bits in event_features(host, enrichment, families):
        if bits <= 0:
            continue
        for cid in fs.index.get((kind, value), ()):
            level, _b, grp = CLAUSE[kind]
            cur = hits[cid].get(grp)
            if cur is None or bits > cur[2]:
                hits[cid][grp] = (kind, value, bits, level)
    out = []
    for cid, groups in hits.items():
        camp = sum(b for _k, _v, b, lv in groups.values() if lv == "campaign")
        grp_bits = sum(b for _k, _v, b, lv in groups.values() if lv == "group")
        total = camp + grp_bits
        if total < ALERT_MIN_BITS or all(k in WEAK for k, _v, _b, _lv in groups.values()):
            continue
        n_groups = sum(1 for _k, _v, _b, lv in groups.values() if lv == "campaign")
        level = "campaign" if camp >= 6 else "group"
        conf = "wysoka" if camp >= 10 and n_groups >= 2 else "średnia" if camp >= 6 else "niska"
        fp = fs.fingerprints[cid]
        out.append({"campaign_id": cid, "campaign_label": fp.label, "group_id": fp.group_id, "level": level,
                    "confidence": conf, "bits": round(total, 2), "campaign_bits": round(camp, 2),
                    "matched": [{"kind": k, "value": v, "bits": round(b, 2), "level": lv, "label_pl": LABEL_PL[k]}
                                for k, v, b, lv in sorted(groups.values(), key=lambda t: -t[2])],
                    "status": "kandydat"})
    out.sort(key=lambda a: (-a["campaign_bits"], -a["bits"], a["campaign_id"]))
    return out[:top]
