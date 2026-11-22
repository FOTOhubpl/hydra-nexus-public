"""Fetch a small set of real videos from Wikimedia Commons with permissive licences (server only, calibration data).

Keeps files whose extmetadata LicenseShortName is CC0 / Public domain / CC BY / CC BY-SA (any version), size <= 60 MB,
duration >= 4 s; downloads the 480p VP9 transcode when available. Writes manifest.json (title, licence, artist,
url) next to the files so attribution can be given. Search terms target people talking on camera.

    python scripts/dfk_eval/commons_videos.py --out data/commons_video --n 160
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://commons.wikimedia.org/w/api.php"
UA = "hydra-nexus-eval/1.0 (research; contact via project repository)"
TERMS = ["interview", "speech", "press conference", "lecture", "vlog", "talk", "news", "conversation",
         "wywiad", "konferencja prasowa", "street", "portrait video", "presentation", "debate"]
OK = re.compile(r"^(cc0|public domain|pd|cc[ -]by(-sa)?( \d\.\d)?)", re.I)


def get(params: dict) -> dict:
    q = urllib.parse.urlencode({**params, "format": "json"})
    req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": UA})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            time.sleep(10 * (attempt + 1))
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n", type=int, default=160)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    man_p = a.out / "manifest.json"
    manifest = json.loads(man_p.read_text()) if man_p.exists() else []
    have = {m["title"] for m in manifest}
    for term in TERMS:
        if len(manifest) >= a.n:
            break
        res = get({"action": "query", "generator": "search", "gsrsearch": f"filetype:video {term}",
                   "gsrnamespace": 6, "gsrlimit": 50, "prop": "videoinfo",
                   "viprop": "url|size|extmetadata|derivatives"})
        for p in (res.get("query") or {}).get("pages", {}).values():
            if len(manifest) >= a.n:
                break
            title = p["title"]
            ii = (p.get("videoinfo") or [{}])[0]
            em = ii.get("extmetadata") or {}
            lic = (em.get("LicenseShortName") or {}).get("value", "")
            dur = float(ii.get("duration") or 0) if ii.get("duration") else None
            if title in have or not OK.match(lic.strip()):
                continue
            if dur is not None and dur < 4:
                continue
            url = ii.get("url", "").split("?")[0]
            if not url.lower().endswith((".webm", ".ogv", ".mp4")):
                continue
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", title.split(":", 1)[1])[:120]
            ders = {d.get("transcodekey"): d.get("src") for d in ii.get("derivatives") or []}
            tr = ders.get("480p.vp9.webm") or ders.get("360p.vp9.webm") or ders.get("360p.webm")
            cands = [tr] if tr else ([url] if (ii.get("size") or 0) <= 60e6 else [])
            for u in cands:
                try:
                    ext = Path(urllib.parse.urlparse(u).path).suffix
                    for attempt in range(4):
                        rc = subprocess.run(["curl", "-sf", "-m", "180", "-A", UA, "-o", str(a.out / f"{name}{ext}"),
                                             u]).returncode
                        if rc == 0:
                            break
                        time.sleep(15 * (attempt + 1))
                    if rc != 0:
                        raise RuntimeError(f"curl rc={rc}")
                    manifest.append({"title": title, "file": f"{name}{ext}", "license": lic, "url": u,
                                     "artist": re.sub("<[^>]+>", "", (em.get("Artist") or {}).get("value", ""))[:200],
                                     "description_url": f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(title)}"})
                    have.add(title)
                    break
                except Exception as exc:  # noqa: BLE001
                    print("skip", u[-60:], type(exc).__name__, getattr(exc, "code", ""))
            time.sleep(2.0)
        man_p.write_text(json.dumps(manifest, indent=1))
        print(term, len(manifest), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
