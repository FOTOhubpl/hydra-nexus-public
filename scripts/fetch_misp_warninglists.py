"""Fetch a pinned offline snapshot of the mapped MISP warninglists (CC0-1.0; X4BNet datacenter/VPN lists MIT).

Usage: uv run python scripts/fetch_misp_warninglists.py [--commit SHA] [--dest DIR] [--force]
Default DEST: $HYDRA_DATA_DIR/external/misp-warninglists. Every list in the upstream `lists/` folder at the commit
must have an explicit effect in `hydra.enrich.warninglists.EFFECT`; lists with effect `none` (REASON) are not
downloaded. Writes <dest>/<commit>/lists/<name>/list.json, a SNAPSHOT.json (repo, commit, commit date,
retrieved_at, per-file effect, type, matching_attributes, sha256, size, entry count; the unused lists with their
upstream size and reason) and points <dest>/current at it. Files already present with the same git blob sha are
kept (re-run is cheap; --force re-downloads). Data stays outside git; hydra/enrich/warninglists.py reads
<dest>/current.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hydra.enrich.warninglists import EFFECT, REASON  # noqa: E402

REPO = "MISP/misp-warninglists"
UA = {"User-Agent": "hydra-nexus-warninglists/2.0"}


def get(url: str) -> bytes:
    headers = dict(UA)
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"  # optional: only to lift the anonymous API rate limit
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:  # noqa: S310
        return r.read()


def git_blob_sha(b: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()  # noqa: S324 — git object id, not security


def upstream_index(sha: str) -> dict[str, dict] | None:
    """{list name: {"size", "blob"}} of lists/*/list.json at the commit (one GitHub API call); None when the API
    is unavailable (e.g. anonymous rate limit): then the mapped names are fetched without the coverage check."""
    try:
        t = json.loads(get(f"https://api.github.com/repos/{REPO}/git/trees/{sha}?recursive=1"))
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: GitHub tree listing unavailable ({e}); fetching the mapped lists without the upstream "
              "coverage and blob check (set GITHUB_TOKEN to lift the API rate limit)", file=sys.stderr)
        return None
    if t.get("truncated"):
        sys.exit("GitHub tree listing truncated: cannot verify that every list is mapped")
    out = {}
    for e in t.get("tree", []):
        parts = e["path"].split("/")
        if len(parts) == 3 and parts[0] == "lists" and parts[2] == "list.json":
            out[parts[1]] = {"size": e.get("size"), "blob": e.get("sha")}
    return out


def main() -> None:
    a = sys.argv[1:]
    root = Path(os.environ.get("HYDRA_DATA_DIR", Path.home() / "HydraNexus" / "data"))
    dest = Path(a[a.index("--dest") + 1]) if "--dest" in a else root / "external" / "misp-warninglists"
    force = "--force" in a
    if "--commit" in a:
        sha = a[a.index("--commit") + 1]
    else:
        sha = json.loads(get(f"https://api.github.com/repos/{REPO}/commits/main"))["sha"]
    index = upstream_index(sha)
    verified = index is not None
    if index is None:
        index = {n: {"size": None, "blob": None} for n in EFFECT}
    try:
        date = json.loads(get(f"https://api.github.com/repos/{REPO}/commits/{sha}"))["commit"]["committer"]["date"]
    except Exception:  # noqa: BLE001 — the date is informational
        date = None
    unmapped = sorted(set(index) - set(EFFECT))
    gone = sorted(n for n in EFFECT if n not in index)
    if unmapped:
        print(f"WARNING: {len(unmapped)} upstream lists have no mapping (not fetched): {', '.join(unmapped)}",
              file=sys.stderr)
    if gone:
        print(f"note: {len(gone)} mapped lists are not in this commit: {', '.join(gone)}", file=sys.stderr)
    snap = dest / sha
    files, skipped = [], []
    for name in sorted(index):
        effect = EFFECT.get(name)
        if effect in (None, "none"):
            skipped.append({"name": name, "effect": effect or "unmapped", "upstream_size": index[name]["size"],
                            "reason": REASON.get(name, "no mapping in hydra.enrich.warninglists.EFFECT")})
            continue
        p = snap / "lists" / name / "list.json"
        blob = index[name]["blob"]
        b = p.read_bytes() if p.is_file() and not force else b""
        if not b or (blob and git_blob_sha(b) != blob):
            try:
                b = get(f"https://raw.githubusercontent.com/{REPO}/{sha}/lists/{name}/list.json")
            except urllib.error.HTTPError as e:
                if verified or e.code != 404:
                    raise
                print(f"note: {name} not in {sha[:12]}", file=sys.stderr)
                continue
            if blob and git_blob_sha(b) != blob:
                sys.exit(f"{name}: downloaded bytes do not match the git blob at {sha}")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b)
        j = json.loads(b)
        files.append(
            {
                "name": name,
                "effect": effect,
                "type": j.get("type"),
                "matching_attributes": j.get("matching_attributes"),
                "version": j.get("version"),
                "entries": len(j.get("list") or []),
                "size": len(b),
                "sha256": hashlib.sha256(b).hexdigest(),
            }
        )
        print(f"{name:44} {effect:13} {j.get('type'):10} {len(j.get('list') or []):7}", file=sys.stderr)
    # the licence statement (CC0-1.0) is in the README; the repository has no LICENSE file
    (snap / "README.md").write_bytes(get(f"https://raw.githubusercontent.com/{REPO}/{sha}/README.md"))
    meta = {
        "repo": f"https://github.com/{REPO}",
        "commit": sha,
        "commit_date": date,
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "licence": "CC0-1.0 (repository); X4BNet datacenter/VPN lists MIT (notice in list description)",
        "upstream_lists": len(index) if verified else None,
        "upstream_verified": verified,
        "mapped": len(files),
        "bytes": sum(f["size"] for f in files),
        "files": files,
        "not_fetched": skipped,
    }
    (snap / "SNAPSHOT.json").write_text(json.dumps(meta, indent=1))
    cur = dest / "current"
    if cur.is_symlink() or cur.exists():
        cur.unlink()
    cur.symlink_to(sha)
    print(f"{snap}: {len(files)} lists fetched, {len(skipped)} not fetched, {meta['bytes']} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
