"""Reference base (face gallery + logo templates) built from the operator's OWN imported material.

NASK task §5 allows "a small reference base of the most frequent persons". The project ships no face biometrics
and no brand logo images (privacy / biometric data, trademarks); the jury builds the base locally from what it
imported, with one call (`hydra reference build`, POST /v1/reference/build, UI "Zbuduj galerię referencyjną").

- faces: a gallery entry only from faces the material itself names (one quality-gated face in an image whose
  alt/title/figcaption or file name states the name). Up to 5 vectors per name, outliers (cos < 0.45 to the entry
  medoid) dropped. Roles from `persons.json` (`public_figures`); every other name is a persona. No external lookup.
- logos: a brand mark that recurs (same image content) in >= 2 incidents and whose file name or alt/title text
  matches the brand lexicon, or whose alt/title says "logo <name>". Stored as grey PNG + logos.json.

Output goes to the writable reference dir (`reference_dir(settings, write=True)`): $HYDRA_VIS_REFERENCE_DIR, else
<state>/reference. Never into git. Scoring and extractor logic are unchanged; this module only builds and locates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np

from hydra.config import Settings

Progress = Callable[[int, int, str], None]
MIN_LOGO_INCIDENTS = 2
EMPTY_PL = ("Brak materiału do zbudowania bazy referencyjnej: najpierw zaimportuj materiały (Pulpit → Analizuj"
            " lub `hydra analyse <folder>`).")


# ------------------------------------------------------------------------------------------ location


def _nonempty(p: Path) -> bool:
    return p.is_dir() and any(p.iterdir())


def reference_dir(settings: Settings, *, write: bool = False) -> Path | None:
    """Where the reference base is read from (write=False) or written to (write=True).
    Read order: $HYDRA_VIS_REFERENCE_DIR (non-empty) > <state>/reference (non-empty) > <data>/nask/reference."""
    env = os.environ.get("HYDRA_VIS_REFERENCE_DIR")
    state_ref = settings.state_dir / "reference" if settings.state_dir is not None else None
    if write:
        return Path(env) if env else state_ref
    if env and _nonempty(Path(env)):
        return Path(env)
    if state_ref is not None and _nonempty(state_ref):
        return state_ref
    return settings.data_dir / "nask" / "reference" if settings.data_dir is not None else None


def fingerprint(settings: Settings) -> str:
    """Digest of the gallery + logo templates in use (extract cache key of ident/logo: a rebuilt base re-extracts
    only those two extractors)."""
    rd = reference_dir(settings)
    h = hashlib.sha256()
    if rd is not None:
        for p in sorted([rd / "faces" / "gallery.json", rd / "logos" / "logos.json", *(rd / "logos").glob("*.png")]
                        if (rd / "logos").is_dir() else [rd / "faces" / "gallery.json"]):
            if p.is_file():
                h.update(p.name.encode())
                h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()[:16]


def _roles(ref: Path, src_ref: Path | None) -> dict[str, str]:
    for d in (ref, src_ref):
        if d is not None and (d / "persons.json").is_file():
            try:
                names = json.loads((d / "persons.json").read_text(encoding="utf-8")).get("public_figures", [])
                return {n: "public_figure" for n in names}
            except Exception:  # noqa: BLE001
                return {}
    return {}


# ------------------------------------------------------------------------------------------ sources


def _guess_role(rel: Path, mime: str, siblings: set[str]) -> str | None:
    """Artefact role by path (same heuristics as scripts/vis_sample_run.py)."""
    low = rel.name.lower()
    parts = [p.lower() for p in rel.parts]
    if low.endswith(".har"):
        return "har"
    if low.endswith(".info.json"):
        return "platform_metadata"
    in_assets = any(p.endswith("_files") or p == "resources" for p in parts[:-1])
    if (low.endswith((".html", ".htm", ".mhtml")) and not in_assets) or low == "dom.html":
        return "landing_html"
    if in_assets:
        return "landing_asset"
    if low == "screenshot.png":
        return "landing_screenshot"
    if mime.startswith("video/") or rel.suffix.lower() in {".mp4", ".mov", ".ts", ".webm", ".m4v"}:
        return "ad_video" if f"{rel.name.rsplit('.', 1)[0]}.info.json" in siblings else "screen_recording"
    if mime.startswith("image/"):
        return "feed_screenshot" if low.startswith("img_") else "ad_creative"
    return None


def incidents_from_folder(folder: Path) -> Iterator[Any]:
    """A dataset folder (one subfolder per incident), read in place (no import, no DB write)."""
    from hydra.contracts import Incident, IncidentArtifact
    from hydra.genome import base
    from hydra.ingest import sniff_mime

    for sub in sorted(p for p in Path(folder).iterdir() if p.is_dir()):
        arts = []
        for p in sorted(sub.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(sub)
            mime = sniff_mime(p)
            role = _guess_role(rel, mime, {x.name for x in p.parent.iterdir()})
            if role is None:
                continue
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            arts.append(base.ResolvedArtifact(IncidentArtifact(
                artifact_id=digest, role=role, path_in_incident=rel.as_posix(), mime=mime, size=p.stat().st_size), p))
        inc = Incident(id=f"nask-{sub.name}", source="nask-sample", source_ref=sub.name,
                       artifacts=[a.meta for a in arts])
        yield base.ResolvedIncident(inc, arts)


def incidents_from_state(settings: Settings, corpus: str | None = "nask") -> Iterator[Any]:
    """The imported incidents of the state (DB read-only, originals read-only from the CAS)."""
    from hydra.adapters.nask_folder import load_incident, resolve
    from hydra.corpus import incidents_in
    from hydra.db import connect
    from hydra.storage import ContentStore

    if not settings.db_path.exists():
        return
    store = ContentStore(settings.cas_dir)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        ids = incidents_in(conn, corpus)
        incs = [load_incident(conn, i) for i in ids]
    for inc in incs:
        if inc is not None:
            yield resolve(inc, store)


def _materialise(source: Iterable[Any] | Path | None, settings: Settings) -> list[Any]:
    if source is None:
        return list(incidents_from_state(settings))
    if isinstance(source, (str, Path)):
        return list(incidents_from_folder(Path(source)))
    return list(source)


def _prepare(settings: Settings, out: Path | None) -> tuple[Path, Path | None]:
    ref = Path(out) if out else reference_dir(settings, write=True)
    assert ref is not None
    src_ref = settings.data_dir / "nask" / "reference" if settings.data_dir is not None else None
    ref.mkdir(parents=True, exist_ok=True)
    if src_ref is not None and src_ref != ref and (src_ref / "persons.json").is_file() \
            and not (ref / "persons.json").exists():
        shutil.copy2(src_ref / "persons.json", ref / "persons.json")  # hand-made roles, not biometrics
    return ref, src_ref


# ------------------------------------------------------------------------------------------ faces


def merge_same_faces(entries: list[dict], th: float = 0.80) -> list[dict]:
    """One face photo named differently on different sites (a renamed persona) becomes one entry with `aliases`."""
    merged: list[dict] = []
    for e in entries:
        ve = np.asarray(e["vectors"], np.float32)
        for m in merged:
            vm = np.asarray(m["vectors"], np.float32)
            if e["role"] == m["role"] == "persona" and float((ve @ vm.T).max()) >= th:
                m["aliases"] = sorted(set(m["aliases"]) | {e["name"]})
                m["vectors"] += e["vectors"]
                m["evidence"] += e["evidence"]
                m["support"]["incidents"] = sorted(set(m["support"]["incidents"]) | set(e["support"]["incidents"]))
                m["support"]["artifacts"] += e["support"]["artifacts"]
                break
        else:
            e["aliases"] = [e["name"]]
            merged.append(e)
    return merged


def build_faces(settings: Settings, source: Iterable[Any] | Path | None = None, *, out: Path | None = None,
                progress: Progress | None = None) -> dict[str, Any]:
    """Face gallery from material-named faces. `source`: None = the state's imported incidents, a folder, or an
    iterable of ResolvedIncident. Returns {entries, incidents, path, message_pl}."""
    from hydra.genome import ident
    from hydra.genome.base import ExtractContext

    incs = _materialise(source, settings)
    ref, src_ref = _prepare(settings, out)
    if not incs:
        return {"entries": 0, "incidents": 0, "path": None, "message_pl": EMPTY_PL}
    roles = _roles(ref, src_ref)
    ctx = ExtractContext(settings=settings, work_dir=settings.work_dir / "reference_build")
    ctx.models["face_gallery"] = None  # build from the material only, never from a previous gallery
    found: dict[str, list[dict]] = defaultdict(list)
    for n, inc in enumerate(incs):
        if progress:
            progress(n, len(incs), f"twarze: {inc.incident.id}")
        ctx.work_dir = settings.work_dir / "reference_build" / inc.incident.id
        r = ident.ident(inc, ctx)
        vec = {v.key: v.data for v in r.vectors}
        q = {g.vector_ref: g for g in r.genes if g.kind == "face"}
        for g in r.genes:
            if g.kind != "person_named" or g.provenance != "OBS" or "; face=" not in (g.evidence.note or ""):
                continue
            key = g.evidence.note.split("; face=")[1].split(";")[0].strip()
            fg = q.get(key)
            if key not in vec or fg is None or "gated=1" not in (fg.evidence.note or ""):
                continue
            found[g.value].append(dict(vector=np.asarray(vec[key], np.float32), quality=fg.quality,
                                       incident=inc.incident.id, artifact=g.evidence.artifact_id,
                                       why=g.evidence.note.split("; faces=")[0]))
    entries = []
    for name, items in sorted(found.items()):
        uniq: dict[str, dict] = {}
        for it in sorted(items, key=lambda x: -x["quality"]):
            uniq.setdefault(it["artifact"], it)
        items = list(uniq.values())
        V = np.stack([it["vector"] for it in items])
        V = V / np.maximum(np.linalg.norm(V, axis=1, keepdims=True), 1e-12)
        med = int((V @ V.T).sum(axis=1).argmax())
        keep = [it for it, sim in zip(items, V @ V[med], strict=True) if sim >= 0.45][:5]
        surname = name.split()[-1]
        stems = [surname.lower()[:-1] if len(surname) > 5 else surname.lower()]
        entries.append(dict(
            name=name, role=roles.get(name, "persona"), surname_stems=stems,
            support=dict(artifacts=len(keep), incidents=sorted({it["incident"] for it in keep})),
            evidence=[it["why"][:160] for it in keep],
            vectors=[[round(float(x), 6) for x in it["vector"]] for it in keep]))
    entries = merge_same_faces(entries)
    (ref / "faces").mkdir(parents=True, exist_ok=True)
    path = ref / "faces" / "gallery.json"
    path.write_text(json.dumps(dict(
        model=ident.SFACE_TAG, built=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        rule="material-named faces only (alt/title/figcaption/file name), one gated face per image",
        source_incidents=len(incs), entries=entries), ensure_ascii=False, indent=1), encoding="utf-8")
    msg = (f"Galeria twarzy: {len(entries)} osób nazwanych w materiale ({len(incs)} incydentów przejrzanych)."
           if entries else f"Galeria twarzy: w {len(incs)} incydentach nie znaleziono twarzy podpisanej imieniem i"
           " nazwiskiem w samym materiale (alt/title/podpis/nazwa pliku); galeria jest pusta.")
    return {"entries": len(entries), "incidents": len(incs), "path": str(path), "message_pl": msg,
            "public_figures": sum(e["role"] == "public_figure" for e in entries)}


# ------------------------------------------------------------------------------------------ logos

_LOGO_ALT = re.compile(r"(?i)\blogo(?:typ)?\b[\s:\-–]*([^\n|]{2,40})")


def _brand_for(name: str, texts: list[str]) -> tuple[str, str] | None:
    from hydra.genome.logo import brands_in_text

    hits = brands_in_text(Path(name).stem)
    if not hits:
        for t in texts:
            hits = brands_in_text(t)
            if hits:
                break
    if hits:
        return hits[0]
    for t in texts:  # "logo Xyz" in alt/title: the material names the brand itself
        m = _LOGO_ALT.search(t)
        if m and m.group(1).strip():
            return m.group(1).strip().strip(".,;"), "media"
    return None


def build_logos(settings: Settings, source: Iterable[Any] | Path | None = None, *, out: Path | None = None,
                progress: Progress | None = None, min_incidents: int = MIN_LOGO_INCIDENTS) -> dict[str, Any]:
    """Logo templates from brand marks that recur (identical image bytes) in >= `min_incidents` incidents and carry
    a brand name (lexicon on file name / alt / title, or 'logo <name>' in alt/title)."""
    from hydra.genome.ident import _asset_key, alt_text_map
    from hydra.genome.vis_media import load_unit_image

    incs = _materialise(source, settings)
    ref, _ = _prepare(settings, out)
    if not incs:
        return {"templates": 0, "incidents": 0, "path": None, "message_pl": EMPTY_PL}
    groups: dict[str, dict[str, Any]] = {}
    for n, inc in enumerate(incs):
        if progress:
            progress(n, len(incs), f"logo: {inc.incident.id}")
        alts = alt_text_map(inc)
        for a in inc.artifacts:
            mime = (a.meta.mime or "").lower()
            name = Path(a.meta.path_in_incident).name
            if not mime.startswith("image/") or mime == "image/svg+xml" or a.meta.size > 2_000_000:
                continue
            texts = alts.get(_asset_key(a.meta.path_in_incident), [])
            brand = _brand_for(name, texts)
            if brand is None:
                continue
            g = groups.setdefault(a.meta.artifact_id, {"brand": brand, "path": a.path, "incidents": set()})
            g["incidents"].add(inc.incident.id)
    d = ref / "logos"
    d.mkdir(parents=True, exist_ok=True)
    for p in d.glob("*.png"):
        p.unlink()
    meta: dict[str, dict[str, Any]] = {}
    for aid, g in sorted(groups.items(), key=lambda kv: (kv[1]["brand"][0], kv[0])):
        if len(g["incidents"]) < min_incidents:
            continue
        try:
            im, _, _ = load_unit_image(g["path"], max_side=4096)
        except Exception:  # noqa: BLE001 — unreadable image: skip
            continue
        if min(im.size) < 16:
            continue
        brand, kind = g["brand"]
        safe = re.sub(r"[^0-9A-Za-z]+", "_", brand).strip("_")[:40] or "logo"
        fname = f"{safe}_{aid[:8]}.png"
        im.convert("L").save(d / fname)
        meta[fname] = {"brand": brand, "kind": kind, "incidents": len(g["incidents"]), "artifact_id": aid}
    (d / "logos.json").write_text(json.dumps({
        "note": "built from the operator's imported material: recurring brand marks (>= 2 incidents)",
        "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "templates": meta}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    msg = (f"Wzorce logo: {len(meta)} znaków marek powtarzających się w co najmniej {min_incidents} incydentach."
           if meta else f"Wzorce logo: w {len(incs)} incydentach nie znaleziono znaku marki powtarzającego się w co"
           f" najmniej {min_incidents} incydentach; wzorce są puste (działa słownik marek).")
    return {"templates": len(meta), "incidents": len(incs), "path": str(d), "message_pl": msg,
            "brands": sorted({m["brand"] for m in meta.values()})}


# ------------------------------------------------------------------------------------------ both + status


def build(settings: Settings, source: Iterable[Any] | Path | None = None, *, faces: bool = True, logos: bool = True,
          out: Path | None = None, progress: Progress | None = None) -> dict[str, Any]:
    incs = _materialise(source, settings)
    res: dict[str, Any] = {"incidents": len(incs)}
    if not incs:
        return {**res, "ok": False, "message_pl": EMPTY_PL}
    if faces:
        res["faces"] = build_faces(settings, incs, out=out, progress=progress)
    if logos:
        res["logos"] = build_logos(settings, incs, out=out, progress=progress)
    res["ok"] = True
    res["message_pl"] = " ".join(r["message_pl"] for r in (res.get("faces"), res.get("logos")) if r) + \
        " Baza zostaje lokalnie; aby geny ID/logo pojawiły się w wynikach, przelicz analizę."
    res["path"] = str(Path(out) if out else reference_dir(settings, write=True))
    return res


def status(settings: Settings) -> dict[str, Any]:
    """Counts of the reference base in use (no vectors, no names of persons: counts and brand names only)."""
    rd = reference_dir(settings)
    faces = logos = 0
    built = None
    if rd is not None and (rd / "faces" / "gallery.json").is_file():
        try:
            g = json.loads((rd / "faces" / "gallery.json").read_text(encoding="utf-8"))
            faces, built = len(g.get("entries", [])), g.get("built")
        except Exception:  # noqa: BLE001
            pass
    if rd is not None and (rd / "logos").is_dir():
        logos = len(list((rd / "logos").glob("*.png")))
    from hydra.jury_report import reference_status

    return {"dir_kind": "env" if os.environ.get("HYDRA_VIS_REFERENCE_DIR") else "state",
            "face_entries": faces, "logo_templates": logos, "built": built,
            "fingerprint": fingerprint(settings), **{"references": reference_status(settings)}}
