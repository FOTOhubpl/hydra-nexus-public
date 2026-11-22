"""JURY-READY smoke: the jury's path from a clean state, offline, in one command (`just jury-smoke`).

    uv run python scripts/jury_smoke.py [--root /tmp/hydra-jury-smoke] [--n 20] [--keep]

Steps (each prints PASS/FAIL with seconds; exit 1 on any FAIL):
 1. fresh root (own state, data, cache; models from HYDRA_MODELS_DIR), synthetic data only: the fuzz layouts of
    tests/fuzz/jury_corpus.py and a NASK-like dataset of `--n` incidents;
 2. API on a free loopback port (tests/e2e/server.py: HYDRA_OFFLINE=1, HYDRA_AUTH=1, socket guard, network log),
    serving ui/dist when built; empty-state endpoints and the GUI shell answer 200;
 3. GUI-equivalent upload of the hostile `mixed` layout as one ZIP (multipart POST /v1/analyse) -> corpus run with
    a per-file import report; then the NASK-like dataset by path (JSON POST /v1/analyse) -> corpus run;
 4. every result endpoint answers 200 with content (runs, incidents, campaigns, pairs, graph, dashboard, analytics,
    incident card, campaign card, pair evidence, campaign export, search, assignments) and GUI routes return the SPA;
 5. evidence export: GET /v1/export/dataset ZIP, every manifest.json sha256 re-verified; campaign JSON export;
 6. audit chain: GET /v1/audit/verify ok, then `hydra verify` (CAS re-hash + chain) on the stopped state;
 7. CLI batch on a second fresh state: `hydra analyse <dataset>` exit 0 and `hydra export-dataset`;
 8. network log empty (no connection attempt left the host).
Never touches ~/HydraNexus/state or the demo ports 8000/8100.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.e2e import server as e2e  # noqa: E402
from tests.fuzz.jury_corpus import build_fuzz, build_scale, zip_dir  # noqa: E402

RESULTS: list[tuple[str, str, float, str]] = []


def step(name: str):  # noqa: ANN201
    def deco(fn):  # noqa: ANN001, ANN202
        def run(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            t0 = time.monotonic()
            try:
                note = fn(*a, **kw) or ""
                RESULTS.append((name, "PASS", time.monotonic() - t0, str(note)))
                print(f"PASS {name} ({time.monotonic() - t0:.1f} s) {note}", flush=True)
                return True
            except Exception as exc:  # noqa: BLE001 — report and continue with the next step
                RESULTS.append((name, "FAIL", time.monotonic() - t0, f"{type(exc).__name__}: {exc}"[:400]))
                print(f"FAIL {name} ({time.monotonic() - t0:.1f} s) {type(exc).__name__}: {exc}"[:600], flush=True)
                return False
        return run
    return deco


def _ok(c, path: str, *, nonempty: str | None = None) -> dict | list:  # noqa: ANN001
    r = c.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"
    body = r.json() if "json" in r.headers.get("content-type", "") else r.text
    if nonempty is not None:
        val = body.get(nonempty) if isinstance(body, dict) else body
        assert val, f"{path}: empty {nonempty}"
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--n", type=int, default=20, help="incidents in the NASK-like synthetic dataset")
    ap.add_argument("--keep", action="store_true", help="keep the root directory")
    ap.add_argument("--reference-bundle", type=Path, default=None,
                    help="install this offline reference-data bundle (scripts/reference_data.py) first; without it the"
                         " run checks the visible 'missing reference data' note")
    a = ap.parse_args()
    root = (a.root or Path(tempfile.mkdtemp(prefix="hydra-jury-smoke-"))).resolve()
    if root.exists() and any(root.iterdir()):
        shutil.rmtree(root)
    (root / "data").mkdir(parents=True, exist_ok=True)
    ui = REPO / "ui" / "dist"
    ctx: dict = {}

    @step("1 synthetic data")
    def s1() -> str:
        # a clean jury machine: never the developer's ~/HydraNexus/data/external (tests/e2e/server.py would link
        # it into an empty root), only the bundle when given
        ext = root / "data" / "external"
        ext.mkdir(parents=True, exist_ok=True)
        if a.reference_bundle:
            sys.path.insert(0, str(REPO / "scripts"))
            from reference_data import install

            res = install(a.reference_bundle.expanduser().resolve(), ext)
            assert all(v.startswith("ok") for v in res.values()), res
        ctx["fuzz"] = build_fuzz(root / "data" / "fuzz")
        ctx["dataset"] = build_scale(root / "data" / "jury" / "znane", a.n, video_every=10)
        ctx["mixed_zip"] = zip_dir(ctx["fuzz"]["mixed"], root / "mixed.zip")
        return (f"{len(ctx['fuzz'])} fuzz layouts, {a.n} incidents, reference bundle "
                f"{'installed' if a.reference_bundle else 'absent'}")

    @step("2 clean-state server")
    def s2() -> str:
        srv = e2e.start(root, ui_dist=ui if (ui / "index.html").is_file() else None, analyse=False)
        ctx["srv"] = srv
        with srv.client() as c:
            h = _ok(c, "/health")
            assert h["offline"] is True, h
            for p in ("/v1/runs/latest", "/v1/incidents", "/v1/campaigns", "/v1/pairs", "/v1/graph",
                      "/v1/dashboard", "/v1/analytics/overview", "/v1/assignments", "/v1/audit/verify"):
                _ok(c, p)
            if (ui / "index.html").is_file():
                assert "<html" in c.get("/").text.lower()
        return f"port {srv.port}, models {len(h.get('models', []))}"

    @step("2b login (HYDRA_AUTH=1)")
    def s2b() -> str:
        # The jury's browser path: the panel is closed without a login; the admin logs in, gets a session cookie and
        # a CSRF token, and every later step uses a personal access token issued by that admin (tests/e2e/server.py).
        srv = ctx["srv"]
        with srv.client(auth=False) as anon:
            assert anon.get("/v1/campaigns").status_code == 401
            assert anon.get("/v1/auth/session").json()["authenticated"] is False
            hint = anon.get("/v1/auth/session").json()["credentials_hint"]
            if (ui / "index.html").is_file():
                assert "<html" in anon.get("/login").text.lower()  # the login page itself is public
        with srv.session_client() as c:
            me = c.get("/v1/auth/session").json()
            assert me["authenticated"] and me["user"]["role"] == "admin", me
            assert c.get("/v1/campaigns").status_code == 200
            csrf = c.headers.pop("X-CSRF-Token")
            assert c.post("/v1/auth/tokens", json={"name": "jury"}).status_code == 403  # no CSRF header
            r = c.post("/v1/auth/tokens", json={"name": "jury"}, headers={"X-CSRF-Token": csrf})
            assert r.status_code == 201
            assert c.post("/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
            assert c.get("/v1/campaigns").status_code == 401
        return f"401 before login, session + CSRF + token + logout ok; credentials hint {hint['file']}"

    def _analyse(c, **kw) -> dict:  # noqa: ANN001
        r = c.post("/v1/analyse", **kw)
        assert r.status_code == 202, r.text[:300]
        job = r.json()
        assert job["mode"] == "corpus", job
        t0, seen = time.monotonic(), set()
        while True:
            body = c.get(f"/v1/analyse/{job['job_id']}").json()
            prog = c.get(f"/v1/demo/progress/{job['job_id']}")
            if prog.status_code == 200:
                seen.add(str((prog.json().get("core_progress") or {}).get("stage")))
            if body["job"]["status"] in ("done", "failed"):
                break
            assert time.monotonic() - t0 < 1800, "analyse did not finish in 30 min"
            time.sleep(1.0)
        assert body["job"]["status"] == "done", body["job"]
        body["_stages"] = sorted(seen)
        return body

    @step("3a GUI upload of hostile ZIP")
    def s3a() -> str:
        with ctx["srv"].client(timeout=600) as c, ctx["mixed_zip"].open("rb") as fh:
            body = _analyse(c, files={"files": ("zbiór jury.zip", fh, "application/zip")})
        rep = body["result"]["import_report"]
        codes = {f["code"].split(":")[0] for f in rep["files"]}
        assert {"FILE_EMPTY", "ZIP_BOMB_SUSPECTED", "ZIP_PATH_TRAVERSAL", "LOOSE_ROOT_FILE"} <= codes, codes
        assert rep["incidents"] >= 6, rep["summary_pl"]
        return f"{rep['incidents']} incidents, {rep['skipped_files']} files with a status; stages {body['_stages']}"

    @step("3b dataset by path")
    def s3b() -> str:
        with ctx["srv"].client(timeout=600) as c:
            body = _analyse(c, json={"path": str(ctx["dataset"])})
        notes = " ".join(body["result"].get("notes") or [])
        if a.reference_bundle:
            assert "Brak danych referencyjnych offline" not in notes, notes
        else:
            assert "Brak danych referencyjnych offline" in notes, notes
        m = body["result"]["metrics"]
        ctx["metrics"] = m
        assert m["incidents"] == a.n and m["campaigns"] >= 1, m
        return (f"{m['incidents']} incidents, {m['pairs_scored']} pairs, {m['campaigns']} campaigns "
                f"({m['multi_member_campaigns']} multi), timings {m.get('timings_s')}")

    @step("4 result endpoints")
    def s4() -> str:
        with ctx["srv"].client() as c:
            _ok(c, "/v1/runs/latest", nonempty="run")
            inc = _ok(c, "/v1/incidents?limit=500", nonempty="items")
            camps = _ok(c, "/v1/campaigns", nonempty="items")
            pairs = _ok(c, "/v1/pairs?limit=50", nonempty="items")
            _ok(c, "/v1/graph", nonempty="nodes")
            _ok(c, "/v1/dashboard", nonempty="kpis")
            _ok(c, "/v1/analytics/overview")
            _ok(c, "/v1/campaign-groups")
            _ok(c, "/v1/assignments")
            _ok(c, f"/v1/incidents/{inc['items'][0]['id']}")
            multi = next((x for x in camps["items"] if x.get("size", 0) > 1), camps["items"][0])
            ctx["campaign"] = multi["id"]
            _ok(c, f"/v1/campaigns/{multi['id']}")
            _ok(c, f"/v1/campaigns/{multi['id']}/export")
            p = pairs["items"][0]
            _ok(c, f"/v1/pairs/{p['a']}/{p['b']}")
            _ok(c, "/v1/search?q=zwrot")
            n_ui = 0
            if (ui / "index.html").is_file():
                for route in ("/", "/campaigns", "/incidents", "/pairs", "/graph", "/analyse", "/data",
                              f"/incident/{inc['items'][0]['id']}", f"/campaigns/{multi['id']}",
                              f"/pair/{p['a']}/{p['b']}"):
                    r = c.get(route)
                    assert r.status_code == 200 and "<div id=" in r.text, route
                    n_ui += 1
        return f"{inc['total']} incidents, {camps['total']} campaigns, {n_ui} GUI routes"

    @step("5 evidence export")
    def s5() -> str:
        with ctx["srv"].client(timeout=600) as c:
            r = c.get("/v1/export/dataset")
            assert r.status_code == 200 and r.headers["content-type"].startswith("application/zip"), r.status_code
            z = zipfile.ZipFile(io.BytesIO(r.content))
            names = z.namelist()
            man_name = next(n for n in names if n.endswith("manifest.json"))
            man = json.loads(z.read(man_name))
            entries = man.get("files") or man
            checked = 0
            prefix = man_name[: -len("manifest.json")]
            for e in (entries if isinstance(entries, list) else []):
                name = e.get("name") or e.get("path")
                data = z.read(prefix + name)
                assert hashlib.sha256(data).hexdigest() == e["sha256"], name
                checked += 1
            assert checked > 0, "manifest lists no files"
            (root / "exports").mkdir(exist_ok=True)
            (root / "exports" / "dataset.zip").write_bytes(r.content)
        signed = ""
        pkg = str(root / "exports" / "dataset.zip")
        out = subprocess.run([sys.executable, "-m", "hydra.cli", "verify-export", pkg],
                             cwd=REPO, env=e2e.server_env(root, 0, None), capture_output=True, text=True,  # noqa: S603
                             timeout=600)
        if "No such command" not in out.stderr:
            assert out.returncode == 0, out.stdout[-500:] + out.stderr[-500:]
            signed = "; hydra verify-export PASS (Ed25519 + sha256)"
        return f"{len(names)} files, {checked} sha256 verified, {len(r.content)} bytes{signed}"

    @step("6 audit chain")
    def s6() -> str:
        with ctx["srv"].client() as c:
            v = _ok(c, "/v1/audit/verify")
            assert v["ok"] and v["events"] > 0, v
        srv = ctx["srv"]
        log = srv.netlog
        srv.stop()
        ctx["netlog"] = log
        env = e2e.server_env(root, 0, None)
        out = subprocess.run([sys.executable, "-m", "hydra.cli", "verify"], cwd=REPO, env=env,  # noqa: S603
                             capture_output=True, text=True, timeout=1800)
        assert out.returncode == 0, out.stdout[-500:] + out.stderr[-500:]
        res = json.loads(out.stdout[out.stdout.index("{"):])
        return f"API chain ok ({v['events']} events); hydra verify: {res['audit_events']} events, cas ok"

    @step("7 CLI batch on a fresh state")
    def s7() -> str:
        cli_root = root / "cli"
        (cli_root / "data").mkdir(parents=True, exist_ok=True)
        if a.reference_bundle:
            (cli_root / "data" / "external").symlink_to(root / "data" / "external")
        env = e2e.server_env(cli_root, 0, None)
        t0 = time.monotonic()
        out = subprocess.run([sys.executable, "-m", "hydra.cli", "analyse", str(ctx["fuzz"]["nask_folders"]),
                              str(ctx["fuzz"]["zip_in_zip"])], cwd=REPO, env=env,  # noqa: S603
                             capture_output=True, text=True, timeout=3600)
        assert out.returncode == 0, out.stdout[-800:] + out.stderr[-800:]
        assert "import:" in out.stdout, out.stdout[-500:]
        dt = time.monotonic() - t0
        exp = subprocess.run([sys.executable, "-m", "hydra.cli", "export-dataset", "latest",
                              str(cli_root / "export")],
                             cwd=REPO, env=env, capture_output=True, text=True, timeout=600)  # noqa: S603
        assert exp.returncode == 0, exp.stdout[-500:] + exp.stderr[-500:]
        assert (cli_root / "export" / "manifest.json").is_file()
        first = out.stdout.splitlines()[0] if out.stdout else ""
        return f"{dt:.0f} s; {first[:160]}"

    @step("8 offline: no network attempts")
    def s8() -> str:
        log = Path(ctx.get("netlog") or root / "netlog.jsonl")
        lines = log.read_text().splitlines() if log.exists() else []
        assert not lines, f"{len(lines)} blocked connection attempts: {lines[:3]}"
        return "network log empty"

    for fn in (s1, s2, s2b, s3a, s3b, s4, s5, s6, s7, s8):
        if not fn() and fn in (s1, s2):
            break
    srv = ctx.get("srv")
    if srv is not None:
        srv.stop()
    fails = [r for r in RESULTS if r[1] == "FAIL"]
    print("\nJURY SMOKE:", "FAIL" if fails else "PASS", f"({len(RESULTS)} steps, root {root})")
    rows = [{"step": n, "status": st, "seconds": round(sec, 1), "note": note} for n, st, sec, note in RESULTS]
    (root / "jury_smoke.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    if not a.keep and not fails:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    os.environ.setdefault("HYDRA_OFFLINE", "1")
    raise SystemExit(main())
