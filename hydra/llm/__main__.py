"""CLI for the optional LLM layer.  `. scripts/env.sh && uv run python -m hydra.llm <cmd>`

    status                              configuration (no credentials)
    narrative [--run R] [ids...]        run llm_narrative@1 on stored incidents and append its genes
    brief [--run R] CAMPAIGN_ID|nask-N  campaign brief (Polish, grounded, validated)
    pair [--run R] A B                  pair explanation
    measure [--run R] [--campaign ID]   narrative on all incidents of the run + briefs; prints aggregates:
                                        latency, tokens, estimated cost, cache hits, llm vs rule scheme agreement

Live calls need HYDRA_LLM=1 and AWS credentials for Bedrock; without them everything is served from the cache
or reported as unavailable.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from typing import Any

from hydra.config import Settings
from hydra.db import Writer, connect
from hydra.llm import brief, status
from hydra.llm.client import get_client


def _conn(s: Settings) -> sqlite3.Connection:
    c = connect(s.db_path, readonly=True)
    c.row_factory = sqlite3.Row
    return c


def _run(s: Settings, run: str | None) -> str:
    if run:
        return run
    with closing(_conn(s)) as c:
        r = c.execute("SELECT id FROM runs WHERE kind='analyse' ORDER BY finished_at IS NULL, created_at DESC"
                      " LIMIT 1").fetchone()
    if r is None:
        sys.exit("no analyse run in the store")
    return r["id"]


def _campaign_of(s: Settings, run: str, ref: str) -> str:
    with closing(_conn(s)) as c:
        if c.execute("SELECT 1 FROM campaigns WHERE run_id=? AND id=?", (run, ref)).fetchone():
            return ref
        r = c.execute("SELECT campaign_id FROM campaign_members WHERE run_id=? AND incident_id=?",
                      (run, ref)).fetchone()
    if r is None:
        sys.exit(f"no campaign {ref!r} in run {run}")
    return r["campaign_id"]


def _incidents(s: Settings, run: str | None) -> list[str]:
    with closing(_conn(s)) as c:
        if run:
            ids = [r[0] for r in c.execute("SELECT DISTINCT incident_id FROM campaign_members WHERE run_id=?", (run,))]
            if ids:
                return sorted(ids)
        return sorted(r[0] for r in c.execute("SELECT id FROM incidents"))


def _narrative(s: Settings, ids: list[str]) -> list[dict[str, Any]]:
    from hydra.llm.narrative import annotate
    from hydra.storage import ContentStore

    w = Writer(s.db_path)
    try:
        return annotate(ids, s, w, ContentStore(s.cas_dir))
    finally:
        w.close()


def _agreement(s: Settings, ids: list[str]) -> dict[str, Any]:
    rows = []
    with closing(_conn(s)) as c:
        for iid in ids:
            rule = sorted({r[0] for r in c.execute(
                "SELECT value FROM genes WHERE incident_id=? AND kind='scheme' AND status='observed'", (iid,))})
            llm = [r[0] for r in c.execute(
                "SELECT value FROM genes WHERE incident_id=? AND kind='llm_scheme' AND status='observed'", (iid,))]
            rows.append({"incident_id": iid, "rule": rule, "llm": llm[0] if llm else None,
                         "agree": bool(llm) and llm[0] in rule})
    both = [r for r in rows if r["llm"] and r["rule"]]
    return {"compared": len(both), "agree": sum(r["agree"] for r in both), "rows": rows}


def _stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    live = [x for x in items if x.get("status") == "ok" and not x.get("cached")]
    lat = [float(x.get("latency_s") or 0) for x in live]
    return {
        "calls": len(items), "ok": sum(x.get("status") == "ok" for x in items),
        "cache_hits": sum(bool(x.get("cached")) for x in items), "live": len(live),
        "latency_s_median": _median(lat), "latency_s_max": max(lat) if lat else None,
        "input_tokens": sum(int((x.get("usage") or {}).get("input_tokens", 0)) for x in live),
        "output_tokens": sum(int((x.get("usage") or {}).get("output_tokens", 0)) for x in live),
        "cost_usd_estimated": round(sum(float(x.get("cost_usd") or 0) for x in live), 5),
        **{f"{k}_median": _median([float((x.get("usage") or {})[k]) for x in live if (x.get("usage") or {}).get(k)])
           for k in ("prompt_tps", "gen_tps")},
    }


def _median(v: list[float]) -> float | None:
    v = sorted(v)
    if not v:
        return None
    return round(v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2, 2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hydra.llm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("narrative")
    p.add_argument("--run")
    p.add_argument("ids", nargs="*")
    p = sub.add_parser("brief")
    p.add_argument("--run")
    p.add_argument("campaign")
    p = sub.add_parser("pair")
    p.add_argument("--run")
    p.add_argument("a")
    p.add_argument("b")
    p = sub.add_parser("measure")
    p.add_argument("--run")
    p.add_argument("--campaign", action="append", default=[])
    p.add_argument("--pair", action="append", default=[], help="a,b (e.g. nask-48,nask-61)")
    p.add_argument("--skip-narrative", action="store_true")
    a = ap.parse_args(argv)
    s = Settings()
    if a.cmd == "status":
        print(json.dumps(status(), indent=1))
        return 0
    run = _run(s, a.run)
    if a.cmd == "narrative":
        print(json.dumps(_narrative(s, a.ids or _incidents(s, run)), ensure_ascii=False, indent=1))
    elif a.cmd == "brief":
        card = brief.campaign_card(s, run, _campaign_of(s, run, a.campaign))
        print(json.dumps(brief.brief_for_campaign(card, settings=s), ensure_ascii=False, indent=1))
    elif a.cmd == "pair":
        print(json.dumps(brief.explain_pair(brief.pair_body(s, run, a.a, a.b), settings=s), ensure_ascii=False,
                         indent=1))
    elif a.cmd == "measure":
        ids = _incidents(s, run)
        nar = [] if a.skip_narrative else _narrative(s, ids)
        briefs = [brief.brief_for_campaign(brief.campaign_card(s, run, _campaign_of(s, run, c)), settings=s)
                  for c in (a.campaign or [])]
        pairs = [brief.explain_pair(brief.pair_body(s, run, *pp.split(",")), settings=s) for pp in a.pair]
        brief_calls = [x for b in briefs for x in b.get("attempts", [])]
        pair_calls = [x for b in pairs for x in b.get("attempts", [])]
        out = {"run_id": run, "incidents": len(ids), "llm": status(), "narrative": _stats(nar),
               "narrative_status": {st: sum(x.get("status") == st for x in nar)
                                    for st in {x.get("status") for x in nar}},
               "scheme_agreement": _agreement(s, ids), "briefs": _stats(brief_calls),
               "brief_pass": {"first_attempt": sum(len(b["attempts"]) == 1 and b["status"] == "ok" for b in briefs),
                              "ok": sum(b["status"] == "ok" for b in briefs), "n": len(briefs),
                              "attempts": [len(b["attempts"]) for b in briefs]},
               "pairs": _stats(pair_calls),
               "pair_pass": {"first_attempt": sum(len(b["attempts"]) == 1 and b["status"] == "ok" for b in pairs),
                             "ok": sum(b["status"] == "ok" for b in pairs), "n": len(pairs),
                             "attempts": [len(b["attempts"]) for b in pairs]},
               "pair_texts": [{"pair": f"{b.get('a')}|{b.get('b')}", "status": b["status"], "text": b.get("text_pl"),
                               "validation": b.get("validation")} for b in pairs],
               "json_valid": {"narrative": f"{sum(x.get('status') == 'ok' for x in nar)}/{len(nar)}"},
               "brief_texts": [{"campaign_id": b["campaign_id"], "status": b["status"], "attempts": len(b["attempts"]),
                                "validation": b.get("validation"), "facts": len(b["facts"]), "text": b.get("text_pl"),
                                "citations": b.get("citations"),
                                "reason_pl": b.get("reason_pl")} for b in briefs],
               "client_stats": get_client(s).stats}
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
