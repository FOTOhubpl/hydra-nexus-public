"""Synthetic run state + random event sequences for the DATAFLOW tests (ADR-0009). Synthetic rows only.

`World` writes an immutable analyse run (incidents, genes, artifacts, campaigns with groups, pairs of every
hypothesis) and then applies events through the real write paths where they exist:
- new material and re-decisions: `hydra.pipeline._write_run(kind="assign")` (the path of upload, assign-batch and
  the scheduler inbox), after the incident and its genes are stored;
- pair reviews: the `reviews` row + `REVIEW` event of `POST /v1/reviews`; unsure marks: `hydra.review.mark_unsure`;
- exports: `hydra.export_log.record_export`.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from hydra import events
from hydra.contracts import Assignment, PairScore
from hydra.db import Writer
from hydra.export_log import record_export
from hydra.pipeline import _write_run
from hydra.synapse import PairEval

RUN = "syn-df-run"
FAMILIES = ("VIS", "TXT", "INF", "ID")
KINDS = {"VIS": ("pdq", "logo"), "TXT": ("ocr_line", "phrase"), "INF": ("domain", "tracker_id"), "ID": ("face",)}
DEPS = (None, None, "vis_template:x", "inf:kit:tracker", "txt:boiler")
ROLES = ("ad_creative", "feed_screenshot", "landing_screenshot", "page_asset")
HYPS = ("same_campaign", "shared_template", "shared_asset", "abstain", "independent", "independent")
EXPORT_KINDS = ("campaign_json", "stix_run", "subgraph_json")


@dataclass
class World:
    writer: Writer
    rng: random.Random
    base: list[str] = field(default_factory=list)
    campaigns: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    pairs: list[tuple[str, str]] = field(default_factory=list)
    reviews: list[str] = field(default_factory=list)
    unsure: list[str] = field(default_factory=list)
    n: int = 0
    log: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ rows
    def _incident(self, conn: sqlite3.Connection, iid: str) -> None:
        r = self.rng
        now = events.utcnow()
        day = f"2026-{r.randint(9, 11):02d}-{r.randint(1, 28):02d}T10:00:00+00:00" if r.random() < 0.85 else None
        src = r.choice(("nask-sample", "ext:extended", "upload"))
        conn.execute("INSERT INTO incidents (id, source, source_ref, title, observed_at, label, meta, created_at)"
                     " VALUES (?, ?, ?, ?, ?, NULL, '{}', ?)", (iid, src, f"syn/{iid}", f"Tytuł {iid}", day, now))
        for k in range(r.randint(0, 3)):
            aid = f"{iid}-art{k}".ljust(64, "0")
            mime = r.choice(("image/png", "image/jpeg", "text/html", "image/svg+xml"))
            conn.execute("INSERT OR IGNORE INTO artifacts (id, mime, size, source_ref, meta, created_at) VALUES"
                         " (?, ?, ?, ?, '{}', ?)", (aid, mime, r.randint(10, 9999), f"syn/{iid}/{k}", now))
            conn.execute("INSERT INTO incident_artifacts (incident_id, artifact_id, role, path_in_incident, meta)"
                         " VALUES (?, ?, ?, ?, '{}')", (iid, aid, r.choice(ROLES), f"f{k}"))
        self._genes(conn, iid)

    def _genes(self, conn: sqlite3.Connection, iid: str) -> None:
        r = self.rng
        now = events.utcnow()
        for _ in range(r.randint(1, 7)):
            fam = r.choice(FAMILIES)
            kind = r.choice(KINDS[fam])
            status = r.choice(("observed", "observed", "observed", "unavailable", "failed"))
            value = f"{kind}-{r.randint(0, 9)}" if status == "observed" and r.random() < 0.9 else None
            vec = f"{iid}|a|{kind}|0" if value is None and status == "observed" and r.random() < 0.5 else None
            ev = '{"enrichment_key":"rdap|x"}' if r.random() < 0.2 else ('{"note":"enrichment_key"}'
                                                                         if r.random() < 0.1 else "{}")
            conn.execute("INSERT INTO genes (incident_id, family, kind, value, vector_ref, quality, status, provenance,"
                         " evidence, extractor, dependency_group, created_at) VALUES (?, ?, ?, ?, ?, 1.0, ?, 'OBS', ?,"
                         " 'syn@1', ?, ?)", (iid, fam, kind, value, vec, status, ev, r.choice(DEPS), now))

    def _pair_eval(self, a: str, b: str, hyp: str | None = None) -> PairEval:
        r = self.rng
        a, b = sorted((a, b))
        fb = {f: round(r.uniform(-3, 14), 2) for f in r.sample(FAMILIES, r.randint(1, 3))}
        hyp = hyp or r.choice(HYPS)
        score = PairScore(a=a, b=b, family_bits=fb, missing_families=[], contradictions=[], reasons=[],
                          total_bits=round(sum(fb.values()), 4), probability=round(r.random(), 3), calibrated=False,
                          hypothesis=hyp, candidate_sources=["exact"], model="syn@1")
        return PairEval(score=score, campaign_bits=r.uniform(0, 12), kit_bits=r.choice((0.0, 3.0, 7.5)),
                        asset_bits=r.uniform(0, 4))

    # ------------------------------------------------------------------ base run
    def build_base(self, n_incidents: int = 24, n_campaigns: int = 6) -> None:
        r = self.rng
        self.base = [f"b{i:03d}" for i in range(n_incidents)]
        self.campaigns = [f"c{j}" for j in range(n_campaigns)]
        groups = {c: (f"g{j % 2}" if j % 3 else None) for j, c in enumerate(self.campaigns)}
        member: dict[str, str | None] = {}
        for iid in self.base:
            member[iid] = r.choice(self.campaigns) if r.random() < 0.85 else None
        now = events.utcnow()

        def tx(conn: sqlite3.Connection) -> None:
            for iid in self.base:
                self._incident(conn, iid)
            conn.execute("INSERT INTO runs (id, kind, params, metrics, created_at, finished_at) VALUES"
                         " (?, 'analyse', '{\"synthetic\":true}', '{}', ?, ?)", (RUN, now, now))
            for c in self.campaigns:
                ms = [i for i in self.base if member[i] == c]
                detail = {"id": c, "label": f"Kampania {c}", "members": ms, "group_id": groups[c],
                          "first_seen": "2026-09-15" if r.random() < 0.7 else None, "last_seen": "2026-10-20"}
                conn.execute("INSERT INTO campaigns (id, run_id, label, confidence, status, detail) VALUES"
                             " (?, ?, ?, 0.8, 'active', ?)", (c, RUN, f"Kampania {c}", events.canonical_json(detail)))
                conn.executemany("INSERT INTO campaign_members (run_id, campaign_id, incident_id) VALUES (?, ?, ?)",
                                 [(RUN, c, m) for m in ms])
            for iid in self.base:  # incidents without a campaign are in the run through their own decision
                if member[iid] is None or r.random() < 0.3:
                    conn.execute("INSERT INTO assignments (incident_id, run_id, decision, campaign_id, detail,"
                                 " created_at) VALUES (?, ?, ?, ?, '{}', ?)",
                                 (iid, RUN, r.choice(("assign", "new_campaign", "abstain")), member[iid], now))
            for i, a in enumerate(self.base):
                for b in self.base[i + 1:]:
                    if r.random() < 0.25:
                        pe = self._pair_eval(a, b)
                        s = pe.score
                        detail = {**s.model_dump(), "kit_bits": pe.kit_bits, "campaign_bits": pe.campaign_bits}
                        conn.execute("INSERT INTO pairs (a, b, run_id, total_bits, probability, calibrated, hypothesis,"
                                     " model, detail) VALUES (?, ?, ?, ?, ?, 0, ?, 'syn@1', ?)",
                                     (s.a, s.b, RUN, s.total_bits, s.probability, s.hypothesis,
                                      events.canonical_json(detail)))
                        self.pairs.append((s.a, s.b))
            events.append(conn, "RUN_FINISHED", {"run_id": RUN, "kind": "analyse", "pairs": len(self.pairs)})

        self.writer.run(tx)

    # ------------------------------------------------------------------ events
    def assign(self, iid: str | None = None, *, n_pairs: int | None = None, decision: str | None = None) -> str:
        """New material (iid None), or a new decision for `iid` (new or base incident), via `_write_run`."""
        r = self.rng
        fresh = iid is None
        if fresh:
            self.n += 1
            iid = f"n{self.n:04d}"
        assert iid is not None
        k = r.randint(0, 5) if n_pairs is None else n_pairs
        others = r.sample([b for b in self.base if b != iid], min(k, len(self.base) - 1))
        pairs = {}
        for o in others:
            pe = self._pair_eval(iid, o)
            pairs[(pe.score.a, pe.score.b)] = pe
        decision = decision or r.choice(("assign", "assign", "new_campaign", "abstain"))
        camp = r.choice([*self.campaigns, "c-missing"]) if decision == "assign" else None
        a = Assignment(incident_id=iid, decision=decision, campaign_id=camp, reason_pl="syntetyczne", model="syn@1",
                       candidates=[])
        self.n += 1
        run_id = f"assign-syn-{self.n:05d}"

        def tx(conn: sqlite3.Connection) -> None:
            if fresh:
                self._incident(conn, iid)
            _write_run(conn, run_id, "assign", {"base_run": RUN, "item": f"syn/{iid}"}, {"decision": decision},
                       pairs, None, [a])

        self.writer.run(tx)
        if fresh:
            self.new.append(iid)
        self.log.append(f"assign {iid} {decision} {camp} pairs={len(pairs)}")
        return run_id

    def review(self) -> None:
        r = self.rng
        cands = list(self.pairs)
        if self.new:
            cands += [tuple(sorted((x, r.choice(self.base)))) for x in r.sample(self.new, min(3, len(self.new)))]
        a, b = r.choice(cands) if cands else sorted(r.sample(self.base, 2))
        action = r.choice(("accept_link", "reject_link"))
        rid = f"rv{len(self.reviews):04d}{r.randint(0, 999)}"
        self._review_row(rid, action, {"a": a, "b": b}, None)
        self.reviews.append(rid)
        self.log.append(f"review {action} {a} {b}")

    def undo(self) -> None:
        live = [x for x in self.reviews if not x.startswith("undo")]
        if not live:
            return self.review()
        target = self.rng.choice(live)
        self.reviews.remove(target)
        self._review_row(f"undo{target}", "undo", {}, target)
        self.log.append(f"undo {target}")
        return None

    def _review_row(self, rid: str, action: str, target: dict[str, Any], undoes: str | None) -> None:
        payload = {"id": rid, "action": action, "target": target, "analyst": "syn", "undoes": undoes}

        def tx(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO reviews (id, action, target, analyst, note, undoes, created_at) VALUES"
                         " (?, ?, ?, 'syn', NULL, ?, ?)", (rid, action, events.canonical_json(target), undoes,
                                                           events.utcnow()))
            events.append(conn, "REVIEW", payload)

        self.writer.run(tx)

    def unsure_toggle(self) -> None:
        from hydra.review import mark_unsure, undo_unsure

        if self.unsure and self.rng.random() < 0.5:
            undo_unsure(self.writer, self.unsure.pop())
            self.log.append("unsure undo")
            return
        a, b = self.rng.choice(self.pairs) if self.pairs else sorted(self.rng.sample(self.base, 2))
        try:
            mark, _ = mark_unsure(self.writer, {"a": a, "b": b})
        except ValueError:  # already marked
            return
        self.unsure.append(mark["id"])
        self.log.append(f"unsure {a} {b}")

    def export(self) -> None:
        record_export(self.writer, self.rng.choice(EXPORT_KINDS), RUN, {"incidents": 3})
        self.log.append("export")

    def step(self) -> None:
        r = self.rng.random()
        if r < 0.40 or not self.new:
            self.assign()
        elif r < 0.52:
            self.assign(self.rng.choice(self.new))
        elif r < 0.60:
            self.assign(self.rng.choice(self.base))
        elif r < 0.75:
            self.review()
        elif r < 0.82:
            self.undo()
        elif r < 0.90:
            self.unsure_toggle()
        else:
            self.export()
