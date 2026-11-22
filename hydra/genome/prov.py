"""File provenance genes: C2PA manifests, generator watermarks, generator metadata markers, StyleGAN-avatar geometry.

These are facts about files, not verdicts (docs/genes/PROV.md, research 04_impersonation_provenance.md):
- presence of a C2PA manifest / AI assertion, an SD/SDXL DWT-DCT watermark or a generator metadata chunk says
  "a tool declared or embedded this"; it is high precision, low recall;
- absence is information for the analyst ("nie wykryto"), never evidence of authenticity and never a penalty;
- on platform re-encodes and screenshots manifests and watermarks are stripped, so "not detected" is expected.

All genes carry the dependency group tier `label` (synapse.level() -> "none"): they never add linking bits. They
feed the "Nadużycie wizerunku" analyst card (hydra/likeness.py) only.

Genes (family META unless stated):
- `c2pa_manifest`        incident summary: value "present" | "absent" (checked N files) | status unavailable;
- `c2pa_claim_generator`, `c2pa_signer`, `c2pa_validation` (raw reader state) per file with a manifest;
- `c2pa_binding`         per file: "valid" (reader state Valid/Trusted, no integrity failure code) | "errors"
                         (hash / signature / assertion failure) | "unknown"; integrity of the claim-to-file binding;
- `c2pa_trust`           per file: "trusted" | "untrusted" | "unknown"; signer trust is policy, not integrity;
- `c2pa_ai_generated`    value = IPTC digitalSourceType (trainedAlgorithmicMedia, ...) when an action declares it AND
                         the binding is valid (a declared, cryptographically bound claim, not a verdict);
- `c2pa_ai_declared_unverified` the same declaration when the binding has errors or is unknown (raw diagnostic,
                         quality 0.1; never a positive premise on the card);
- `ai_metadata_marker`   PNG text chunks (A1111 `parameters`, ComfyUI `prompt`/`workflow`), EXIF Software or XMP
                         DigitalSourceType naming a generator;
- `wm_generator`         value "sdxl_dwtdct" | "sd_v1_dwtdct" when the decoded bits match the public generator mark;
- `wm_check`             incident summary: "detected" | "not_detected" (checked N images) | unavailable;
- ID `synth_avatar_eyes` both eye centres within EYE_TOL of the FFHQ/StyleGAN canonical positions on a square image
                         (MediaPipe Face Landmarker iris centres, YuNet eye landmarks as fallback); quality <= 0.3;
- ID `avatar_geometry_check` incident summary: "flagged" | "not_flagged" (checked N faces) | unavailable.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedArtifact, ResolvedIncident, register

log = logging.getLogger(__name__)

VERSION = 1
C2PA_MAX_BYTES = 80_000_000
MAX_FILES = 60
WM_MAX_IMAGES = 30
WM_MAX_SIDE = 4096
WM_MIMES = {"image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff"}  # no GIF/ICO: palette files
CHECK_ROLES = {"ad_creative", "ad_video", "landing_asset", "landing_screenshot", "feed_screenshot", "other",
               "screen_recording"}
AI_SOURCE_TYPES = ("trainedalgorithmicmedia", "compositewithtrainedalgorithmicmedia", "algorithmicmedia",
                   "compositesynthetic")
GENERATOR_WORDS = re.compile(r"stable[ -]?diffusion|midjourney|dall[- ]?e|firefly|comfyui|automatic1111|"
                             r"invokeai|novelai|flux|imagen|leonardo|ideogram|sdxl|gemini|grok|chatgpt|openai",
                             re.I)

# Public generator marks (Stability reference scripts, MIT): SD v1 reference txt2img writes the bytes
# b"StableDiffusionV1"; SDXL / generative-models write the 48-bit message 0xB3EC907BB19E. Both use dwtDct.
SDXL_BITS = [int(b) for b in format(0xB3EC907BB19E, "048b")]
SDV1_BYTES = b"StableDiffusionV1"
WM_MATCH_FRACTION = 0.92  # >= 44/48 bits; random images agree on ~50% (P(>=44/48) < 1e-9 by binomial)

# FFHQ alignment (StyleGAN training set): the crop is 4x the eye distance, centred 0.1 x eye-to-mouth below the
# eye midpoint, so for an average face the eyes land at about (0.375, 0.475) and (0.625, 0.475) of the side.
# Own derivation from the published alignment description; tolerance set conservatively.
FFHQ_EYES = ((0.375, 0.475), (0.625, 0.475))
EYE_TOL = 0.03
SQUARE_TOL = 0.06
# Same file as vid_avsync (models.lock.json key "mediapipe-face-landmarker"); the older "mediapipe/" dir is still read.
MP_MODELS = ("mediapipe-face-landmarker/face_landmarker.task", "mediapipe/face_landmarker.task")
LABEL_GROUP = {"META": "META:label:provenance", "ID": "ID:label:synthetic_avatar"}
# Signer-trust codes are a policy question (is the certificate on a trust list?), not file integrity. Measured with
# c2pa-python on the synthetic signed JPEG (tests/genes_prov/c2pa_synth.py): untouched file -> state "Valid" plus
# signingCredential.untrusted; one flipped byte -> state "Invalid" plus assertion.dataHash.mismatch.
TRUST_CODE_PREFIX = "signingCredential."
BINDING_PL = {"valid": "integralność poprawna", "errors": "błędy walidacji integralności",
              "unknown": "integralności nie potwierdzono"}
TRUST_PL = {"trusted": "podpisujący na liście zaufanych", "untrusted": "certyfikat spoza listy zaufanych",
            "unknown": "zaufanie do podpisującego nieznane"}


def _gene(inc: ResolvedIncident, kind: str, value: str | None, ev: EvidencePointer, *, family: Family = Family.META,
          quality: float = 1.0, prov: Provenance = Provenance.OBS, status: GeneStatus = GeneStatus.OBSERVED) -> Gene:
    return Gene(incident_id=inc.incident.id, family=family, kind=kind, value=value, quality=round(quality, 3),
                status=status, provenance=prov, evidence=ev, extractor=f"prov@{VERSION}",
                dependency_group=LABEL_GROUP[str(family)])


def _candidates(inc: ResolvedIncident) -> list[ResolvedArtifact]:
    out, seen = [], set()
    for a in inc.artifacts:
        mime = (a.meta.mime or "").lower()
        if a.meta.artifact_id in seen or a.meta.role not in CHECK_ROLES:
            continue
        if mime.startswith(("image/", "video/")) and not mime.startswith("image/svg"):
            seen.add(a.meta.artifact_id)
            out.append(a)
    return out[:MAX_FILES]


# ------------------------------------------------------------------------------------------------ C2PA


def parse_manifest_store(store: dict) -> dict:
    """Active-manifest facts from c2pa Reader JSON: generator, signer, AI source type, validation state."""
    manifests = store.get("manifests") or {}
    m = manifests.get(store.get("active_manifest") or "") or (next(iter(manifests.values()), {}) if manifests else {})
    gens = []
    for g in m.get("claim_generator_info") or []:
        if isinstance(g, dict) and g.get("name"):
            gens.append(f"{g['name']} {g.get('version') or ''}".strip())
    if not gens and m.get("claim_generator"):
        gens.append(str(m["claim_generator"]))
    sig = m.get("signature_info") or {}
    signer = " / ".join(str(sig[k]) for k in ("common_name", "issuer") if sig.get(k)) or None
    ai = None
    for a in m.get("assertions") or []:
        if not str(a.get("label", "")).startswith("c2pa.actions"):
            continue
        for act in (a.get("data") or {}).get("actions") or []:
            src = str(act.get("digitalSourceType") or "")
            if src.rsplit("/", 1)[-1].lower() in AI_SOURCE_TYPES:
                ai = src.rsplit("/", 1)[-1]
    codes = [str(s.get("code")) for s in store.get("validation_status") or [] if isinstance(s, dict)]
    state = str(store.get("validation_state") or "")
    integrity = [c for c in codes if not c.startswith(TRUST_CODE_PREFIX)]
    if integrity or (state.lower() == "invalid" and not codes):
        binding = "errors"
    elif state.lower() in ("valid", "trusted"):
        binding = "valid"
    else:  # no state from the reader, or "Invalid" explained only by trust codes: integrity not established
        binding = "unknown"
    trust = ("trusted" if state.lower() == "trusted" else
             "untrusted" if any(c.startswith(TRUST_CODE_PREFIX) for c in codes) else "unknown")
    return {"generator": gens[0] if gens else None, "signer": signer, "ai_source": ai,
            "validation": state or ("errors" if codes else "unknown"),
            "untrusted": "signingCredential.untrusted" in codes, "n_manifests": len(manifests),
            "binding": binding, "trust": trust, "integrity_codes": integrity[:8]}


def read_c2pa(path: Path, mime: str) -> dict | None:
    """Manifest facts, None when the file carries no manifest. Raises on reader errors other than 'not found'."""
    import c2pa

    try:
        with open(path, "rb") as f, c2pa.Reader(mime, f) as r:
            return parse_manifest_store(json.loads(r.json()))
    except Exception as exc:  # c2pa raises its own error classes; "no JUMBF / ManifestNotFound" = absent
        if "notfound" in type(exc).__name__.lower() or "no jumbf" in str(exc).lower():
            return None
        raise


def c2pa_genes(inc: ResolvedIncident, files: list[ResolvedArtifact]) -> list[Gene]:
    try:
        import c2pa

        supported = {m.lower() for m in c2pa.Reader.get_supported_mime_types()}
    except Exception as exc:  # noqa: BLE001
        return [_gene(inc, "c2pa_manifest", None, EvidencePointer(note=f"c2pa unavailable: {exc}"[:200]),
                      status=GeneStatus.UNAVAILABLE)]
    out: list[Gene] = []
    checked = errors = 0
    present: list[str] = []
    for a in files:
        mime = (a.meta.mime or "").lower()
        if mime not in supported or a.meta.size > C2PA_MAX_BYTES:
            continue
        try:
            facts = read_c2pa(a.path, mime)
        except Exception as exc:  # noqa: BLE001 — a malformed manifest is a finding about one file, not a crash
            errors += 1
            log.debug("c2pa read failed for %s: %s", a.meta.path_in_incident, exc)
            continue
        checked += 1
        if facts is None:
            continue
        aid = a.meta.artifact_id
        present.append(aid)
        codes = ", ".join(facts["integrity_codes"])
        note = (f"C2PA {a.meta.path_in_incident}: walidacja {facts['validation']}; {BINDING_PL[facts['binding']]}"
                + (f" ({codes})" if codes else "") + f"; {TRUST_PL[facts['trust']]}")
        ev = EvidencePointer(artifact_id=aid, note=note[:300])
        if facts["generator"]:
            out.append(_gene(inc, "c2pa_claim_generator", facts["generator"], ev))
        if facts["signer"]:
            out.append(_gene(inc, "c2pa_signer", facts["signer"], ev))
        out.append(_gene(inc, "c2pa_validation", str(facts["validation"]), ev, quality=0.5))
        out.append(_gene(inc, "c2pa_binding", facts["binding"], ev, quality=0.5))
        out.append(_gene(inc, "c2pa_trust", facts["trust"], ev, quality=0.5))
        if facts["ai_source"] and facts["binding"] == "valid":
            out.append(_gene(inc, "c2pa_ai_generated", facts["ai_source"], ev))
        elif facts["ai_source"]:
            # raw diagnostic kept: the manifest declares AI content but its binding to this file is broken or unknown,
            # so the declaration says nothing reliable about this file (never raises "generated")
            out.append(_gene(inc, "c2pa_ai_declared_unverified", facts["ai_source"], EvidencePointer(
                artifact_id=aid, note=(f"deklaracja AI w manifeście bez potwierdzonej integralności — nie podnosi "
                                       f"pewności, że plik jest generowany; {note}")[:300]), quality=0.1))
    if present:
        out.append(_gene(inc, "c2pa_manifest", "present", EvidencePointer(
            artifact_id=present[0], note=f"manifest C2PA w {len(present)} z {checked} sprawdzonych plików")))
    elif checked:
        out.append(_gene(inc, "c2pa_manifest", "absent", EvidencePointer(
            note=f"brak manifestu C2PA w {checked} sprawdzonych plikach (brak nie świadczy o autentyczności)"),
            quality=0.2))
    else:
        out.append(_gene(inc, "c2pa_manifest", None, EvidencePointer(
            note=f"brak plików w formacie obsługiwanym przez C2PA (błędy odczytu: {errors})"),
            status=GeneStatus.UNAVAILABLE))
    return out


# ------------------------------------------------------------------------------------------- metadata


def metadata_markers(path: Path) -> list[str]:
    """Generator markers in PNG text chunks, EXIF Software and XMP (own code, Pillow)."""
    from PIL import Image

    found: list[str] = []
    with Image.open(path) as im:
        info = dict(getattr(im, "text", {}) or {})
        info.update({k: v for k, v in im.info.items() if isinstance(v, str | bytes)})
        for key in ("parameters", "prompt", "workflow"):
            if key in info:
                found.append(f"png:{key}")
        sw = str(im.getexif().get(0x0131, "") or "")
        if sw and GENERATOR_WORDS.search(sw):
            found.append(f"exif:Software={sw[:60]}")
        xmp = info.get("XML:com.adobe.xmp") or info.get("xmp") or b""
        xs = xmp.decode("utf-8", "replace") if isinstance(xmp, bytes) else str(xmp)
        m = re.search(r"DigitalSourceType[^a-zA-Z]{0,40}[^\"<]*?(trainedAlgorithmicMedia|"
                      r"compositeWithTrainedAlgorithmicMedia)", xs)
        if m:
            found.append(f"xmp:DigitalSourceType={m.group(1)}")
    return found


# ------------------------------------------------------------------------------------------ watermark


def watermark_match(bgr: np.ndarray) -> tuple[str | None, dict[str, float]]:
    """Fraction of agreeing bits for each public generator mark; name of the mark above WM_MATCH_FRACTION."""
    from hydra.vendor.imwatermark import bits_of_bytes, decode_bits

    marks = {"sdxl_dwtdct": SDXL_BITS, "sd_v1_dwtdct": bits_of_bytes(SDV1_BYTES)}
    scores = {}
    for name, bits in marks.items():
        got = decode_bits(bgr, len(bits)).astype(int)
        scores[name] = round(float(np.mean(got == np.array(bits))), 3)
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return (best if scores[best] >= WM_MATCH_FRACTION else None), scores


def watermark_genes(inc: ResolvedIncident, files: list[ResolvedArtifact]) -> list[Gene]:
    import cv2

    out: list[Gene] = []
    checked = 0
    small = 0
    hits: list[str] = []
    for a in files:
        if (a.meta.mime or "").lower() not in WM_MIMES or checked >= WM_MAX_IMAGES:
            continue
        try:
            bgr = cv2.imread(str(a.path), cv2.IMREAD_COLOR)
        except Exception:  # noqa: BLE001
            bgr = None
        if bgr is None:
            continue
        h, w = bgr.shape[:2]
        if h * w < 256 * 256 or max(h, w) > WM_MAX_SIDE:
            small += 1
            continue
        checked += 1
        try:
            name, scores = watermark_match(bgr)
        except Exception as exc:  # noqa: BLE001
            log.debug("watermark decode failed: %s", exc)
            continue
        if name:
            hits.append(a.meta.artifact_id)
            out.append(_gene(inc, "wm_generator", name, EvidencePointer(
                artifact_id=a.meta.artifact_id, note=f"zgodność bitów znaku {name}: {scores[name]:.0%} "
                f"({a.meta.path_in_incident})"[:300]), prov=Provenance.INF))
    if hits:
        out.append(_gene(inc, "wm_check", "detected", EvidencePointer(
            artifact_id=hits[0], note=f"znak wodny generatora wykryty w {len(hits)} z {checked} obrazów"),
            prov=Provenance.INF))
    elif checked:
        out.append(_gene(inc, "wm_check", "not_detected", EvidencePointer(
            note=f"znak wodny generatora nie wykryty w {checked} obrazach (SD v1 / SDXL DWT-DCT); brak znaku nie "
                 f"świadczy o autentyczności"), prov=Provenance.INF, quality=0.2))
    else:
        out.append(_gene(inc, "wm_check", None, EvidencePointer(
            note=f"brak obrazów >= 256x256 px do sprawdzenia (za małe lub za duże: {small})"),
            status=GeneStatus.UNAVAILABLE, prov=Provenance.INF))
    return out


# ---------------------------------------------------------------------------------- avatar geometry


@dataclass
class EyePair:
    left: tuple[float, float]   # image-normalised (x, y), left eye in the image
    right: tuple[float, float]
    source: str                 # mediapipe | yunet


def ffhq_eye_offset(eyes: EyePair, width: int, height: int) -> float | None:
    """Largest distance (fraction of the side) of the two eye centres from the FFHQ canonical positions.
    None when the image is not square (StyleGAN outputs are square)."""
    if min(width, height) <= 0 or abs(width / height - 1.0) > SQUARE_TOL:
        return None
    (lx, ly), (rx, ry) = sorted((eyes.left, eyes.right))
    d1 = float(np.hypot(lx - FFHQ_EYES[0][0], ly - FFHQ_EYES[0][1]))
    d2 = float(np.hypot(rx - FFHQ_EYES[1][0], ry - FFHQ_EYES[1][1]))
    return round(max(d1, d2), 4)


def _mp_landmarker(ctx: ExtractContext):
    if "mp_face_landmarker" not in ctx.models:
        ctx.models["mp_face_landmarker"] = None
        md = ctx.settings.models_dir
        p = next((md / rel for rel in MP_MODELS if (md / rel).is_file()), None) if md else None
        if p is not None:
            from mediapipe.tasks.python import BaseOptions, vision

            opt = vision.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=str(p)), num_faces=2)
            ctx.models["mp_face_landmarker"] = vision.FaceLandmarker.create_from_options(opt)
    return ctx.models["mp_face_landmarker"]


def eyes_mediapipe(rgb: np.ndarray, landmarker) -> list[EyePair]:
    import mediapipe as mp

    res = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
    out = []
    for lms in res.face_landmarks or []:
        if len(lms) < 478:
            continue
        a = (float(lms[468].x), float(lms[468].y))  # iris centres (refined face mesh: 468 and 473)
        b = (float(lms[473].x), float(lms[473].y))
        left, right = sorted((a, b))
        out.append(EyePair(left, right, "mediapipe"))
    return out


def eyes_yunet(rgb: np.ndarray, ctx: ExtractContext) -> list[EyePair]:
    import cv2

    from hydra.genome.ident import load_face_models

    fm = load_face_models(ctx)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    fm.detector.setInputSize((w, h))  # type: ignore[attr-defined]
    _, found = fm.detector.detect(bgr)  # type: ignore[attr-defined]
    out = []
    for f in [] if found is None else found:
        lm = f[4:8].reshape(2, 2)
        a, b = (float(lm[0][0]) / w, float(lm[0][1]) / h), (float(lm[1][0]) / w, float(lm[1][1]) / h)
        left, right = sorted((a, b))
        out.append(EyePair(left, right, "yunet"))
    return out


def avatar_genes(inc: ResolvedIncident, ctx: ExtractContext, files: list[ResolvedArtifact]) -> list[Gene]:
    from PIL import Image

    out: list[Gene] = []
    try:
        lmk = _mp_landmarker(ctx)
    except Exception as exc:  # noqa: BLE001
        log.debug("mediapipe unavailable: %s", exc)
        lmk = None
    faces = flagged = squares = 0
    for a in files:
        if not (a.meta.mime or "").startswith("image/"):
            continue
        try:
            with Image.open(a.path) as im:
                w, h = im.size
                if min(w, h) < 64 or abs(w / max(1, h) - 1.0) > SQUARE_TOL:
                    continue
                squares += 1
                im = im.convert("RGB")
                if max(w, h) > 1024:
                    im = im.resize((1024, round(1024 * h / w)))
                rgb = np.asarray(im)
            pairs = None
            if lmk is not None:
                try:
                    pairs = eyes_mediapipe(rgb, lmk)
                except Exception as exc:  # noqa: BLE001 — e.g. OSError libEGL.so.1 on a headless Linux host
                    log.debug("mediapipe image failed, falling back to YuNet: %s", exc)
                    lmk = None
            if pairs is None:
                pairs = eyes_yunet(rgb, ctx)
        except Exception as exc:  # noqa: BLE001
            log.debug("avatar geometry failed for %s: %s", a.meta.path_in_incident, exc)
            continue
        for p in pairs[:1] if len(pairs) == 1 else []:  # StyleGAN outputs hold one centred face
            faces += 1
            off = ffhq_eye_offset(p, w, h)
            if off is not None and off <= EYE_TOL:
                flagged += 1
                out.append(_gene(inc, "synth_avatar_eyes", f"ffhq_offset={off:.3f}", EvidencePointer(
                    artifact_id=a.meta.artifact_id, bbox=(0.0, 0.0, float(w), float(h)),
                    note=f"oczy w pozycjach typowych dla wyrównanych twarzy StyleGAN/FFHQ (odchylenie {off:.1%} "
                         f"boku, próg {EYE_TOL:.0%}, {p.source}); cecha typowa dla obrazów generowanych, "
                         f"nie dowód ({a.meta.path_in_incident})"[:300]),
                    family=Family.ID, quality=0.3, prov=Provenance.INF))
    if faces:
        out.append(_gene(inc, "avatar_geometry_check", "flagged" if flagged else "not_flagged", EvidencePointer(
            note=f"geometria oczu sprawdzona na {faces} kwadratowych obrazach z jedną twarzą; "
                 f"zgodnych z FFHQ: {flagged}"), family=Family.ID, quality=0.3, prov=Provenance.INF))
    else:
        out.append(_gene(inc, "avatar_geometry_check", None, EvidencePointer(
            note=f"brak kwadratowych obrazów z jedną wykrytą twarzą (kwadratowych obrazów: {squares})"),
            family=Family.ID, status=GeneStatus.UNAVAILABLE, prov=Provenance.INF))
    return out


# ------------------------------------------------------------------------------------------ extractor


@register("prov", family=Family.META, version=VERSION)
def prov(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    files = _candidates(inc)
    if not files:
        ev = EvidencePointer(note="brak obrazów i nagrań do sprawdzenia pochodzenia")
        return ExtractResult(genes=[
            _gene(inc, "c2pa_manifest", None, ev, status=GeneStatus.NOT_APPLICABLE),
            _gene(inc, "wm_check", None, ev, status=GeneStatus.NOT_APPLICABLE, prov=Provenance.INF)])
    genes = c2pa_genes(inc, files)
    for a in files:
        if not (a.meta.mime or "").startswith("image/"):
            continue
        try:
            marks = metadata_markers(a.path)
        except Exception:  # noqa: BLE001
            continue
        for m in marks:
            genes.append(_gene(inc, "ai_metadata_marker", m, EvidencePointer(
                artifact_id=a.meta.artifact_id, note=f"metadane pliku {a.meta.path_in_incident}: {m}"[:300])))
    genes += watermark_genes(inc, files)
    genes += avatar_genes(inc, ctx, files)
    return ExtractResult(genes=genes)

