"""Generate the synthetic WATCH-RT replay fixture (hydra/watch/rt/fixtures/demo) and large synthetic feeds for the
matching benchmark. Every host is under the reserved .example / .test TLDs (RFC 2606): nothing here is real data.

    uv run python scripts/watch_rt_fixture.py demo            # rewrite the committed demo fixture
    uv run python scripts/watch_rt_fixture.py bench --n 10000 --out /tmp/watch-bench
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "hydra" / "watch" / "rt" / "fixtures"
WORDS = ["zysk", "bonus", "inwestycje", "orlen", "baltyk", "energia", "kryptowaluty", "oszczednosci", "dywidenda",
         "paczka", "doplata", "zwrot", "podatek", "loteria", "nagroda", "portfel", "emerytura", "lokata"]
T0 = datetime(2026, 11, 20, 4, 0, tzinfo=UTC)  # in the past: replay never shows future-dated items

WATCHLIST = [
    {"kind": "host", "value": "promo.zysk-orlen-demo.example", "label": "SYNTETYCZNA kampania A (inwestycje)"},
    {"kind": "domain", "value": "inwestuj-baltyk-demo.example", "label": "SYNTETYCZNA kampania B (energia)"},
    {"kind": "phone", "value": "+48 512 000 111", "label": "SYNTETYCZNA kampania A (infolinia operatora)"},
    {"kind": "wallet", "value": "0x52908400098527886E0F7030069857D2E4169EE7", "label": "SYNTETYCZNA kampania C"},
    {"kind": "tracker", "value": "G-DEMO12345", "label": "SYNTETYCZNA kampania B (tracker)"},
    {"kind": "page_id", "value": "100000000000001", "label": "SYNTETYCZNA kampania A (strona reklamodawcy)"},
    {"kind": "url", "value": "https://paczka-doplata-demo.test/pay/", "label": "SYNTETYCZNA kampania D (paczki)"},
]


def host(rng: random.Random, tld: str = "example") -> str:
    return f"{rng.choice(WORDS)}-{rng.choice(WORDS)}{rng.randint(1, 999)}.{tld}"


def cert_line(h: str, t: datetime, ref: str) -> str:
    return json.dumps({"RegisterPositionId": ref, "DomainAddress": h, "ActionType": "block",
                       "ActionTime": t.strftime("%Y-%m-%dT%H:%M:%S")})


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def demo() -> None:
    rng = random.Random(20261122)
    d = ROOT / "demo"
    d.mkdir(parents=True, exist_ok=True)
    for p in d.iterdir():
        p.unlink()
    ticks: list[dict[str, str]] = []
    ref = 900000
    for i in range(12):
        t = T0 + timedelta(minutes=i)
        tick: dict[str, str] = {}
        lines = []
        for _ in range(rng.randint(3, 7)):
            ref += 1
            lines.append(cert_line(host(rng), t, f"syn-{ref}"))
        if i == 2:
            lines.append(cert_line("promo.zysk-orlen-demo.example", t, "syn-hit-1"))
        if i == 5:
            lines.append(cert_line("login.inwestuj-baltyk-demo.example", t, "syn-hit-2"))
        if i == 9:
            lines.append(cert_line("promo.zysk-orlen-demo.example", t, "syn-hit-1"))  # duplicate: dedup by id
        (d / f"t{i:02d}-cert.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        tick["cert-pl"] = f"t{i:02d}-cert.log"
        if i in (1, 4, 7, 10):
            urls = [{"id": str(70000 + i * 10 + k), "url": f"http://{host(rng, 'test')}/bins/{rng.randint(1, 99)}.sh",
                     "url_status": "online", "threat": "malware_download", "tags": ["elf", "mirai"],
                     "date_added": t.strftime("%Y-%m-%d %H:%M:%S UTC"), "urlhaus_reference": None}
                    for k in range(rng.randint(2, 5))]
            if i == 4:
                urls.append({"id": "70999", "url": "https://paczka-doplata-demo.test/pay/", "url_status": "online",
                             "threat": "malware_download", "tags": ["phishing"],
                             "date_added": t.strftime("%Y-%m-%d %H:%M:%S UTC")})
            (d / f"t{i:02d}-urlhaus.json").write_text(json.dumps({"query_status": "ok", "urls": urls}, indent=1),
                                                       encoding="utf-8")
            tick["urlhaus"] = f"t{i:02d}-urlhaus.json"
        if i in (0, 6):
            base = [f"https://{host(random.Random(k), 'example')}/login" for k in range(8)]
            extra = [f"https://{host(rng)}/verify" for _ in range(4)] if i == 6 else []
            if i == 6:
                extra.append("https://cdn.demo-tracker.example/l?id=G-DEMO12345")
            (d / f"t{i:02d}-openphish.txt").write_text("\n".join(base + extra) + "\n", encoding="utf-8")
            tick["openphish"] = f"t{i:02d}-openphish.txt"
        if i in (3, 8):
            certs = [{"id": 8000000000 + i * 100 + k, "issuer_name": "C=US, O=Let's Encrypt, CN=R11",
                      "name_value": f"{w}.zysk-orlen-{rng.randint(1, 99)}.example\nwww.{w}.zysk-orlen.example",
                      "entry_timestamp": _ts(t), "not_before": _ts(t)}
                     for k, w in enumerate(rng.sample(WORDS, 3))]
            if i == 8:
                certs.append({"id": 8000000999, "issuer_name": "C=US, O=Let's Encrypt, CN=R10",
                              "name_value": "promo.zysk-orlen-demo.example", "entry_timestamp": _ts(t)})
            (d / f"t{i:02d}-crtsh.json").write_text(json.dumps(certs, indent=1), encoding="utf-8")
            tick["crtsh"] = f"t{i:02d}-crtsh.json"
        if i == 5:
            ads = [{"id": "syn-ad-1", "page_id": "100000000000001", "page_name": "Inwestycje Demo (SYNTETYCZNE)",
                    "link_urls": ["https://promo.zysk-orlen-demo.example/start"], "keyword": "zysk",
                    "text": "Zarabiaj z nami! Zadzwoń: +48 512 000 111", "ad_delivery_start_time": t.isoformat()},
                   {"id": "syn-ad-2", "page_id": "100000000000077", "page_name": "Sklep Demo (SYNTETYCZNE)",
                    "link_urls": [f"https://{host(rng)}/"], "keyword": "bonus", "text": "Promocja tylko dziś",
                    "ad_delivery_start_time": t.isoformat()}]
            (d / f"t{i:02d}-meta.json").write_text(json.dumps(ads, indent=1, ensure_ascii=False), encoding="utf-8")
            tick["meta-adlibrary"] = f"t{i:02d}-meta.json"
        ticks.append(tick)
    manifest = {
        "name": "demo", "synthetic": True, "interval_s": 60,
        "description_pl": ("SYNTETYCZNE nagranie demonstracyjne: 12 odpytań co minutę (CERT Polska, URLhaus, "
                           "OpenPhish, crt.sh, Meta) z wymyślonymi domenami .example/.test i syntetyczną listą "
                           "obserwowanych. "
                           "Trzy dodatkowe wpisy w formacie CERT niosą prawdziwe hosty z lokalnych kampanii (jeśli "
                           "lista z kampanii jest zbudowana), żeby pokazać dopasowanie do istniejących kampanii."),
        "watchlist": WATCHLIST, "ct_keywords": ["zysk-orlen"], "campaign_probes": {"tick": 4, "count": 3},
        "ticks": ticks,
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {d} ({len(ticks)} ticks)")


def bench(n: int, out: Path) -> None:
    """One CERT-format snapshot with n synthetic lines (1 % carry a watchlist host) for the matching benchmark."""
    rng = random.Random(7)
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    for k in range(n):
        h = "promo.zysk-orlen-demo.example" if k % 100 == 0 else host(rng)
        lines.append(cert_line(h, T0, f"b-{k}"))
    (out / "t00-cert.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"name": out.name, "synthetic": True, "interval_s": 60,
                                                   "watchlist": WATCHLIST, "ticks": [{"cert-pl": "t00-cert.log"}]}))
    print(f"wrote {out} ({n} items)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["demo", "bench"])
    ap.add_argument("--n", type=int, default=10000)
    ap.add_argument("--out", type=Path, default=Path("/tmp/watch-bench"))
    a = ap.parse_args()
    if a.what == "demo":
        demo()
    else:
        bench(a.n, a.out)
