"""Offline temporal replay (INNOWACJE §13). No network: inputs are local recordings only.

`KnowledgeView(observations, cutoff)` holds ONLY observations with `available_at <= cutoff`; asking it for anything
else raises `TemporalLeak`. Signatures and hypotheses are built from a view; later observations are replayed in
`available_at` order by `ReplayClock` and verified against hypotheses already written to the ledger.

Source (a), REAL: the WATCH stream's CERT Polska warning-list recordings
(`<data>/watch/recordings/certpl*.jsonl`, written by hydra/watch/recorder.py on feat/watch). Times:
- `event_time`   = `ActionTime` of the CERT action;
- `available_at` = `rec_ts` for the live tail (recorded). For the backfill (fetched once, later), the CERT log is
  regenerated about every 5 minutes (recorder docstring), so `available_at = ActionTime + 5 min` is an
  ASSUMPTION, stated in `available_at_basis` on every such observation;
- `collected_at` = `rec_ts`.
A CERT "campaign" here is a name-generator family (a digit template such as `pi-oferta#` over >= 2 registrable
domains). It is a name-pattern hypothesis, never proof of one operator. Brand subdomains are decoration.

Source (b), SYNTHETIC: `hydra.hunt.synthetic` (mutation vocabulary of eval/variants.py), reported separately.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from hydra.hunt.hypotheses import iso, parse_ts
from hydra.hunt.records import DatasetKind, Observation, Tier, Trait, sha256_of


class TemporalLeak(RuntimeError):
    """Raised when code issuing a hypothesis asks for data that was not available at the cutoff."""


class KnowledgeView:
    def __init__(self, observations: list[Observation], cutoff: str) -> None:
        self.cutoff = cutoff
        c = parse_ts(cutoff)
        past = {o.observation_id: o for o in observations if parse_ts(o.available_at) <= c}
        # evidence versions of one series (CR-ASTRA-RES-4): only the latest version available at the cutoff is
        # current; earlier ones stay addressable by id (they were known) but are not listed or matched by ref
        latest: dict[str, Observation] = {}
        for o in past.values():
            s = o.meta.get("evidence_series")
            if s and (s not in latest or (parse_ts(o.available_at), o.observation_id) >
                      (parse_ts(latest[s].available_at), latest[s].observation_id)):
                latest[s] = o
        self._visible = {k: o for k, o in past.items()
                         if not o.meta.get("evidence_series") or latest[o.meta["evidence_series"]] is o}
        self._superseded = {k: o for k, o in past.items() if k not in self._visible}
        self._by_ref: dict[str, list[Observation]] = defaultdict(list)
        for o in self._visible.values():
            self._by_ref[o.ref].append(o)
        # only ids are kept for the future, to tell a leak from an unknown id; never their content
        self._future_ids = {x for o in observations if parse_ts(o.available_at) > c for x in (o.observation_id, o.ref)}

    def visible(self) -> list[Observation]:
        return sorted(self._visible.values(), key=lambda o: (o.available_at, o.observation_id))

    def get(self, key: str) -> list[Observation]:
        if key in self._visible:
            return [self._visible[key]]
        if key in self._by_ref:
            return list(self._by_ref[key])
        if key in self._superseded:
            return [self._superseded[key]]
        if key in self._future_ids:
            raise TemporalLeak(f"{key} has available_at > cutoff {self.cutoff}")
        raise KeyError(key)

    def manifest_sha256(self) -> str:
        return sha256_of(sorted((o.observation_id, o.payload_sha256) for o in self._visible.values()))


class ReplayClock:
    """Later observations in `available_at` order."""

    def __init__(self, observations: list[Observation], start: str) -> None:
        s = parse_ts(start)
        self._q = sorted((o for o in observations if parse_ts(o.available_at) > s),
                         key=lambda o: (o.available_at, o.observation_id))
        self.now = start

    def advance(self, until: str) -> Iterator[Observation]:
        u = parse_ts(until)
        while self._q and parse_ts(self._q[0].available_at) <= u:
            o = self._q.pop(0)
            self.now = o.available_at
            yield o
        self.now = until


# ------------------------------------------------------------------------------------------- CERT PL names

SECOND_LEVEL = {"com.pl", "net.pl", "org.pl", "co.uk", "com.br", "info.pl", "waw.pl", "biz.pl", "edu.pl"}
# Lexicon of brands commonly impersonated in PL listings. Brand = DECORATION: never an invariant, never truth.
BRANDS = {"olx", "allegro", "allegrolokalnie", "vinted", "inpost", "dpd", "dhl", "poczta", "pocztapolska", "orlen",
          "pge", "mbank", "pko", "ing", "santander", "blik", "alebilet", "otomoto", "booking", "revolut", "facebook",
          "whatsapp", "netflix", "lidl", "biedronka", "zabka", "gls", "ups", "fedex", "orange", "play", "plus",
          "tmobile", "pekao", "millennium", "aliorbank", "credit-agricole", "bnp", "vectra", "tauron", "enea",
          "energa", "gov", "mobywatel", "epuap", "ceneo", "empik", "rossnet", "rossmann", "sprzedajemy", "gratka"}
_DIG = re.compile(r"\d+")
PUBLICATION_LAG = timedelta(minutes=5)


def split_host(host: str) -> tuple[str, str, list[str]]:
    """host -> (registrable, tld, sub_labels)"""
    labels = host.lower().strip(".").split(".")
    n = 3 if len(labels) >= 3 and ".".join(labels[-2:]) in SECOND_LEVEL else 2
    reg = ".".join(labels[-n:])
    return reg, ".".join(labels[-(n - 1):]), labels[:-n]


def name_traits(host: str) -> list[Trait]:
    reg, tld, subs = split_host(host)
    sld = reg.split(".")[0]
    out = [Trait(kind="registrable", value=reg, tier=Tier.DECORATION, group="decoration", dep="name:registrable",
                 source="name")]
    if _DIG.search(sld) and _DIG.sub("", sld).strip("-"):
        tpl = _DIG.sub("#", sld)
        # a template with < 3 letters (`o#`, `pl-#`) is too generic to name one generator: kit tier, never invariant
        generic = len(re.sub(r"[^a-z]", "", tpl)) < 3
        out.append(Trait(kind="name_template", value=tpl, tier=Tier.KIT if generic else Tier.OPERATOR,
                         group="name_pattern", dep="name:template", source="name"))
        longest = max(len(d) for d in _DIG.findall(sld))
        out.append(Trait(kind="digits_len", value=str(longest), tier=Tier.KIT, group="name_pattern",
                         dep="name:digits", source="name"))
    out.append(Trait(kind="tld", value=tld, tier=Tier.KIT, group="name_pattern", dep="name:tld", source="name"))
    for lab in subs:
        if lab in BRANDS or lab.replace("-", "") in BRANDS:
            out.append(Trait(kind="brand_label", value=lab, tier=Tier.DECORATION, group="decoration",
                             dep="name:brand", source="name"))
    for tok in re.split(r"[-.]", sld):
        if tok in BRANDS:
            out.append(Trait(kind="brand_label", value=tok, tier=Tier.DECORATION, group="decoration",
                             dep="name:brand", source="name"))
    return out


TIME_BASES = ("assumed_publication", "acquisition")


def load_certpl(recordings: Path, *, time_basis: str = "assumed_publication",
                lag: timedelta = PUBLICATION_LAG) -> tuple[list[Observation], dict[str, Any]]:
    """`time_basis="assumed_publication"`: backfill `available_at = ActionTime + lag` (ASSUMED: a counterfactual
    live recorder). `"acquisition"`: `available_at = rec_ts` for every record, i.e. when HYDRA actually held it
    (CR-ASTRA-RES-4: nothing is back-dated). Name traits derive from the record itself, so extraction completes at
    acquisition (`completed_at = rec_ts`)."""
    if time_basis not in TIME_BASES:
        raise ValueError(f"time_basis must be one of {TIME_BASES}")
    obs: dict[int, Observation] = {}
    live_lag_s: list[float] = []
    files = sorted(recordings.glob("certpl*.jsonl"))
    for f in files:
        backfill = "backfill" in f.name
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                raw = rec["raw"]
            except (ValueError, KeyError):
                continue
            if raw.get("ActionType") != "block" or not raw.get("DomainAddress"):
                continue
            pid = int(raw["RegisterPositionId"])
            ev = iso(parse_ts(raw["ActionTime"]))
            if backfill and time_basis == "assumed_publication":
                avail = iso(parse_ts(raw["ActionTime"]) + lag)
                basis = f"assumed: ActionTime + {lag.total_seconds() / 60:g} min"
            elif backfill:
                avail, basis = iso(parse_ts(rec["rec_ts"])), "recorded: rec_ts of the backfill fetch (acquisition)"
            else:
                avail, basis = iso(parse_ts(rec["rec_ts"])), "recorded: rec_ts of the live tail"
                live_lag_s.append((parse_ts(rec["rec_ts"]) - parse_ts(raw["ActionTime"])).total_seconds())
            host = str(raw["DomainAddress"]).lower()
            o = Observation(observation_id=f"certpl:{pid}", source="certpl_recording", ref=host, event_time=ev,
                            available_at=avail, available_at_basis=basis, collected_at=rec["rec_ts"],
                            completed_at=rec["rec_ts"], evidence_version=f"certpl:{pid}:{sha256_of(raw)[:16]}",
                            payload_sha256=sha256_of(raw), evidence_pointer=f"{f.name}#RegisterPositionId={pid}",
                            traits=name_traits(host), dataset_kind=DatasetKind.REAL,
                            meta={"unit": split_host(host)[0]})
            if pid not in obs or not backfill:  # the live record (real rec_ts) wins over the backfill copy
                obs[pid] = o
    out = sorted(obs.values(), key=lambda o: (o.available_at, o.observation_id))
    lags = sorted(live_lag_s)

    def q(p: float) -> float | None:
        return round(lags[min(len(lags) - 1, int(p * len(lags)))] / 60, 1) if lags else None

    info = {"files": [f.name for f in files], "observations": len(out), "time_basis": time_basis,
            "assumed": sum(1 for o in out if o.available_at_basis.startswith("assumed")),
            "live_tail_lag_min": {"n": len(lags), "basis": "rec_ts - ActionTime of live-tail records (measured; "
                                  "includes the recorder poll interval)", "median": q(0.5), "p90": q(0.9),
                                  "max": round(lags[-1] / 60, 1) if lags else None},
            "first_available": out[0].available_at if out else None,
            "last_available": out[-1].available_at if out else None}
    return out, info


def registrable_units(obs: list[Observation]) -> dict[str, list[Trait]]:
    """Observations -> one unit per registrable domain (all its listed hosts' traits, deduplicated)."""
    units: dict[str, dict[str, Trait]] = defaultdict(dict)
    for o in obs:
        reg = split_host(o.ref)[0]
        for t in o.traits:
            units[reg].setdefault(t.key, t)
    return {k: list(v.values()) for k, v in units.items()}


def families(units: dict[str, list[Trait]], min_members: int = 2) -> dict[str, list[str]]:
    fam: dict[str, list[str]] = defaultdict(list)
    for reg, traits in units.items():
        for t in traits:
            if t.kind == "name_template":
                fam[t.value].append(reg)
    return {k: sorted(v) for k, v in fam.items() if len(v) >= min_members}
