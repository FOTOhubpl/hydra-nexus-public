"""ID genes: faces (YuNet 2023mar + SFace 2021dec, 128-d) and persons named by the material.

Genes
- `face` (INF): one per detected face in an image, one per face *track* in a video (faces of one artifact
  linked at SFace cosine >= 0.60; the track vector is the mean of its 5 best faces). vector_ref -> SFace
  vector, bbox + timecode in the evidence, quality = detector score x size x frontal x sharpness.
  The cross-corpus identity cluster is CORE's job. Calibration from the sample (VISUAL_GENES.md §4):
  cos >= 0.60 same person, 0.50–0.60 weak only, < 0.45 different; AI portraits of different people reach
  0.56–0.58, so CORE must not single-link chain faces below 0.60.
- `person_named` (OBS): an `<img alt|title>` (or a `<figcaption>`) of a landing asset that has a face names
  a person ("First Surname" with a known first name, or a surname from the reference base). The evidence
  bbox is the face when the image has exactly one face; the note carries the face vector key so CORE can
  detect a *renamed persona* (same face vector, different name across incidents).
- `person_named` (INF): a gated face matches the small reference base (names stated by the material only;
  $HYDRA_DATA_DIR/nask/reference/faces/gallery.json, built by scripts/build_vis_reference.py) at cos >= 0.60.
- `person_candidate` (INF): the same match in the 0.50–0.60 band ("possible", show crops, weak).
Vision never invents a name: a face that resembles someone but is not in the material-named gallery stays
unnamed. OCR naming is GENES-TXT's job.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

import numpy as np

from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, Vector, register
from hydra.genome.vis_media import VisualUnit, collect_units, unit_note

log = logging.getLogger(__name__)

EXTRACTOR = "ident@1"
SFACE_TAG = "sface-2021dec"
YUNET_FILE = "yunet/face_detection_yunet_2023mar.onnx"
SFACE_FILE = "sface/face_recognition_sface_2021dec.onnx"

DET_SCORE_MIN = 0.60
FACE_MIN_PX = 24
SAME_PERSON = 0.60          # strong link / gallery naming
POSSIBLE_PERSON = 0.50      # weak band
TRACK_LINK = 0.60
UPSCALE_MIN_SIDE = 200      # tiny comment avatars are upscaled before detection


# ------------------------------------------------------------------------------------------ models


@dataclass
class FaceModels:
    detector: object
    recognizer: object


def load_face_models(ctx: ExtractContext) -> FaceModels:
    if "faces" not in ctx.models:
        import cv2

        md = ctx.settings.models_dir
        assert md is not None
        det_p, rec_p = md / YUNET_FILE, md / SFACE_FILE
        if not det_p.exists() or not rec_p.exists():
            from hydra.ml.models import ModelUnavailable

            raise ModelUnavailable(f"YuNet/SFace not found under {md}")
        det = cv2.FaceDetectorYN.create(str(det_p), "", (320, 320), DET_SCORE_MIN, 0.3, 5000)
        rec = cv2.FaceRecognizerSF.create(str(rec_p), "")
        ctx.models["faces"] = FaceModels(det, rec)
    return ctx.models["faces"]


@dataclass
class Face:
    unit: VisualUnit
    box: tuple[float, float, float, float]   # unit pixels
    score: float
    face_px: float                           # min(w, h) in source pixels
    yaw: float                               # 0 = frontal (nose offset / eye distance)
    blur: float                              # Laplacian variance of the aligned crop
    vector: np.ndarray
    gated: bool = False
    quality: float = 0.0


def face_quality(score: float, face_px: float, yaw: float, blur: float) -> tuple[float, bool]:
    """Quality in [0,1] and the analyst gate (det >= 0.85, face >= 56 px, yaw <= 0.3, blur >= 30)."""
    gated = score >= 0.85 and face_px >= 56 and yaw <= 0.3 and blur >= 30
    q = (min(1.0, max(0.0, (score - 0.5) / 0.45)) * min(1.0, face_px / 96.0)
         * max(0.2, 1.0 - yaw) * min(1.0, blur / 100.0))
    return round(float(q), 3), gated


def detect_faces(u: VisualUnit, fm: FaceModels) -> list[Face]:
    import cv2

    rgb = np.asarray(u.image)
    img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    h, w = img.shape[:2]
    up = 1.0
    if min(h, w) < UPSCALE_MIN_SIDE:
        up = UPSCALE_MIN_SIDE / max(1, min(h, w))
        img = cv2.resize(img, (round(w * up), round(h * up)), interpolation=cv2.INTER_CUBIC)
    fm.detector.setInputSize((img.shape[1], img.shape[0]))  # type: ignore[attr-defined]
    _, found = fm.detector.detect(img)  # type: ignore[attr-defined]
    out: list[Face] = []
    if found is None:
        return out
    for f in found:
        fw, fh = float(f[2]), float(f[3])
        if min(fw, fh) < FACE_MIN_PX:
            continue
        aligned = fm.recognizer.alignCrop(img, f)  # type: ignore[attr-defined]
        vec = np.asarray(fm.recognizer.feature(aligned), dtype=np.float32).ravel()  # type: ignore[attr-defined]
        vec = vec / max(float(np.linalg.norm(vec)), 1e-12)
        lm = f[4:14].reshape(5, 2)
        eye_d = float(np.linalg.norm(lm[0] - lm[1]))
        yaw = abs(float(lm[2][0] - (lm[0][0] + lm[1][0]) / 2)) / max(eye_d, 1.0)
        blur = float(cv2.Laplacian(cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
        box = (float(f[0]) / up, float(f[1]) / up, fw / up, fh / up)
        face_px = min(fw, fh) / up / (u.scale or 1.0)
        q, gated = face_quality(float(f[14]), face_px, yaw, blur)
        out.append(Face(u, box, float(f[14]), face_px, yaw, blur, vec, gated, q))
    return out


# ------------------------------------------------------------------------------------------ tracks


@dataclass
class Track:
    faces: list[Face] = field(default_factory=list)

    @property
    def best(self) -> Face:
        return max(self.faces, key=lambda f: f.quality)

    def vector(self) -> np.ndarray:
        top = sorted(self.faces, key=lambda f: -f.quality)[:5]
        v = np.mean([f.vector for f in top], axis=0)
        return (v / max(float(np.linalg.norm(v)), 1e-12)).astype(np.float32)


def build_tracks(faces: list[Face], link: float = TRACK_LINK) -> list[Track]:
    """Greedy per-artifact tracks: a face joins the track whose current vector is most similar (>= link)."""
    tracks: list[Track] = []
    vecs: list[np.ndarray] = []
    for f in sorted(faces, key=lambda f: (f.unit.timecode_s or 0.0)):
        best, bi = -1.0, -1
        for i, v in enumerate(vecs):
            s = float(f.vector @ v)
            if s > best:
                best, bi = s, i
        if bi >= 0 and best >= link:
            tracks[bi].faces.append(f)
            vecs[bi] = tracks[bi].vector()
        else:
            tracks.append(Track([f]))
            vecs.append(f.vector)
    return tracks


# ------------------------------------------------------------------------------------------ names


@lru_cache(maxsize=1)
def first_names() -> frozenset[str]:
    p = Path(__file__).parent / "assets" / "first_names.txt"
    words = [w for line in p.read_text(encoding="utf-8").splitlines() if not line.startswith("#")
             for w in line.split()]
    from hydra.genome.lexicons import local

    words += (local.load("names") or {}).get("first_names", [])
    return frozenset(words)


_UP = "A-ZĄĆĘŁŃÓŚŹŻ"
_LO = "a-ząćęłńóśźżäöüéè"
_NAME_RE = re.compile(rf"\b([{_UP}][{_LO}]+)\s+([{_UP}][{_LO}]+(?:-[{_UP}][{_LO}]+)?)\b")


def names_in_text(text: str, surnames: dict[str, str] | None = None) -> list[str]:
    """Person names stated in a short text: 'First Surname' with a known first name, plus bare surnames
    of the reference base (mapped to the canonical name). Order of first appearance, deduplicated."""
    out: list[str] = []
    fn = first_names()
    for m in _NAME_RE.finditer(text or ""):
        if m.group(1) in fn and m.group(2) not in fn:
            name = f"{m.group(1)} {m.group(2)}"
            if name not in out:
                out.append(name)
    if surnames:
        low = fold(text or "")
        for stem, canon in surnames.items():
            st = fold(stem)
            if len(st) < 5 or canon in out or not re.search(rf"(?<![a-z]){re.escape(st)}", low):
                continue
            if not any(st in fold(n) for n in out):  # a full-name hit already covers this surname
                out.append(canon)
    return out


def fold(text: str) -> str:
    """Lower-case ASCII fold (ł -> l, diacritics stripped) for file-name matching."""
    import unicodedata

    t = text.lower().replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))


def _asset_key(path_or_url: str) -> str:
    """Basename without the query and without a page-capture '_<sha8>' suffix, lower-case."""
    p = unquote(urlsplit(path_or_url).path if "://" in path_or_url else path_or_url.split("?")[0])
    name = Path(p.replace("\\", "/")).name.lower()
    stem, dot, ext = name.rpartition(".")
    if dot:
        stem = re.sub(r"_[0-9a-f]{8}$", "", stem)
        return f"{stem}.{ext}"
    return re.sub(r"_[0-9a-f]{8}$", "", name)


def alt_text_map(inc: ResolvedIncident) -> dict[str, list[str]]:
    """asset key -> alt/title/figcaption texts from every landing HTML of the incident."""
    from bs4 import BeautifulSoup

    out: dict[str, list[str]] = {}
    for a in inc.by_role("landing_html"):
        try:
            if a.path.stat().st_size > 20_000_000:
                continue
            soup = BeautifulSoup(a.path.read_bytes(), "lxml")
        except Exception:  # noqa: BLE001
            continue
        for img in soup.find_all(["img", "source"]):
            src = img.get("src") or img.get("data-src") or (img.get("srcset") or "").split(" ")[0]
            if not src:
                continue
            texts = [t.strip() for t in (img.get("alt"), img.get("title")) if isinstance(t, str) and t.strip()]
            fig = img.find_parent("figure")
            if fig is not None and fig.find("figcaption") is not None:
                texts.append(fig.find("figcaption").get_text(" ", strip=True)[:300])
            if texts:
                out.setdefault(_asset_key(str(src)), []).extend(t for t in texts if t not in out.get(
                    _asset_key(str(src)), []))
    return out


# ------------------------------------------------------------------------------------------ gallery


def reference_dir(ctx: ExtractContext) -> Path | None:
    """$HYDRA_VIS_REFERENCE_DIR (non-empty) > <state>/reference (built by `hydra reference build`) > <data>/nask/
    reference (hydra.reference_build.reference_dir)."""
    from hydra.reference_build import reference_dir as _rd

    return _rd(ctx.settings)


@dataclass
class Gallery:
    names: list[str]
    roles: list[str]
    vectors: np.ndarray            # [n_vectors, 128]
    owner: np.ndarray              # vector -> entry index
    surnames: dict[str, str]       # lower-case surname stem -> canonical name
    aliases: list[list[str]] = field(default_factory=list)  # names the material gives the same face

    def match(self, v: np.ndarray) -> tuple[int, float]:
        if len(self.vectors) == 0:
            return -1, 0.0
        sims = self.vectors @ v
        best = np.full(len(self.names), -1.0)
        np.maximum.at(best, self.owner, sims)
        i = int(best.argmax())
        return i, float(best[i])


def load_gallery(ctx: ExtractContext) -> Gallery | None:
    if "face_gallery" in ctx.models:
        return ctx.models["face_gallery"]
    g = None
    rd = reference_dir(ctx)
    p = rd / "faces" / "gallery.json" if rd else None
    if p is not None and p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            names, roles, vecs, owner, surnames, aliases = [], [], [], [], {}, []
            for i, e in enumerate(d.get("entries", [])):
                names.append(e["name"])
                aliases.append(list(e.get("aliases", [e["name"]])))
                roles.append(e.get("role", "unknown"))
                for v in e.get("vectors", []):
                    vecs.append(np.asarray(v, np.float32))
                    owner.append(i)
                if e.get("role") == "public_figure":  # bare-surname mentions only for public figures
                    for s in e.get("surname_stems", []):
                        surnames[s.lower()] = e["name"]
            arr = np.stack(vecs) if vecs else np.zeros((0, 128), np.float32)
            arr = arr / np.maximum(np.linalg.norm(arr, axis=1, keepdims=True), 1e-12)
            g = Gallery(names, roles, arr.astype(np.float32), np.asarray(owner, int), surnames, aliases)
        except Exception as exc:  # noqa: BLE001
            log.warning("face gallery unreadable: %s", exc)
    ctx.models["face_gallery"] = g
    return g


# ------------------------------------------------------------------------------------------ extractor


def _face_gene(inc_id: str, f: Face, key: str, n: int = 1, t_range: tuple[float, float] | None = None) -> Gene:
    u = f.unit
    extra = f"det={f.score:.2f}; px={f.face_px:.0f}; yaw={f.yaw:.2f}; blur={f.blur:.0f}; gated={int(f.gated)}"
    if n > 1:
        extra += f"; track_faces={n}"
    if t_range:
        extra += f"; t={t_range[0]:.0f}-{t_range[1]:.0f}s"
    return Gene(incident_id=inc_id, family=Family.ID, kind="face", vector_ref=key, quality=f.quality,
                provenance=Provenance.INF, extractor=EXTRACTOR,
                dependency_group="id_face_boilerplate" if u.boilerplate else None,
                evidence=EvidencePointer(artifact_id=u.artifact_id, bbox=u.to_source_bbox(*f.box),
                                         timecode_s=u.timecode_s, note=unit_note(u, extra)))


def _name_gene(inc_id: str, name: str, f: Face | None, u: VisualUnit, *, kind: str, prov: Provenance,
               quality: float, note: str) -> Gene:
    bbox = u.to_source_bbox(*f.box) if f is not None else None
    return Gene(incident_id=inc_id, family=Family.ID, kind=kind, value=name, quality=round(quality, 3),
                provenance=prov, extractor=EXTRACTOR, dependency_group=f"id_person:{name.lower()}",
                evidence=EvidencePointer(artifact_id=u.artifact_id, bbox=bbox, timecode_s=u.timecode_s,
                                         note=unit_note(u, note)[:300]))


@register("ident", family=Family.ID, version=1)
def ident(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    inc_id = inc.incident.id
    res = ExtractResult()
    us = collect_units(inc, ctx)
    units = [u for u in us.units if u.kind in ("image", "frame") and u.image_class not in ("pixel", "icon", "logo")]
    if not units:
        res.genes.append(Gene(incident_id=inc_id, family=Family.ID, kind="face", status=GeneStatus.NOT_APPLICABLE,
                              extractor=EXTRACTOR, evidence=EvidencePointer(note="no images or frames")))
        return res
    try:
        fm = load_face_models(ctx)
    except Exception as exc:  # noqa: BLE001
        res.genes.append(Gene(incident_id=inc_id, family=Family.ID, kind="face", status=GeneStatus.UNAVAILABLE,
                              extractor=EXTRACTOR, evidence=EvidencePointer(note=f"{type(exc).__name__}: {exc}")))
        return res
    gallery = load_gallery(ctx)
    alts = alt_text_map(inc)
    surnames = gallery.surnames if gallery else {}

    faces_by_artifact: dict[str, list[Face]] = {}
    unit_faces: dict[str, list[Face]] = {}
    for u in units:
        try:
            fs = detect_faces(u, fm)
        except Exception as exc:  # noqa: BLE001
            res.genes.append(Gene(incident_id=inc_id, family=Family.ID, kind="face", status=GeneStatus.FAILED,
                                  extractor=EXTRACTOR, evidence=EvidencePointer(
                                      artifact_id=u.artifact_id, note=f"{type(exc).__name__}: {exc}"[:300])))
            continue
        unit_faces[u.uid] = fs
        faces_by_artifact.setdefault(u.artifact_id, []).extend(fs)

    named_faces: list[tuple[Face, str, np.ndarray]] = []  # (face, key, vector) eligible for gallery naming
    obs_names: dict[str, set[str]] = {}  # face key -> names stated by the material for that single face
    for aid, fs in faces_by_artifact.items():
        if not fs:
            continue
        if fs[0].unit.kind == "frame":
            for j, tr in enumerate(build_tracks(fs)):
                best = tr.best
                key = f"{inc_id}|{aid}|sface|track{j}"
                ts = [f.unit.timecode_s for f in tr.faces if f.unit.timecode_s is not None]
                res.vectors.append(Vector(key=key, model=SFACE_TAG, data=tr.vector()))
                res.genes.append(_face_gene(inc_id, best, key, len(tr.faces), (min(ts), max(ts)) if ts else None))
                if any(f.gated for f in tr.faces):
                    named_faces.append((best, key, tr.vector()))
        else:
            for j, f in enumerate(fs):
                key = f"{inc_id}|{aid}|sface|{f.unit.idx}-{j}"
                res.vectors.append(Vector(key=key, model=SFACE_TAG, data=f.vector))
                res.genes.append(_face_gene(inc_id, f, key))
                if f.gated:
                    named_faces.append((f, key, f.vector))
                f.unit.meta.setdefault("face_keys", []).append(key)

    # names stated by the material (alt / title / figcaption, file name stems of the reference base)
    for u in units:
        if u.kind != "image" or u.artifact.meta.role != "landing_asset":
            continue
        fs = unit_faces.get(u.uid) or []
        if not fs:
            continue
        texts = list(alts.get(_asset_key(u.artifact.meta.path_in_incident), []))
        names: list[tuple[str, str]] = []
        for t in texts:
            names += [(n, f"alt: {t[:110]}") for n in names_in_text(t, surnames)]
        stem = Path(u.name).stem.replace("_", " ").replace("-", " ")
        names += [(n, f"filename: {u.name[:80]}") for n in names_in_text(stem, surnames)]
        seen: set[str] = set()
        one = fs[0] if len(fs) == 1 else None
        for name, why in names:
            if name in seen:
                continue
            seen.add(name)
            fkeys = u.meta.get("face_keys", [])
            note = f"{why}; faces={len(fs)}" + (f"; face={fkeys[0]}" if one is not None and fkeys else "")
            q = 0.9 if one is not None and len(seen) == 1 else 0.6
            if one is not None and fkeys:
                obs_names.setdefault(fkeys[0], set()).add(name)
            res.genes.append(_name_gene(inc_id, name, one, u, kind="person_named", prov=Provenance.OBS,
                                        quality=q, note=note))

    # gallery naming (INF): names come only from the material-named reference base.
    # public figures -> person_named (>= 0.60) / person_candidate (0.50-0.60);
    # invented personas -> persona_match (never an identification) and persona_renamed when the material
    # gives the same face another name (a strong kit-reuse gene).
    if gallery is not None:
        for f, key, v in named_faces:
            i, sim = gallery.match(v)
            if i < 0 or sim < POSSIBLE_PERSON:
                continue
            name, role = gallery.names[i], gallery.roles[i]
            known = set(gallery.aliases[i]) if i < len(gallery.aliases) else {name}
            stated = obs_names.get(key, set())
            note = f"gallery match cos={sim:.3f}; role={role}; face={key}"
            if sim >= SAME_PERSON and role != "public_figure" and (len(known) > 1 or stated - known):
                names_all = sorted(known | stated)
                res.genes.append(_name_gene(inc_id, " | ".join(names_all), f, f.unit, kind="persona_renamed",
                                            prov=Provenance.INF, quality=sim,
                                            note=f"one face, several names in the material; {note}"))
            if stated & known:
                continue  # the material already names this face so; a gallery hit adds nothing
            if role == "public_figure":
                kind = "person_named" if sim >= SAME_PERSON else "person_candidate"
            else:
                kind = "persona_match" if sim >= SAME_PERSON else "person_candidate"
            res.genes.append(_name_gene(inc_id, name, f, f.unit, kind=kind, prov=Provenance.INF,
                                        quality=sim, note=note))
    return res
