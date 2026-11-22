"""Synthetic v2 rows for API tests and UI development (never shipped, never shown as a result).

Ids are prefixed `syn-` and the run kind is `analyse`, params `{"synthetic": true}`, so a synthetic DB can
never be mistaken for the organiser run. Rows are built from the contracts models, so shapes stay valid.

Usage for UI work on a throwaway state dir:
    HYDRA_STATE_DIR=/tmp/hydra-syn uv run python -m tests.ui_api.synthetic
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw

from hydra import events
from hydra.contracts import (
    Assignment,
    AssignmentCandidate,
    Campaign,
    EnrichmentRecord,
    EvidencePointer,
    Gene,
    GeneMatch,
    PairScore,
)
from hydra.db import Writer
from hydra.storage import ContentStore

RUN_ID = "syn-run-1"
ASSIGN_RUN_ID = "syn-assign-1"  # mirrors CORE: assignments live in `assign` runs with params.base_run
EVAL_RUN_ID = "syn-eval-1"


def _png(color: tuple[int, int, int], text: str) -> bytes:
    im = Image.new("RGB", (320, 240), color)
    d = ImageDraw.Draw(im)
    d.rectangle((20, 160, 300, 210), fill=(250, 250, 250))
    d.text((30, 175), text, fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


INCIDENTS = [
    # id, title, observed_at, colour, campaign
    ("syn-1", "Fałszywy artykuł: inwestycja w energię", "2026-10-01T10:00:00+00:00", (40, 120, 214), "syn-c1"),
    ("syn-2", "Fałszywy artykuł: energia (wariant)", "2026-10-03T09:00:00+00:00", (60, 130, 220), "syn-c1"),
    ("syn-3", "Platforma inwestycyjna – znany prezenter", "2026-10-05T12:00:00+00:00", (80, 140, 200), "syn-c1"),
    ("syn-4", "Cudowny lek na stawy", "2026-10-07T08:00:00+00:00", (220, 100, 50), "syn-c2"),
    ("syn-5", "Cudowny lek na stawy – nowa domena", "2026-10-09T08:00:00+00:00", (230, 110, 60), "syn-c2"),
    ("syn-6", "Kasyno online – bonus", None, (30, 170, 120), None),
]

PAIRS = [
    # a, b, family_bits, prob, hypothesis
    ("syn-1", "syn-2", {"VIS": 6.2, "TXT": 8.1, "INF": 12.4}, 0.991, "same_campaign"),
    ("syn-1", "syn-3", {"VIS": 3.0, "INF": 9.5}, 0.93, "same_campaign"),
    ("syn-2", "syn-3", {"TXT": 2.1, "INF": 4.0}, 0.62, "abstain"),
    ("syn-4", "syn-5", {"VIS": 9.3, "TXT": 5.5, "INF": -3.0}, 0.88, "same_campaign"),
    ("syn-3", "syn-4", {"VIS": 1.2}, 0.08, "shared_template"),
    ("syn-5", "syn-6", {"TXT": 0.6}, 0.03, "independent"),
]


def _gm(fam: str, kind: str, va: str | None, vb: str | None, bits: float, df: int | None, pl: str,
        prov: str = "OBS") -> GeneMatch:
    return GeneMatch(family=fam, kind=kind, value_a=va, value_b=vb, bits=bits, doc_freq=df, provenance=prov,
                     explanation_pl=pl)


def _genes(inc_id: str, campaign: str | None, art: str) -> list[Gene]:
    ev = EvidencePointer(artifact_id=art)
    dom = {"syn-c1": "energia-zysk.example", "syn-c2": "zdrowe-stawy.example"}.get(campaign or "", "kasyno.example")
    if inc_id == "syn-5":
        dom = "stawy-zdrowie.example"
    g = [
        Gene(incident_id=inc_id, family="VIS", kind="pdq", value=f"{inc_id}-pdq", evidence=ev, extractor="pdq@1"),
        Gene(incident_id=inc_id, family="TXT", kind="ocr_line", value=f"Zarabiaj z {inc_id}", evidence=ev,
             provenance="INF", extractor="rapidocr@6"),
        Gene(incident_id=inc_id, family="INF", kind="domain", value=dom, extractor="har_hosts@1",
             evidence=EvidencePointer(artifact_id=art, har_entry=0, enrichment_key=f"rdap|{dom}")),
        Gene(incident_id=inc_id, family="INF", kind="tracker_id", value="fbq:123456789" if campaign else None,
             status="observed" if campaign else "unavailable", extractor="dom_trackers@1"),
        Gene(incident_id=inc_id, family="ID", kind="face", value=None, status="not_applicable",
             extractor="sface@1"),
    ]
    if campaign == "syn-c1":
        g.append(Gene(incident_id=inc_id, family="ID", kind="person_named", value="Prezenter X", provenance="INF",
                      extractor="face_ref@1", quality=0.8))
    return g


def seed(db_path: Path, cas_dir: Path, *, with_eval: bool = True) -> None:
    store = ContentStore(cas_dir)
    w = Writer(db_path)
    now = events.utcnow()
    try:
        arts: dict[str, str] = {}
        blobs: dict[str, bytes] = {}
        for inc_id, title, _obs, color, _c in INCIDENTS:
            data = _png(color, title[:40])
            digest = store.put_bytes(data)
            arts[inc_id] = digest
            blobs[inc_id] = data

        def tx(conn: sqlite3.Connection) -> None:
            for inc_id, title, obs, _color, camp in INCIDENTS:
                d = arts[inc_id]
                conn.execute(
                    "INSERT OR IGNORE INTO artifacts (id, mime, size, source_ref, meta, created_at)"
                    " VALUES (?, 'image/png', ?, ?, '{}', ?)", (d, len(blobs[inc_id]), f"{inc_id}/ad.png", now))
                conn.execute(
                    "INSERT INTO incidents (id, source, source_ref, title, observed_at, label, meta, created_at)"
                    " VALUES (?, 'synthetic', ?, ?, ?, ?, '{}', ?)",
                    (inc_id, f"synthetic/{inc_id}", title, obs, f"TRUTH-{camp}", now))
                conn.execute(
                    "INSERT INTO incident_artifacts (incident_id, artifact_id, role, path_in_incident, meta)"
                    " VALUES (?, ?, 'ad_creative', 'ad.png', '{}')", (inc_id, d))
                for gene in _genes(inc_id, camp, d):
                    conn.execute(
                        "INSERT INTO genes (incident_id, family, kind, value, vector_ref, quality, status, provenance,"
                        " evidence, extractor, dependency_group, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (gene.incident_id, gene.family, gene.kind, gene.value, gene.vector_ref, gene.quality,
                         gene.status, gene.provenance, events.canonical_json(gene.evidence.model_dump()),
                         gene.extractor, gene.dependency_group, now))
            for dom in ("energia-zysk.example", "zdrowe-stawy.example"):
                rec = EnrichmentRecord(key=f"rdap|{dom}", source="rdap", query=dom, fetched_at=now,
                                       status="observed", summary={"registrar": "Example Registrar",
                                                                   "created": "2026-09-28"})
                conn.execute(
                    "INSERT INTO enrichment (key, source, query, fetched_at, status, summary) VALUES (?,?,?,?,?,?)",
                    (rec.key, rec.source, rec.query, rec.fetched_at, rec.status, events.canonical_json(rec.summary)))
            conn.execute(
                "INSERT INTO runs (id, kind, params, metrics, created_at, finished_at)"
                " VALUES (?, 'analyse', ?, '{}', ?, ?)",
                (RUN_ID, events.canonical_json({"synthetic": True}), now, now))
            for a, b, fb, p, hyp in PAIRS:
                contra = [_gm("INF", "domain", "zdrowe-stawy.example", "stawy-zdrowie.example", -3.0, 1,
                              "Różne domeny docelowe")] if a == "syn-4" else []
                reasons = [_gm(f, "x", "a", "b", v, 2, f"Wspólny gen rodziny {f}") for f, v in fb.items() if v > 0]
                score = PairScore(a=a, b=b, family_bits=fb, missing_families=["ID"], contradictions=contra,
                                  reasons=sorted(reasons, key=lambda r: -abs(r.bits)), total_bits=sum(fb.values()),
                                  probability=p, calibrated=False, hypothesis=hyp, candidate_sources=["exact"],
                                  model="rarity_bits@1")
                conn.execute(
                    "INSERT INTO pairs (a, b, run_id, total_bits, probability, calibrated, hypothesis, model, detail)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (a, b, RUN_ID, score.total_bits, p, 0, hyp, score.model, score.model_dump_json()))
            camps = {
                "syn-c1": ("Fałszywe inwestycje: energia", ["syn-1", "syn-2", "syn-3"], "investment"),
                "syn-c2": ("Suplementy: stawy", ["syn-4", "syn-5"], "health"),
            }
            for cid, (label, members, scheme) in camps.items():
                camp = Campaign(id=cid, label=label, members=members, scheme=scheme, confidence=0.9,
                                core_genome=[_gm("INF", "tracker_id", "fbq:123456789", None, 7.5, len(members),
                                                 "Ten sam identyfikator piksela")])
                conn.execute(
                    "INSERT INTO campaigns (id, run_id, label, confidence, status, detail) VALUES (?,?,?,?,?,?)",
                    (cid, RUN_ID, label, camp.confidence, camp.status, camp.model_dump_json()))
                for m in members:
                    conn.execute("INSERT INTO campaign_members (run_id, campaign_id, incident_id) VALUES (?,?,?)",
                                 (RUN_ID, cid, m))
            asg = Assignment(incident_id="syn-6", decision="new_campaign", reason_pl="Brak kampanii z poparciem",
                             model="assign@1", candidates=[AssignmentCandidate(campaign_id="syn-c2", probability=0.03)])
            conn.execute(
                "INSERT INTO runs (id, kind, params, metrics, created_at, finished_at)"
                " VALUES (?, 'assign', ?, ?, ?, ?)",
                (ASSIGN_RUN_ID, events.canonical_json({"base_run": RUN_ID, "item": "synthetic/syn-6"}),
                 events.canonical_json({"decision": asg.decision}), now, now))
            conn.execute(
                "INSERT INTO assignments (incident_id, run_id, decision, campaign_id, detail, created_at)"
                " VALUES (?,?,?,?,?,?)", ("syn-6", ASSIGN_RUN_ID, asg.decision, None, asg.model_dump_json(), now))
            if with_eval:
                # mirrors eval/run.py ui_metrics(res) (feat/eval): nested per-corpus blocks, no per-row lists
                pw = {"precision": 0.9, "recall": 0.6, "f1": 0.72, "fp": 1}
                metrics = {
                    "synthetic": True,
                    "meta": {"backend": "synthetic fixture"},
                    "headline": {"backend": "synthetic fixture", "crashes": 0,
                                 "sample": {"campaign_pairwise_f1": 0.72, "loo_accuracy": 0.75,
                                            "open_set_rejection": 0.5}},
                    "system": {"sample": {"grouping": {"campaign": {"pairwise": pw}},
                                          "loo": {"summary": {"accuracy": 0.75}},
                                          "open_set": {"summary": {"rejection_rate": 0.5, "n": 2}},
                                          "hard_negatives": {"pairs": 1, "violations_campaign": 0}}},
                    "ladder": {"B0_phash": {"sample": {"campaign_pairwise": {**pw, "f1": 0.5}, "threshold": 10}},
                               "B3_rarity": {"sample": {"campaign_pairwise": pw, "threshold": 22}}},
                    "ablation": {"corpus": "sample", "full": {"loo_accuracy": 0.75},
                                 "-INF": {"loo_accuracy": 0.5, "delta_loo_accuracy": 0.25}},
                    "robustness": {"drop_har": {"n": 2, "accuracy": 0.5}},
                }
                conn.execute(
                    "INSERT INTO runs (id, kind, params, metrics, created_at, finished_at)"
                    " VALUES (?, 'eval', ?, ?, ?, ?)",
                    (EVAL_RUN_ID, events.canonical_json({"synthetic": True}), events.canonical_json(metrics), now, now))
            events.append(conn, "SYNTHETIC_SEED", {"run_id": RUN_ID})

        w.run(tx)
    finally:
        w.close()


if __name__ == "__main__":
    import os
    import sys

    from hydra.config import Settings

    if not os.environ.get("HYDRA_STATE_DIR") or "HydraNexus/state" in os.environ["HYDRA_STATE_DIR"]:
        sys.exit("refusing: set HYDRA_STATE_DIR to a throwaway directory (never the real state)")
    s = Settings()
    s.ensure_dirs()
    seed(s.db_path, s.cas_dir)
    print(f"seeded synthetic rows into {s.db_path}")
