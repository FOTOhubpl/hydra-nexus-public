#!/usr/bin/env python3
"""Build, enrich and analyse the PUBLIC demo corpus (public phishing feeds; organiser data never touched).

Stages (run in order; each is idempotent and resumable):

    select   feeds -> host groups -> <out>/groups.jsonl + selection stats
    enrich   polite live lookups (HYDRA_ENRICH=1 semantics) that fill the replay cache for the selected hosts:
             RDAP (IANA bootstrap -> registry server, 1 request/s per registry; rdap.org for the rest),
             crt.sh (3 s between calls), DNS via dig (A/AAAA/CNAME/NS/MX/TXT), Team Cymru ASN. Stops at a
             time budget or a domain limit. Writes <out>/enrich_stats.json.
    analyse  import into the OWN state dir (HYDRA_STATE_DIR), genes inf_url + inf_enrich (replay only) + inf_feed,
             rarity, candidates (exact postings capped at --max-df holders), pair scores, campaigns, groups;
             persisted as a normal `analyse` run, so the API/UI show it. Writes <out>/analyse_stats.json.
    report   aggregate infrastructure patterns (registrar+day, NS pairs, SAN siblings, ASN, digit skeletons) and
             the composition of campaign links by gene tier. Writes <out>/report.json.

Example (server):
    export HYDRA_STATE_DIR=~/HydraNexus/state-public HYDRA_ENRICH_CACHE=~/HydraNexus/cache/enrich-public
    uv run python scripts/build_public_corpus.py select --cert-since 2026-11-18
    uv run python scripts/build_public_corpus.py enrich --max-domains 2500 --budget-min 60
    uv run python scripts/build_public_corpus.py analyse
    uv run python scripts/build_public_corpus.py report

Outputs stay outside git (default <data>/public-corpus/pl-feeds). Only hosts are sent to registries, resolvers and CT
logs; no feed URL is ever requested.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import queue
import resource
import sys
import threading
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hydra.adapters import public_feeds as pf  # noqa: E402  (registers inf_feed)
from hydra.config import Settings  # noqa: E402

log = logging.getLogger("public_corpus")
EXTRACTORS = ["inf_url", "inf_enrich", "inf_feed"]


def rss_gb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1024**3 if sys.platform == "darwin" else 1024**2), 3)


def out_dir(args, settings: Settings) -> Path:
    p = Path(args.out or (settings.data_dir / "public-corpus" / args.name)).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


# --------------------------------------------------------------------------------------- select


def stage_select(args, settings: Settings) -> None:
    data = settings.data_dir
    lab = data / "external-lab"
    t0 = time.perf_counter()
    cert = pf.load_cert_pl(Path(args.cert or data / "external" / "cert" / "domains.json"), since=args.cert_since)
    pt = pf.load_phishtank(Path(args.phishtank or lab / "phishtank" / "online-valid.csv"))
    ph = pf.load_phiusiil(Path(args.phiusiil or lab / "phiusiil" / "PhiUSIIL_Phishing_URL_Dataset.csv"))
    groups = pf.group_by_host([*cert, *pt, *ph])
    if args.max_incidents and len(groups) > args.max_incidents:
        groups = groups[: args.max_incidents]
    o = out_dir(args, settings)
    with open(o / "groups.jsonl", "w") as f:
        for g in groups:
            f.write(json.dumps({"host": g.host, "rows": [r.__dict__ for r in g.rows]}, default=str) + "\n")
    multi = Counter("+".join(g.sources) for g in groups)
    brands = Counter(pf.brand_guess(g.host) or "-" for g in groups)
    stats = {
        "rows": {"cert_pl": len(cert), "phishtank_pl": len(pt), "phiusiil_pl": len(ph)},
        "cert_since": args.cert_since, "incidents": len(groups), "by_sources": dict(multi),
        "brand_guess": dict(brands.most_common(25)), "tld": dict(Counter(g.host.rsplit(".", 1)[-1] for g in groups)
                                                                .most_common(15)),
        "seconds": round(time.perf_counter() - t0, 2), "sources": pf.SOURCES,
    }
    dump(o / "select_stats.json", stats)
    print(json.dumps({k: v for k, v in stats.items() if k != "sources"}, indent=1))


def load_groups(o: Path) -> list[pf.HostGroup]:
    return pf.load_groups_jsonl(o / "groups.jsonl")


# --------------------------------------------------------------------------------------- enrich


def _priority(groups: list[pf.HostGroup]) -> list[pf.HostGroup]:
    """Round-robin over sources (newest first inside each), so a partial run still covers every feed."""
    by: dict[str, list[pf.HostGroup]] = defaultdict(list)
    for g in groups:
        by[g.sources[0] if len(g.sources) == 1 else "multi"].append(g)
    for v in by.values():
        v.sort(key=lambda g: g.first_seen or "", reverse=True)
    order = ["multi", "phishtank", "cert_pl", "phiusiil"]
    lists = [by[k] for k in order if k in by]
    out = []
    for i in range(max(len(x) for x in lists)):
        for x in lists:
            if i < len(x):
                out.append(x[i])
    return out


def rdap_bases(enr_cache: Path) -> dict[str, str]:
    """IANA RDAP bootstrap for domains (tld -> base URL), fetched once and kept next to the cache."""
    from hydra.enrich.client import http_json

    p = enr_cache / "_bootstrap" / "iana_rdap_dns.json"
    if not p.is_file():
        _, status, data = http_json("https://data.iana.org/rdap/dns.json", timeout=30)
        if status != "200" or not isinstance(data, dict):
            return {}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    data = json.loads(p.read_text())
    out = {}
    for tlds, urls in data.get("services", []):
        base = next((u for u in urls if u.startswith("https://")), urls[0] if urls else None)
        if base:
            for t in tlds:
                out[t.lower()] = base if base.endswith("/") else base + "/"
    return out


def stage_enrich(args, settings: Settings) -> None:
    from hydra.enrich import asn, crtsh, dns
    from hydra.enrich.client import Enricher, http_json
    from hydra.enrich.domains import icann_domain, is_boilerplate, is_ip, is_platform, valid_host

    o = out_dir(args, settings)
    cache = Path(settings.enrich_cache)
    groups = _priority(load_groups(o))
    domains: list[str] = []
    hosts: list[tuple[str, str | None]] = []
    seen_d: set[str] = set()
    for g in groups:
        h = g.host
        if not valid_host(h) or is_boilerplate(h):
            continue
        d = None if is_platform(h) else icann_domain(h)
        if d and d not in seen_d:
            if len(seen_d) >= args.max_domains:
                continue
            seen_d.add(d)
            domains.append(d)
        elif d is None and len(hosts) >= args.max_domains:
            continue
        hosts.append((h, d))
    deadline = time.monotonic() + args.budget_min * 60
    stats: dict[str, Counter] = defaultdict(Counter)
    times: dict[str, float] = defaultdict(float)
    lock = threading.Lock()

    def note(src: str, status: str, dt: float, cached: bool) -> None:
        with lock:
            stats[src]["cached" if cached else "live"] += 1
            stats[src][("ok" if status in ("200", "ok", "NXDOMAIN", "NODATA") else
                        "not_found" if status == "404" else "fail")] += 1
            times[src] += dt

    def timed(src: str, enr: Enricher, fn):
        before = enr.stats["live"]
        t = time.perf_counter()
        r = fn()
        resp = r[0] if isinstance(r, tuple) else r
        note(src, resp.status, time.perf_counter() - t, enr.stats["live"] == before)
        return resp

    # RDAP: one worker per registry server (1 request/s each), rdap.org bootstrap redirector for the rest
    bases = rdap_bases(cache) if not args.rdap_org_only else {}
    by_base: dict[str, list[str]] = defaultdict(list)
    for d in domains:
        by_base[bases.get(d.rsplit(".", 1)[-1], "https://rdap.org/")].append(d)

    def rdap_worker(base: str, ds: list[str]) -> None:
        """1 request/s per registry; on HTTP 429/403 back off 30 s and double the gap (max 16 s), so a registry
        that throttles harder than 1/s (measured: zdns .top, dns.pl, GMO .shop) is not hammered."""
        enr = Enricher(cache_dir=cache, live=True)
        gap = 1.0
        for d in ds:
            if time.monotonic() > deadline:
                break
            r = timed("rdap", enr, lambda d=d: enr.get("rdap", d, lambda: http_json(f"{base}domain/{d}")))
            if "http429" in r.status or "http403" in r.status:
                with lock:
                    stats["rdap_backoff"][base] += 1
                gap = min(gap * 2, 16.0)
                time.sleep(30)
            elif r.fetched_at and r.cache_path:
                gap = max(1.0, gap * 0.9)
            if gap > 1.0:
                time.sleep(gap - 1.0)

    crt_q: queue.Queue = queue.Queue()
    for d in domains[: args.max_crtsh]:
        crt_q.put(d)
    crt_enr = Enricher(cache_dir=cache, live=True)

    def crt_worker() -> None:
        while time.monotonic() < deadline:
            try:
                d = crt_q.get_nowait()
            except queue.Empty:
                return
            timed("crtsh", crt_enr, lambda d=d: crtsh.lookup(crt_enr, d))

    dns_q: queue.Queue = queue.Queue()
    done_d: set[str] = set()
    for h, d in hosts:
        dns_q.put((h, d))
    dns_enr = Enricher(cache_dir=cache, live=True)
    seen_ip: set[str] = set()

    def dns_worker() -> None:
        while time.monotonic() < deadline:
            try:
                h, d = dns_q.get_nowait()
            except queue.Empty:
                return
            if d:
                with lock:
                    first = d not in done_d
                    done_d.add(d)
                if first:
                    for t in ("NS", "MX", "TXT"):
                        timed("dns", dns_enr, lambda d=d, t=t: dns.query(dns_enr, d, t))
            ips: list[str] = []
            for t in ("A", "AAAA", "CNAME"):
                r = timed("dns", dns_enr, lambda h=h, t=t: dns.query(dns_enr, h, t))
                if t != "CNAME":
                    ips += [x for x in dns.answers(r) if is_ip(x)]
            for ip in ips:
                with lock:
                    new = ip not in seen_ip
                    seen_ip.add(ip)
                if new:
                    timed("asn", dns_enr, lambda ip=ip: asn.lookup(dns_enr, ip))

    t0 = time.perf_counter()
    threads = [threading.Thread(target=rdap_worker, args=(b, ds), daemon=True) for b, ds in by_base.items()]
    threads += [threading.Thread(target=crt_worker, daemon=True) for _ in range(args.crtsh_workers)]
    threads += [threading.Thread(target=dns_worker, daemon=True) for _ in range(args.dns_workers)]
    for t in threads:
        t.start()
    last = 0.0
    while any(t.is_alive() for t in threads):
        time.sleep(5)
        if time.perf_counter() - last > 60:
            last = time.perf_counter()
            with lock:
                snap = {k: dict(v) for k, v in stats.items()}
            print(f"[{time.perf_counter() - t0:7.0f}s] {json.dumps(snap)}", flush=True)
            dump(o / "enrich_stats.json", {"partial": True, "stats": snap})
    out = {
        "domains_selected": len(domains), "hosts_selected": len(hosts), "rdap_servers": len(by_base),
        "crtsh_queued": min(len(domains), args.max_crtsh), "wall_s": round(time.perf_counter() - t0, 1),
        "budget_min": args.budget_min, "stats": {k: dict(v) for k, v in stats.items()},
        "busy_s": {k: round(v, 1) for k, v in times.items()}, "unique_ips": len(seen_ip),
        "rdap_by_server": {b: len(ds) for b, ds in sorted(by_base.items(), key=lambda x: -len(x[1]))[:15]},
        "delays_s": {"rdap_per_server": 1.0, "crtsh": 3.0, "dns": 0.05},
    }
    dump(o / "enrich_stats.json", out)
    print(json.dumps(out, indent=1))


# --------------------------------------------------------------------------------------- analyse


# Default nameservers of registrars and mass hosters seen in this corpus (not in inf.SHARED_NS): one NS set serves
# every customer that did not change it, so it is platform evidence, not an operator account.
PROVIDER_NS = ("registrar.eu", "openprovider.nl", "openprovider.eu", "dyna-ns.net", "dnsowl.com", "spaceship.net",
               "porkbun.com", "name.com", "namebrightdns.com", "cyberfolks.pl", "nazwa.pl", "home.pl", "ovh.net",
               "hostinger.com", "dns.com", "share-dns.com", "share-dns.net", "gname-dns.com", "dnspod.net",
               "alidns.com", "hichina.com", "webhostbox.net", "bluehost.com", "hostgator.com", "dreamhost.com",
               "aftermarket.pl", "co.pl", "atthost.pl", "lh.pl", "zenbox.pl", "1dns.pl", "domeny.tv")


def retier_provider_ns(writer, tier: str) -> int:
    def is_provider(ns: str) -> bool:
        ns = ns.lower().rstrip(".")
        return any(ns == p or ns.endswith("." + p) for p in PROVIDER_NS)

    def _q(conn) -> list[tuple[int, str, str]]:
        return [(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT id, kind, value FROM genes WHERE kind IN ('ns_host', 'ns_set') AND status = 'observed'"
            " AND dependency_group LIKE 'inf:operator:%'")]

    ids = [gid for gid, _k, v in writer.run(_q, timeout=600)
           if v and all(is_provider(x) for x in v.split(",") if x)]
    grp = f"inf:{tier}:provider_ns"
    writer.run(lambda conn: conn.executemany("UPDATE genes SET dependency_group = ? WHERE id = ?",
                                             [(grp, i) for i in ids]), timeout=600)
    return len(ids)


def retier_cdn_ips(writer, settings: Settings, tier: str) -> int:
    """`dns_ip`/`dns_ip24` are operator tier unless the IP is in a Cloudflare prefix (inf_enrich). Behind other
    shared infrastructure (Firebase/Google, Amazon, Fastly, Akamai, large hosters: `inf.CDN_ASNS`) one IP serves
    thousands of unrelated sites (dry run: 647 same-campaign links held by a shared IP alone, 45 firebaseapp.com
    tenants on one Google address). Look the ASN up in the replay cache and move those genes to `tier`."""
    from hydra.enrich import asn
    from hydra.enrich.client import Enricher
    from hydra.genome.inf import CDN_ASNS

    enr = Enricher(cache_dir=Path(settings.enrich_cache), live=False)

    def _q(conn) -> list[tuple[int, str, str]]:
        return [(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT id, kind, value FROM genes WHERE kind IN ('dns_ip', 'dns_ip24') AND status = 'observed'"
            " AND dependency_group LIKE 'inf:operator:%'")]

    rows = writer.run(_q, timeout=600)
    cdn24: set[str] = set()
    ids: list[int] = []
    private: list[int] = []
    for gid, kind, value in rows:
        if not value:
            continue
        try:  # private / ULA answers (e.g. Cloudflare Tunnel's fd10:aec2:5dae::) are the same for every tenant
            if not ipaddress.ip_network(value, strict=False).is_global:
                private.append(gid)
                continue
        except ValueError:
            pass
        if kind != "dns_ip":
            continue
        _, s = asn.lookup(enr, value)
        if s.get("asn") in CDN_ASNS:
            ids.append(gid)
            if "." in value:
                cdn24.add(".".join(value.split(".")[:3]) + ".0/24")
    ids += [gid for gid, kind, value in rows if kind == "dns_ip24" and value in cdn24]
    grp = f"inf:{tier}:cdn_ip"

    def _u(conn) -> None:
        conn.executemany("UPDATE genes SET dependency_group = ? WHERE id = ?", [(grp, i) for i in ids])
        conn.executemany("UPDATE genes SET dependency_group = 'inf:boilerplate:private_ip' WHERE id = ?",
                         [(i,) for i in private])

    writer.run(_u, timeout=600)
    return len(ids) + len(private)


def stage_analyse(args, settings: Settings) -> None:
    from hydra import pipeline
    from hydra.adapters.nask_folder import load_incident
    from hydra.db import Writer, connect
    from hydra.nexus import cluster
    from hydra.retrieval import candidates as retrieve
    from hydra.storage import ContentStore
    from hydra.synapse import Thresholds, score_pair

    settings.enrich = False  # analysis reads only the replay cache (ADR-007)
    settings.ensure_dirs()
    o = out_dir(args, settings)
    groups = load_groups(o)
    th = Thresholds()
    run_id = pipeline.new_run_id("analyse")
    started = pipeline.events.utcnow()
    timings: dict[str, float] = {}
    writer = Writer(settings.db_path)
    store = ContentStore(settings.cas_dir)
    try:
        t = time.perf_counter()
        incidents = pf.import_groups(groups, writer, store)
        with closing(connect(settings.db_path, readonly=True)) as conn:
            incidents = [load_incident(conn, i.id) or i for i in incidents]
        timings["import"] = time.perf_counter() - t
        t = time.perf_counter()
        ex = pipeline.extract(incidents, writer, store, settings, only=EXTRACTORS)
        timings["genes"] = time.perf_counter() - t
        if args.shape_skeleton_tier != "operator":
            # Shape-only skeletons ('L9.com': label lengths + suffix) chain unrelated random-name domains at this
            # scale (dry run 2026-11-21: top reason of 33k of 37.7k same-campaign links, clusters of 104/85/82
            # domains sharing only 'L17.com'/'L9.com'/'L14.com'). The letter-keeping skeleton measured in the
            # scale lab (P 0.95) is `url_digit_skeleton` (inf_feed, operator tier); the shape-only ones become kit.
            grp = f"inf:{args.shape_skeleton_tier}:url_shape"
            nre = writer.run(lambda conn: conn.execute(
                "UPDATE genes SET dependency_group = ? WHERE kind IN ('url_label_skeleton', 'url_label_path_skeleton')"
                " AND dependency_group LIKE 'inf:operator:%'", (grp,)).rowcount, timeout=600)
            log.warning("re-tiered %d shape-only skeleton genes to %s", nre, grp)
        if args.cdn_ip_tier != "operator":
            n_ip = retier_cdn_ips(writer, settings, args.cdn_ip_tier)
            log.warning("re-tiered %d dns_ip/dns_ip24 genes inside CDN/hosting ASNs to %s", n_ip, args.cdn_ip_tier)
            n_ns = retier_provider_ns(writer, args.cdn_ip_tier)
            log.warning("re-tiered %d ns_host/ns_set genes of registrar/hoster default NS to %s", n_ns,
                        args.cdn_ip_tier)
        t = time.perf_counter()
        ids = [i.id for i in incidents]
        with closing(connect(settings.db_path, readonly=True)) as conn:
            genomes = pipeline.load_genomes(conn, ids)
        timings["load_genomes"] = time.perf_counter() - t
        t = time.perf_counter()
        rarity = pipeline.rarity_for(genomes)
        timings["rarity"] = time.perf_counter() - t
        t = time.perf_counter()
        cand = retrieve(genomes, rarity, max_df=args.max_df)
        timings["candidates"] = time.perf_counter() - t
        t = time.perf_counter()
        pairs = {k: score_pair(genomes[k[0]], genomes[k[1]], rarity, th, sorted(src)) for k, src in cand.items()}
        timings["pair_scoring"] = time.perf_counter() - t
        t = time.perf_counter()
        clustering = cluster(genomes, pairs, rarity, th)
        timings["clustering"] = time.perf_counter() - t
        hyp = Counter(str(pe.score.hypothesis) for pe in pairs.values())
        metrics = {
            "corpus": "public", "incidents": len(ids), "genes": sum(len(g.genes) for g in genomes.values()),
            "pairs_scored": len(pairs), "candidate_pairs": len(cand), "hypotheses": dict(hyp),
            "campaigns": len(clustering.campaigns),
            "multi_member_campaigns": sum(1 for c in clustering.campaigns if len(c.members) > 1),
            "campaign_groups": [pipeline.redact_obj(g.model_dump()) for g in clustering.groups],
            "alerts": clustering.alerts, "scorer": "rarity_bits@1", "max_df": args.max_df,
            "shape_skeleton_tier": args.shape_skeleton_tier, "cdn_ip_tier": args.cdn_ip_tier,
            "extract_cached": sum(1 for v in ex.values() if v.get("cached")),
            "timings_s": {k: round(v, 3) for k, v in timings.items()},
            "sources": pf.SOURCES,
        }
        params = {"corpus": "public", "groups": str(o / "groups.jsonl"), "only": EXTRACTORS,
                  "thresholds": th.__dict__, "max_df": args.max_df,
                  "shape_skeleton_tier": args.shape_skeleton_tier, "cdn_ip_tier": args.cdn_ip_tier}
        t = time.perf_counter()
        writer.run(lambda conn: pipeline._write_run(conn, run_id, "analyse", params, metrics, pairs, clustering,
                                                    created_at=started), timeout=1800)
        timings["persist"] = time.perf_counter() - t
    finally:
        writer.close()
    sizes = sorted((len(c.members) for c in clustering.campaigns), reverse=True)
    stats = {
        "run_id": run_id, "state_dir": str(settings.state_dir), "incidents": len(ids),
        "genes": metrics["genes"], "candidate_pairs": len(cand), "hypotheses": dict(hyp),
        "campaigns_total": len(clustering.campaigns), "multi_member_campaigns": metrics["multi_member_campaigns"],
        "incidents_in_multi": sum(s for s in sizes if s > 1), "largest_campaigns": sizes[:15],
        "campaign_groups": len(clustering.groups),
        "group_sizes": sorted((len(g.campaign_ids) for g in clustering.groups), reverse=True)[:10],
        "alerts": clustering.alerts[:10], "timings_s": {k: round(v, 2) for k, v in timings.items()},
        "wall_s": round(sum(timings.values()), 1), "peak_rss_gb": rss_gb(), "max_df": args.max_df,
    }
    dump(o / "analyse_stats.json", stats)
    print(json.dumps(stats, indent=1))


# --------------------------------------------------------------------------------------- report


INFRA_KINDS = ["registrar_day", "registration_day", "registrar", "ns_set", "ns_host", "cert_san_sibling",
               "cert_san_domain", "cert_issuer", "asn", "as_org", "dns_ip24", "url_digit_skeleton",
               "url_label_skeleton", "url_brand", "cert_pl_listed_host", "domain_age_at_observation"]


def stage_report(args, settings: Settings) -> None:
    from hydra import pipeline
    from hydra.db import connect

    o = out_dir(args, settings)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        run_id = pipeline.latest_run(conn)
        df: dict[str, Counter] = defaultdict(Counter)
        cov: dict[str, set] = defaultdict(set)
        status: dict[str, Counter] = defaultdict(Counter)
        op_df: dict[str, Counter] = defaultdict(Counter)
        listed: set[str] = set()
        for r in conn.execute("SELECT incident_id, kind, value, status, dependency_group FROM genes"):
            status[r["kind"]][r["status"]] += 1
            if r["status"] == "observed" and r["value"] is not None:
                cov[r["kind"]].add(r["incident_id"])
                df[r["kind"]][r["value"]] += 1
                if str(r["dependency_group"] or "").startswith("inf:operator:"):
                    op_df[r["kind"]][r["value"]] += 1
                if r["kind"] == "cert_pl_listed_host":
                    listed.add(r["incident_id"])
        src = Counter(r[0] for r in conn.execute("SELECT source FROM incidents"))
        n_inc = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        camps = [json.loads(r["detail"]) for r in conn.execute("SELECT detail FROM campaigns WHERE run_id = ?",
                                                                (run_id,))]
        reason_kinds: Counter = Counter()
        reason_tiers: Counter = Counter()
        same = 0
        for r in conn.execute("SELECT detail FROM pairs WHERE run_id = ? AND hypothesis = 'same_campaign'",
                              (run_id,)):
            same += 1
            d = json.loads(r["detail"])
            ks = {x["kind"] for x in d.get("reasons", []) if x.get("bits", 0) > 0}
            for k in ks:
                reason_kinds[k] += 1
            reason_tiers[" + ".join(sorted(ks)) or "-"] += 1
    cov_rows = {k: {"incidents": len(cov[k]), "distinct": len(df[k]),
                    "shared_values": sum(1 for v in df[k].values() if v > 1),
                    "max_df": max(df[k].values()) if df[k] else 0, "status": dict(status[k])}
                for k in INFRA_KINDS if k in status}
    top = {k: df[k].most_common(args.top) for k in INFRA_KINDS if k in df}
    multi = sorted((c for c in camps if len(c["members"]) > 1), key=lambda c: -len(c["members"]))
    top_camps = []
    for c in multi[: args.top]:
        core = [{"kind": g["kind"], "value": g.get("value_a"), "doc_freq": g.get("doc_freq"),
                 "bits": round(g.get("bits", 0), 1)} for g in c.get("core_genome", [])[:8]]
        top_camps.append({"id": c["id"], "size": len(c["members"]), "label": c["label"], "first": c.get("first_seen"),
                          "last": c.get("last_seen"), "core": core})
    # Cloudflare check: share of same-campaign pairs whose positive reasons are only ASN / NS / IP kinds
    def only(kinds: tuple[str, ...]) -> int:
        return sum(v for k, v in reason_tiers.items() if k != "-" and all(x in kinds for x in k.split(" + ")))

    cdn_only = only(("asn", "as_org", "bgp_prefix", "dns_ip", "dns_ip24"))
    ns_only = only(("ns_host", "ns_set"))
    # CERT Polska pivot: unlisted hosts that share a campaign with >= 1 listed host (candidates, not verdicts)
    piv_c = piv_new = 0
    for c in multi:
        ms = set(c["members"])
        if ms & listed and ms - listed:
            piv_c += 1
            piv_new += len(ms - listed)
    top_op = {k: op_df[k].most_common(args.top) for k in ("registrar_day", "ns_set", "cert_san_sibling",
                                                          "dns_cname", "url_digit_skeleton", "dns_ip",
                                                          "registrable_domain", "url_path_skeleton") if k in op_df}
    rep = {"run_id": run_id, "incidents": n_inc, "sources": dict(src), "coverage": cov_rows, "top_values": top,
           "top_operator_values": top_op, "cert_pl_listed_incidents": len(listed),
           "cert_pivot": {"campaigns_mixed": piv_c, "unlisted_hosts_linked": piv_new},
           "multi_member_campaigns": len(multi), "incidents_in_multi": sum(len(c["members"]) for c in multi),
           "same_campaign_pairs": same, "reason_kinds": dict(reason_kinds.most_common(25)),
           "reason_combos": dict(reason_tiers.most_common(20)), "cdn_only_links": cdn_only, "ns_only_links": ns_only,
           "top_campaigns": top_camps,
           "campaign_size_hist": dict(Counter(min(len(c["members"]), 50) for c in camps))}
    dump(o / "report.json", rep)
    print(json.dumps({k: rep[k] for k in ("run_id", "incidents", "same_campaign_pairs", "reason_kinds",
                                          "reason_combos", "cdn_only_links", "ns_only_links",
                                          "cert_pivot", "sources", "multi_member_campaigns")}, indent=1))
    for c in top_camps[:10]:
        print(c["size"], c["label"][:80], [(x["kind"], x["value"], x["doc_freq"]) for x in c["core"][:4]])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["select", "enrich", "analyse", "report"])
    ap.add_argument("--name", default=pf.CORPUS_NAME, help="corpus name (dir <data>/public-corpus/<name>)")
    ap.add_argument("--out", help="working dir outside git (default <data>/public-corpus/<name>)")
    ap.add_argument("--cert"), ap.add_argument("--phishtank"), ap.add_argument("--phiusiil")
    ap.add_argument("--cert-since", default="2026-11-18", help="CERT Polska InsertDate lower bound (ISO)")
    ap.add_argument("--max-incidents", type=int, default=10_000)
    ap.add_argument("--max-domains", type=int, default=2500)
    ap.add_argument("--max-crtsh", type=int, default=2500)
    ap.add_argument("--budget-min", type=float, default=60.0)
    ap.add_argument("--crtsh-workers", type=int, default=2)
    ap.add_argument("--dns-workers", type=int, default=6)
    ap.add_argument("--rdap-org-only", action="store_true")
    ap.add_argument("--max-df", type=int, default=200, help="posting cap for candidate generation (scale lab D)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--shape-skeleton-tier", default="kit", choices=["operator", "platform", "kit"],
                    help="tier of the shape-only url_label(_path)_skeleton genes in this corpus (default kit)")
    ap.add_argument("--cdn-ip-tier", default="platform", choices=["operator", "platform", "kit", "boilerplate"],
                    help="tier of dns_ip/dns_ip24 genes whose ASN is CDN/shared hosting (inf.CDN_ASNS)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "WARNING"))
    settings = Settings()
    {"select": stage_select, "enrich": stage_enrich, "analyse": stage_analyse, "report": stage_report}[args.stage](
        args, settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
