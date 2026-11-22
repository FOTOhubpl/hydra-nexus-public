"""Hypothesis issue + the append-only, hash-chained HUNT ledger (INNOWACJE §3, §11, §13).

Ledger = JSON lines `<state>/hunt/ledger.jsonl`; each line
`{"seq", "ts", "type", "body", "prev_hash", "hash"}` with `hash = sha256(prev_hash \\n ts \\n type \\n body)`
(the same construction as hydra.events). With a Writer, every record also gets an `events.append` row
(`HUNT_<TYPE>` with the ledger hash) in the main audit chain, so the issue order is auditable in two places.
Status changes are new records (`VERIFIED`), never edits. A local hash chain proves internal order, not an
independent timestamp (INNOWACJE §3).
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from hydra import events
from hydra.contracts import CONTRACTS_VERSION
from hydra.hunt import HUNT_VERSION
from hydra.hunt.records import DatasetKind, Hypothesis, Mode, Signature, Status

GENESIS = "0" * 64
_LOCK = threading.Lock()


def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without a timezone: {s!r}")
    return dt


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _hash(prev: str, ts: str, type_: str, body: str) -> str:
    return hashlib.sha256(f"{prev}\n{ts}\n{type_}\n{body}".encode()).hexdigest()


class Ledger:
    def __init__(self, path: Path, writer: Any | None = None) -> None:
        self.path = Path(path)
        self.writer = writer
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def append(self, type_: str, body: dict[str, Any], ts: str | None = None) -> dict[str, Any]:
        with _LOCK:
            recs = self.records()
            prev = recs[-1]["hash"] if recs else GENESIS
            ts = ts or events.utcnow()
            canon = events.canonical_json(body)
            rec = {"seq": len(recs) + 1, "ts": ts, "type": type_, "body": body, "prev_hash": prev,
                   "hash": _hash(prev, ts, type_, canon)}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        if self.writer is not None:
            ref = {"ledger_seq": rec["seq"], "ledger_hash": rec["hash"], "id": body.get("hypothesis_id")
                   or body.get("observation_id") or body.get("action_id")}
            self.writer.run(lambda conn: events.append(conn, f"HUNT_{type_}", ref))
        return rec

    def verify(self) -> tuple[bool, int, int | None]:
        prev = GENESIS
        n = 0
        for rec in self.records():
            n += 1
            if rec["prev_hash"] != prev or _hash(prev, rec["ts"], rec["type"],
                                                 events.canonical_json(rec["body"])) != rec["hash"]:
                return False, n, rec["seq"]
            prev = rec["hash"]
        return True, n, None

    # ------------------------------------------------------------------- folded state

    def hypotheses(self) -> dict[str, dict[str, Any]]:
        """hypothesis_id -> {"hypothesis", "issue_seq", "issue_hash", "results": [...], "actions": [...]}"""
        out: dict[str, dict[str, Any]] = {}
        for rec in self.records():
            b = rec["body"]
            if rec["type"] == "HYPOTHESIS_ISSUED":
                out[b["hypothesis_id"]] = {"hypothesis": b, "issue_seq": rec["seq"], "issue_hash": rec["hash"],
                                           "issue_ts": rec["ts"], "results": [], "actions": [], "observations": []}
            elif rec["type"] in ("VERIFIED", "ACTION", "OBSERVATION_LINKED") and b.get("hypothesis_id") in out:
                key = {"VERIFIED": "results", "ACTION": "actions", "OBSERVATION_LINKED": "observations"}[rec["type"]]
                out[b["hypothesis_id"]][key].append({**b, "seq": rec["seq"], "hash": rec["hash"]})
        for h in out.values():
            if h["results"]:
                h["hypothesis"] = {**h["hypothesis"], "status": h["results"][-1]["status"]}
        return out


CONFLICT_KINDS = {"tracker_id", "fb_pixel", "pixel_id", "ga_id", "gtm_id", "account_id", "acct", "operator_id",
                  "platform_ad_id", "page_id", "affiliate_id", "offer_id", "entity_phone", "phone", "tds_param"}


def invariant_groups(sig: Signature) -> dict[str, str]:
    by_key = {f"{g.kind}={g.value}": g.group for g in sig.genes}
    return {k: by_key.get(k, "operator") for k in sig.invariants}


def statement_pl(sig: Signature, horizon_h: float, sources: list[str]) -> str:
    inv = ", ".join(sig.invariants[:4]) or "brak niezmienników"
    mut = ", ".join(sig.allowed_mutations[:5])
    return (f"Kampania {sig.campaign_id} prawdopodobnie pojawi się w nowym wariancie w ciągu {horizon_h:g} h "
            f"w źródłach: {', '.join(sources)}. Mogą się zmienić: {mut} ({sig.mutations_basis}). "
            f"Muszą zostać zachowane ślady z grup: {', '.join(sig.required_evidence_groups) or '-'} "
            f"(np. {inv}). Sama marka, wspólny CDN ani publiczny kit nie potwierdzają hipotezy.")


def issue(sig: Signature, view: Any, *, issued_at: str, horizon_hours: float, sources: list[str],
          query_budget: int, ledger: Ledger | None = None, evidence_ids: list[str] | None = None,
          mode: Mode = Mode.TEMPORAL_REPLAY, protocol: str = "temporal_replay_v1") -> Hypothesis:
    """Issue a hypothesis from a frozen KnowledgeView. Refuses evidence or a cutoff from after `issued_at`."""
    cutoff = view.cutoff
    if parse_ts(cutoff) > parse_ts(issued_at):
        raise ValueError("knowledge_cutoff after issued_at")
    ev = sorted(evidence_ids if evidence_ids is not None else sig.members)
    for e in ev:
        view.get(e)  # raises TemporalLeak for anything not available at the cutoff
    from hydra.hunt.signatures import has_operator_invariant

    if not sig.invariants or not has_operator_invariant(sig):
        raise ValueError("signature has no operator-tier invariant: nothing verifiable to predict (kit alone is not "
                         "operator evidence)")
    h = Hypothesis(campaign_revision=sig.campaign_revision, campaign_id=sig.campaign_id, signature_sha256=sig.sha256,
                   issued_at=issued_at, knowledge_cutoff=cutoff, corpus_manifest_sha256=view.manifest_sha256(),
                   statement_pl=statement_pl(sig, horizon_hours, sources), horizon_hours=horizon_hours,
                   horizon_end=iso(parse_ts(issued_at) + timedelta(hours=horizon_hours)),
                   invariants=sig.invariants, invariant_groups=invariant_groups(sig),
                   invariant_sources={f"{g.kind}={g.value}": g.source for g in sig.genes
                                      if f"{g.kind}={g.value}" in sig.invariants},
                   conflict_kinds=sorted({g.kind for g in sig.genes if g.role == "invariant" and g.group == "operator"
                                          and g.kind in CONFLICT_KINDS}),
                   decoration_keys=sorted({f"{g.kind}={g.value}" for g in sig.genes if g.role == "decoration"})[:50],
                   allowed_mutations=sig.allowed_mutations,
                   required_evidence_groups=sig.required_evidence_groups, hard_conflicts=sig.hard_conflicts,
                   sources=sources, query_budget=query_budget, priority=sig.priority, status=Status.PENDING,
                   evidence_ids_at_issue=ev, known_refs=sorted(sig.members), evaluation_protocol=protocol,
                   dataset_kind=DatasetKind(sig.dataset_kind), mode=mode,
                   versions={"hunt": HUNT_VERSION, "contracts": CONTRACTS_VERSION, "signature": sig.sha256[:16]})
    h.hypothesis_id = h.content_id()
    if ledger is not None:
        ledger.append("HYPOTHESIS_ISSUED", {**h.model_dump(mode="json"), "signature": sig.model_dump(mode="json")})
    return h
