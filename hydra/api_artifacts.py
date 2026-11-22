"""Artefact viewer endpoints: original bytes, safe text for previews, thumbnails, video frames, HAR tables,
ASR transcripts, the genes extracted from one artefact and the pairs they contributed to, and a corpus-wide
"Materiały" summary.

Security model (spec §19, review #6): everything is GET-only behind the local-only guard of hydra.api.
Originals are read from the CAS and never modified. Content the browser could execute (HTML, SVG, XML, JS, any
text/*) is never served with its own type: /raw sends it as text/plain + `Content-Security-Policy: sandbox`,
and /text returns it inside JSON so the UI can show it in a sandboxed `srcdoc` iframe (no scripts, opaque
origin, no network except images from this origin). Thumbnails and frames are derived files cached under
<work_dir>/thumbs/ and can be deleted at any time.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
import subprocess
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from hydra.api_task import _assignment_runs, _gene_level, _in, _json, _st, resolve_run, run_incidents, safe_ref
from hydra.redact import redact_document, redact_obj, redact_text, redact_url

router = APIRouter(prefix="/v1", tags=["artifacts"])

TEXT_LIMIT = 2 * 1024 * 1024        # default bytes returned by /text
TEXT_LIMIT_MAX = 8 * 1024 * 1024
HAR_LIMIT = 64 * 1024 * 1024        # HAR files above this are not parsed in the request
REDACT_PARSE_LIMIT = 16 * 1024 * 1024  # JSON/HAR parsed whole for structural redaction of a cut /text window
THUMB_WIDTHS = (160, 320, 640, 1280)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Types the browser may render with their own Content-Type (no script execution possible).
_SAFE_INLINE = re.compile(r"^(image/(png|jpe?g|gif|webp|bmp|avif|x-icon|vnd\.microsoft\.icon)|video/[\w.+-]+|"
                          r"audio/[\w.+-]+|application/pdf|text/css)$")
_TEXT_MIMES = ("application/json", "application/javascript", "application/xml", "application/x-javascript",
               "image/svg+xml", "multipart/related", "application/vnd.apple.mpegurl", "message/rfc822",
               "application/x-mimearchive", "application/ld+json")


def kind_of(mime: str | None, path: str | None = None, role: str | None = None) -> str:
    """Preview family used by the UI: image | video | audio | html | har | json | text | other."""
    m = (mime or "").lower()
    p = (path or "").lower()
    if role == "har" or p.endswith(".har"):
        return "har"
    if m == "image/svg+xml":
        return "text"
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    if m.startswith("audio/"):
        return "audio"
    if m in ("text/html", "application/xhtml+xml", "multipart/related", "application/x-mimearchive") \
            or p.endswith((".html", ".htm", ".mhtml", ".mht")):
        return "html"
    if m in ("application/json", "application/ld+json") or p.endswith(".json"):
        return "json"
    if m.startswith("text/") or m in _TEXT_MIMES:
        return "text"
    return "other"


def _check_id(artifact_id: str) -> str:
    if not _HEX64.match(artifact_id):
        raise HTTPException(status_code=400, detail="artifact id must be a sha256 hex digest")
    return artifact_id


def _row(conn: sqlite3.Connection, artifact_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM artifacts WHERE id=?", (_check_id(artifact_id),)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return row


def _path(st: Any, artifact_id: str) -> Path:
    try:
        return st.store.safe_path(artifact_id)  # no symlinks, never outside the CAS (CR-ASTRA-SEC-6)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="artifact registered but original missing from the store") from None


def _links(conn: sqlite3.Connection, artifact_id: str) -> list[dict[str, Any]]:
    return [{"incident_id": r["incident_id"], "role": r["role"], "path_in_incident": safe_ref(r["path_in_incident"]),
             "meta": redact_obj(_json(r["meta"], {}) or {})}
            for r in conn.execute("SELECT incident_id, role, path_in_incident, meta FROM incident_artifacts"
                                  " WHERE artifact_id=? ORDER BY incident_id, path_in_incident", (artifact_id,))]


def _ascii_name(name: str) -> str:
    base = PurePosixPath(name.replace("\\", "/")).name or "artefakt"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:120] or "artefakt"


def _disposition(kind: str, name: str) -> str:
    from urllib.parse import quote

    base = PurePosixPath(name.replace("\\", "/")).name or "artefakt"
    return f"{kind}; filename=\"{_ascii_name(base)}\"; filename*=UTF-8''{quote(base[:200], safe='')}"


# --------------------------------------------------------------------------------------- bytes


@router.get("/artifacts/{artifact_id}/raw")
def artifact_raw(request: Request, artifact_id: str, download: bool = False) -> FileResponse:
    """Original bytes from the CAS. Renderable-but-inert types keep their MIME; anything that could run as a
    document (HTML, SVG, XML, JS, text) is sent as text/plain under a sandbox CSP. Range requests work (video)."""
    st = _st(request)
    with st.reader() as conn:
        row = _row(conn, artifact_id)
        link = conn.execute("SELECT path_in_incident FROM incident_artifacts WHERE artifact_id=? LIMIT 1",
                            (artifact_id,)).fetchone()
    path = _path(st, artifact_id)
    mime = (row["mime"] or "application/octet-stream").lower()
    media_type = mime if _SAFE_INLINE.match(mime) else (
        "text/plain; charset=utf-8" if kind_of(mime) in ("html", "json", "text", "har") else "application/octet-stream")
    name = (link["path_in_incident"] if link else None) or row["source_ref"] or artifact_id
    headers = {
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": _disposition("attachment" if download else "inline", str(name)),
        "Cache-Control": "private, max-age=3600, immutable",
    }
    if not download and media_type.startswith("text/plain"):
        # Inline text is a preview: credentials in forms, HAR headers, JSON and URLs are redacted (CR-ASTRA-SEC-13).
        # `download=1` is the deliberate retrieval of the byte-identical CAS original (evidence).
        data = path.read_bytes()
        safe = redact_document(data, mime)
        if safe is not data:
            headers["Cache-Control"] = "private, no-store"
            headers["X-Hydra-Redacted"] = "1"
            return Response(safe, media_type=media_type, headers=headers)
    return FileResponse(path, media_type=media_type, headers=headers)


def _read_text(path: Path, limit: int) -> tuple[str, bool, str]:
    with path.open("rb") as f:
        data = f.read(limit + 1)
    truncated = len(data) > limit
    data = data[:limit]
    for enc in ("utf-8", "cp1250", "latin-1"):
        try:
            return data.decode(enc), truncated, enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), truncated, "utf-8?"


_ATTR_RE = re.compile(r"""(\s(?:src|href|poster|data-src)\s*=\s*)(["'])([^"'<>]{1,2000})\2""", re.IGNORECASE)
_SRCSET_RE = re.compile(r"""(\ssrcset\s*=\s*)(["'])([^"'<>]{1,4000})\2""", re.IGNORECASE)


def _asset_map(conn: sqlite3.Connection, artifact_id: str) -> dict[str, str]:
    """Relative path (from the saved page's folder) -> artifact id of the page's saved resources."""
    out: dict[str, str] = {}
    for link in conn.execute("SELECT incident_id, path_in_incident FROM incident_artifacts WHERE artifact_id=?",
                             (artifact_id,)).fetchall():
        page_dir = PurePosixPath(link["path_in_incident"]).parent
        for r in conn.execute("SELECT ia.artifact_id, ia.path_in_incident, a.mime FROM incident_artifacts ia JOIN"
                              " artifacts a ON a.id=ia.artifact_id WHERE ia.incident_id=? AND ia.role IN"
                              " ('landing_asset','landing_screenshot','ad_creative','feed_screenshot')",
                              (link["incident_id"],)):
            if not _SAFE_INLINE.match((r["mime"] or "").lower()):
                continue
            p = PurePosixPath(r["path_in_incident"])
            try:
                rel = str(p.relative_to(page_dir)) if str(page_dir) not in ("", ".") else str(p)
            except ValueError:
                rel = str(p)
            rel = unicodedata.normalize("NFC", rel)
            out.setdefault(rel, r["artifact_id"])
            # pages saved by browsers rename resources (`main.js.pobrane`); key the original name too
            out.setdefault(rel.removesuffix(".pobrane").removesuffix(".download"), r["artifact_id"])
    return out


def _norm_ref(ref: str) -> str:
    ref = unicodedata.normalize("NFC", unquote(ref.strip().split("#")[0].split("?")[0]))
    return ref[2:] if ref.startswith("./") else ref


def rewrite_html(text: str, assets: dict[str, str]) -> tuple[str, int]:
    """Point references to saved page resources at /api/v1/artifacts/<id>/raw (root-relative: a srcdoc frame
    resolves it against the UI origin). Remote URLs stay as they are and are blocked by the frame's CSP."""
    count = 0

    def target(ref: str) -> str | None:
        if urlsplit(ref).scheme:
            return None
        aid = assets.get(_norm_ref(ref))
        return f"/api/v1/artifacts/{aid}/raw" if aid else None

    def attr(m: re.Match[str]) -> str:
        nonlocal count
        t = target(m.group(3))
        if t is None:
            return m.group(0)
        count += 1
        return f"{m.group(1)}{m.group(2)}{t}{m.group(2)}"

    def srcset(m: re.Match[str]) -> str:
        nonlocal count
        parts = []
        for item in m.group(3).split(","):
            bits = item.strip().split()
            if bits and (t := target(bits[0])):
                count += 1
                bits[0] = t
            parts.append(" ".join(bits))
        return f"{m.group(1)}{m.group(2)}{', '.join(parts)}{m.group(2)}"

    return _SRCSET_RE.sub(srcset, _ATTR_RE.sub(attr, text)), count


_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?$")


def _local_origin(origin: str | None) -> str | None:
    """The UI origin for the preview CSP (in dev the UI runs on another loopback port than the API)."""
    return origin if origin and _ORIGIN_RE.match(origin) else None


@router.get("/artifacts/{artifact_id}/text")
def artifact_text(request: Request, artifact_id: str, max_bytes: int = Query(TEXT_LIMIT, ge=1024, le=TEXT_LIMIT_MAX),
                  preview: bool = False, origin: str | None = None) -> dict[str, Any]:
    """Text content as JSON, never as a document. `preview=1` on HTML additionally returns `srcdoc`: the page
    with saved local images re-pointed at this origin and a CSP that forbids scripts and remote loads."""
    st = _st(request)
    with st.reader() as conn:
        row = _row(conn, artifact_id)
        links = _links(conn, artifact_id)
        assets = _asset_map(conn, artifact_id) if preview else {}
    role = links[0]["role"] if links else None
    kind = kind_of(row["mime"], links[0]["path_in_incident"] if links else row["source_ref"], role)
    if kind not in ("html", "json", "text", "har"):
        raise HTTPException(status_code=415, detail=f"no text view for {row['mime']}")
    path = _path(st, artifact_id)
    raw_text, truncated, enc = _read_text(path, max_bytes)
    # Previews never show credentials found in the material (CR-ASTRA-SEC-13); the CAS original is unchanged.
    # JSON/HAR is redacted structurally on the whole document BEFORE the preview window is cut (CR-ASTRA-SW-04):
    # a cut window cannot be parsed, so redacting the window would lose the HAR name/value pairing. The whole
    # document is parsed only within its own budget (REDACT_PARSE_LIMIT); above it the window falls back to text
    # redaction, which also covers HAR `"name": …, "value": …` pairs written as text.
    if kind in ("json", "har") and not truncated:
        text = redact_document(raw_text.encode("utf-8"), row["mime"] or "").decode("utf-8")
    elif kind in ("json", "har") and path.stat().st_size <= REDACT_PARSE_LIMIT:
        safe = redact_document(path.read_bytes(), row["mime"] or "")
        text = redact_text(safe[:max_bytes].decode("utf-8", errors="ignore")) or ""
    else:
        text = redact_text(raw_text) or ""
    out: dict[str, Any] = {"artifact_id": artifact_id, "mime": row["mime"], "kind": kind, "size": row["size"],
                           "encoding": enc, "truncated": truncated, "limit": max_bytes, "text": text,
                           "redacted": text != raw_text}
    if preview and kind == "html":
        origin = _local_origin(origin) or str(request.base_url).rstrip("/")
        doc, n = rewrite_html(text, assets)
        csp = (f"default-src 'none'; img-src {origin} data:; media-src {origin}; style-src 'unsafe-inline' {origin};"
               f" font-src {origin} data:; form-action 'none'; base-uri 'none'")
        out["srcdoc"] = f'<meta http-equiv="Content-Security-Policy" content="{csp}">' \
                        f'<meta name="referrer" content="no-referrer">' + doc
        out["rewritten_assets"] = n
    return out


# --------------------------------------------------------------------------------------- derived images


def _thumbs_dir(st: Any) -> Path:
    d = Path(st.settings.work_dir) / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jpeg_response(path: Path) -> FileResponse:
    return FileResponse(path, media_type="image/jpeg", headers={
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"})


def _ffmpeg_frame(src: Path, out: Path, t: float, width: int) -> bool:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise HTTPException(status_code=503, detail="ffmpeg not found on PATH: video frames unavailable")
    tmp = out.with_suffix(".tmp.jpg")
    try:
        subprocess.run([exe, "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale='min({width},iw)':-2", "-q:v", "4", str(tmp)],
                       check=True, capture_output=True, timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        tmp.unlink(missing_ok=True)
        return False
    if not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(out)
    return True


def _width(w: int) -> int:
    return min(THUMB_WIDTHS, key=lambda x: abs(x - w))


@router.get("/artifacts/{artifact_id}/thumb")
def artifact_thumb(request: Request, artifact_id: str, w: int = Query(320, ge=64, le=1280)) -> FileResponse:
    """JPEG thumbnail (images: EXIF-rotated, downscaled; video: frame at 1 s). Cached in <work_dir>/thumbs."""
    st = _st(request)
    width = _width(w)
    with st.reader() as conn:
        row = _row(conn, artifact_id)
    kind = kind_of(row["mime"])
    out = _thumbs_dir(st) / f"{artifact_id}.w{width}.jpg"
    if out.is_file():
        return _jpeg_response(out)
    src = _path(st, artifact_id)
    if kind == "image":
        from hydra.media import load_work_image

        try:
            img = load_work_image(src, max_side=width).image
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"image cannot be decoded: {type(exc).__name__}") from exc
        import uuid

        # unique temp name: two concurrent requests for one thumbnail (constellation panel) shared ".tmp.jpg", and
        # the second `replace` found it gone (HTTP 500 in the Playwright suite, merge-batch5)
        tmp = out.with_name(f"{out.name}.{uuid.uuid4().hex}.tmp")
        try:
            img.save(tmp, format="JPEG", quality=82)
            tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)
        return _jpeg_response(out)
    if kind == "video":
        if _ffmpeg_frame(src, out, 1.0, width) or _ffmpeg_frame(src, out, 0.0, width):
            return _jpeg_response(out)
        raise HTTPException(status_code=422, detail="no decodable video frame")
    raise HTTPException(status_code=415, detail=f"no thumbnail for {row['mime']}")


@router.get("/artifacts/{artifact_id}/frame")
def artifact_frame(request: Request, artifact_id: str, t: float = Query(..., ge=0.0, le=36_000.0),
                   w: int = Query(320, ge=64, le=1280)) -> FileResponse:
    """One video frame at `t` seconds (rounded to 0.1 s), for keyframe strips and matched-segment previews."""
    st = _st(request)
    width = _width(w)
    with st.reader() as conn:
        row = _row(conn, artifact_id)
    if kind_of(row["mime"]) != "video":
        raise HTTPException(status_code=415, detail="frames exist only for video artefacts")
    tenth = round(t * 10)
    out = _thumbs_dir(st) / f"{artifact_id}.t{tenth}.w{width}.jpg"
    if out.is_file() or _ffmpeg_frame(_path(st, artifact_id), out, tenth / 10, width):
        return _jpeg_response(out)
    raise HTTPException(status_code=422, detail="no frame at this time (beyond the end of the video?)")


# --------------------------------------------------------------------------------------- metadata


def _probe(st: Any, artifact_id: str) -> dict[str, Any] | None:
    cache = _thumbs_dir(st) / f"{artifact_id}.probe.json"
    if cache.is_file():
        return _json(cache.read_text(encoding="utf-8"))
    try:
        from hydra.media import probe

        raw = probe(_path(st, artifact_id))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}
    fmt = raw.get("format") or {}
    v = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in raw.get("streams", []) if s.get("codec_type") == "audio"), None)
    rot = 0
    if v:
        for sd in v.get("side_data_list") or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        rot = int(float((v.get("tags") or {}).get("rotate", rot)))
    w, h = (v.get("width"), v.get("height")) if v else (None, None)
    if w and h and abs(rot) % 180 == 90:
        w, h = h, w  # ffmpeg auto-rotates frames; display size is the rotated one
    out = {"duration_s": float(fmt["duration"]) if fmt.get("duration") else None,
           "format": fmt.get("format_name"), "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
           "width": w, "height": h, "rotation": rot,
           "video_codec": v.get("codec_name") if v else None, "fps": v.get("avg_frame_rate") if v else None,
           "audio_codec": a.get("codec_name") if a else None,
           "sample_rate": int(a["sample_rate"]) if a and a.get("sample_rate") else None}
    cache.write_text(json.dumps(out), encoding="utf-8")
    return out


def _image_size(path: Path) -> dict[str, Any] | None:
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as im:
            w, h = ImageOps.exif_transpose(im).size
            return {"width": w, "height": h, "format": im.format}
    except Exception:  # noqa: BLE001
        return None


PROV_KINDS = ("c2pa_manifest", "c2pa_validation", "c2pa_signer", "c2pa_claim_generator", "c2pa_ai_generated",
              "capture_device", "capture_date", "capture_time", "capture_tool", "wm_check", "exif_software")


@router.get("/artifacts/{artifact_id}/info")
def artifact_info(request: Request, artifact_id: str) -> dict[str, Any]:
    """Everything known about one artefact: CAS row, where it appears (incident, role, path), import sources,
    media properties, gene counts and provenance facts (C2PA / capture metadata genes)."""
    st = _st(request)
    with st.reader() as conn:
        row = _row(conn, artifact_id)
        links = _links(conn, artifact_id)
        sources = [{"source_ref": safe_ref(r["source_ref"]), "job_id": r["job_id"], "imported_at": r["imported_at"]}
                   for r in conn.execute("SELECT source_ref, job_id, imported_at FROM artifact_sources WHERE"
                                         " artifact_id=? ORDER BY imported_at", (artifact_id,))]
        counts = [dict(r) for r in conn.execute(
            "SELECT family, status, COUNT(*) AS n FROM genes WHERE json_extract(evidence, '$.artifact_id')=?"
            " GROUP BY 1, 2 ORDER BY 1, 2", (artifact_id,))]
        tcs = sorted({round(float(r[0]), 2) for r in conn.execute(
            "SELECT DISTINCT json_extract(evidence, '$.timecode_s') FROM genes WHERE"
            " json_extract(evidence, '$.artifact_id')=? AND json_extract(evidence, '$.timecode_s') IS NOT NULL",
            (artifact_id,)) if r[0] is not None})
        prov = []
        inc_ids = sorted({lk["incident_id"] for lk in links})
        if inc_ids:
            for r in conn.execute(f"SELECT incident_id, kind, value, status, evidence, extractor FROM genes WHERE"
                                  f" kind IN ({_in(list(PROV_KINDS))}) AND incident_id IN ({_in(inc_ids)})"
                                  f" ORDER BY kind, id", (*PROV_KINDS, *inc_ids)):
                ev = _json(r["evidence"], {}) or {}
                if ev.get("artifact_id") not in (None, artifact_id):
                    continue
                prov.append({"kind": r["kind"], "value": r["value"], "status": r["status"], "note": ev.get("note"),
                             "scope": "artifact" if ev.get("artifact_id") else "incident",
                             "incident_id": r["incident_id"], "extractor": r["extractor"]})
    role = links[0]["role"] if links else None
    kind = kind_of(row["mime"], links[0]["path_in_incident"] if links else row["source_ref"], role)
    media: dict[str, Any] | None = None
    if kind in ("video", "audio"):
        media = _probe(st, artifact_id)
    elif kind == "image":
        media = _image_size(_path(st, artifact_id))
    return {"id": artifact_id, "sha256": artifact_id, "mime": row["mime"], "size": row["size"], "kind": kind,
            "status": row["status"], "observed_at": row["observed_at"], "created_at": row["created_at"],
            "source_ref": safe_ref(row["source_ref"]), "meta": _json(row["meta"], {}) or {},
            "incidents": links, "sources": sources, "media": media, "gene_counts": counts,
            "timecodes": tcs[:2000], "provenance": prov,
            "limits": {"text_bytes": TEXT_LIMIT, "har_bytes": HAR_LIMIT}}


# --------------------------------------------------------------------------------------- genes of one artefact


@router.get("/artifacts/{artifact_id}/genes")
def artifact_genes(request: Request, artifact_id: str, run_id: str | None = None,
                   limit: int = Query(2000, ge=1, le=10_000)) -> dict[str, Any]:
    """Genes whose evidence points at this artefact (all statuses), with document frequency and rarity
    (self-information log2(N/df), a weight, not a probability) in the run's incidents, level/tier from the
    scorer, and the scored pairs where the same kind+value was a reason with evidence on this artefact."""
    st = _st(request)
    with st.reader() as conn:
        _row(conn, artifact_id)
        run = resolve_run(conn, run_id)
        scope = run_incidents(conn, run) if run else []
        rows = conn.execute(
            "SELECT id, incident_id, family, kind, value, vector_ref, quality, status, provenance, evidence, extractor,"
            " dependency_group FROM genes WHERE json_extract(evidence, '$.artifact_id')=? ORDER BY family, kind, id"
            " LIMIT ?", (artifact_id, limit + 1)).fetchall()
        truncated = len(rows) > limit
        rows = rows[:limit]
        pairs_vals = sorted({(r["kind"], r["value"]) for r in rows if r["value"] is not None
                             and r["status"] == "observed"})
        df: dict[tuple[str, str], int] = {}
        if scope and pairs_vals:
            kinds = sorted({k for k, _ in pairs_vals})
            for r in conn.execute(f"SELECT kind, value, COUNT(DISTINCT incident_id) FROM genes WHERE status='observed'"
                                  f" AND kind IN ({_in(kinds)}) AND incident_id IN ({_in(scope)}) GROUP BY 1, 2",
                                  (*kinds, *scope)):
                if (r[0], r[1]) in set(pairs_vals):
                    df[(r[0], r[1])] = r[2]
        incs = sorted({r["incident_id"] for r in rows})
        used: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
        if run and incs:
            runs = _assignment_runs(conn, run)
            seen: set[tuple[str, str, str, str | None]] = set()
            for r in conn.execute(f"SELECT a, b, run_id, probability, hypothesis, detail FROM pairs WHERE run_id IN"
                                  f" ({_in(runs)}) AND (a IN ({_in(incs)}) OR b IN ({_in(incs)}))",
                                  (*runs, *incs, *incs)):
                d = _json(r["detail"], {}) or {}
                for m in (d.get("reasons") or []) + (d.get("contradictions") or []):
                    for side in ("a", "b"):
                        ev = m.get(f"evidence_{side}") or {}
                        if r[side] not in incs or ev.get("artifact_id") not in (artifact_id, None):
                            continue
                        val = m.get(f"value_{side}")
                        key = (r["a"], r["b"], m.get("kind"), val)
                        if key in seen or (ev.get("artifact_id") is None and val is None):
                            continue
                        seen.add(key)
                        used[(m.get("kind"), val)].append({
                            "a": r["a"], "b": r["b"], "run_id": r["run_id"], "bits": m.get("bits"),
                            "probability": r["probability"], "hypothesis": r["hypothesis"],
                            "explanation_pl": m.get("explanation_pl"), "side": side,
                            "evidence": ev, "other_evidence": m.get("evidence_b" if side == "a" else "evidence_a")})
    n = len(scope)
    genes = []
    for r in rows:
        g = dict(r)
        g["evidence"] = _json(g["evidence"], {}) or {}
        level, tier = _gene_level(r)
        g["level"], g["tier"] = level, tier
        d = df.get((r["kind"], r["value"])) if r["value"] is not None else None
        g["doc_freq"] = d
        g["rarity_bits"] = round(math.log2(n / d), 2) if d and n else None
        g["pairs"] = sorted(used.get((r["kind"], r["value"]), []), key=lambda p: -abs(p["bits"] or 0))[:12]
        genes.append(g)
    return {"artifact_id": artifact_id, "run_id": run, "incidents_in_run": n, "total": len(genes),
            "truncated": truncated, "by_family": dict(Counter(g["family"] for g in genes)),
            "by_status": dict(Counter(g["status"] for g in genes)), "genes": genes}


# --------------------------------------------------------------------------------------- HAR


def _redact(url: str | None) -> str | None:
    try:
        return redact_url(url)
    except Exception:  # noqa: BLE001 — a malformed URL is still redacted as text, never returned raw
        return redact_text(url)


@router.get("/artifacts/{artifact_id}/har")
def artifact_har(request: Request, artifact_id: str, limit: int = Query(1000, ge=1, le=5000)) -> dict[str, Any]:
    """HAR network log as a table (host, status, type, size, time, redirect target) plus redirect chains
    reconstructed from 3xx `redirectURL` / Location. URLs pass through hydra.redact (tokens never leave)."""
    st = _st(request)
    with st.reader() as conn:
        row = _row(conn, artifact_id)
    path = _path(st, artifact_id)
    if row["size"] and row["size"] > HAR_LIMIT:
        raise HTTPException(status_code=413, detail=f"HAR larger than {HAR_LIMIT // (1024 * 1024)} MB")
    try:
        doc = json.loads(path.read_bytes().decode("utf-8", errors="replace"))
        entries = doc["log"]["entries"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"not a HAR log: {type(exc).__name__}") from exc
    rows = []
    for i, e in enumerate(entries):
        req, resp = e.get("request") or {}, e.get("response") or {}
        url = str(req.get("url") or "")
        loc = resp.get("redirectURL") or next((h.get("value") for h in resp.get("headers") or []
                                               if str(h.get("name", "")).lower() == "location"), None)
        content = resp.get("content") or {}
        rows.append({"idx": i, "method": req.get("method"), "url": _redact(url), "host": urlsplit(url).hostname,
                     "status": resp.get("status"), "mime": (content.get("mimeType") or "").split(";")[0] or None,
                     "size": content.get("size") if isinstance(content.get("size"), int) else None,
                     "time_ms": round(float(e["time"]), 1) if isinstance(e.get("time"), (int, float)) else None,
                     "started": e.get("startedDateTime"), "redirect": _redact(loc) if loc else None,
                     "resource_type": e.get("_resourceType"), "server_ip": e.get("serverIPAddress"),
                     "_url": url, "_loc": loc})
    by_url: dict[str, int] = {}
    for r in rows:
        by_url.setdefault(r["_url"], r["idx"])
    chains, in_chain = [], set()
    for r in rows:
        if r["idx"] in in_chain or not r["_loc"] or not (300 <= int(r["status"] or 0) < 400):
            continue
        chain, cur, guard = [r["idx"]], r, 0
        while cur and cur["_loc"] and guard < 20:
            nxt_url = cur["_loc"] if urlsplit(cur["_loc"]).scheme else (
                f"{urlsplit(cur['_url']).scheme}://{urlsplit(cur['_url']).netloc}{cur['_loc']}")
            j = by_url.get(nxt_url)
            if j is None or j in chain:
                chain.append(-1)  # target not in the log
                break
            chain.append(j)
            cur = rows[j]
            guard += 1
        in_chain.update(i for i in chain if i >= 0)
        chains.append([{"idx": i, "host": rows[i]["host"], "status": rows[i]["status"], "url": rows[i]["url"]}
                       if i >= 0 else {"idx": None, "host": None, "status": None, "url": rows[chain[-2]]["redirect"]}
                       for i in chain])
    for r in rows:
        r.pop("_url")
        r.pop("_loc")
    hosts = Counter(r["host"] for r in rows if r["host"])
    return {"artifact_id": artifact_id, "entries": len(rows), "truncated": len(rows) > limit, "rows": rows[:limit],
            "hosts": [{"host": h, "requests": n} for h, n in hosts.most_common(50)],
            "statuses": dict(Counter(str(r["status"]) for r in rows)), "redirect_chains": chains[:50],
            "pages": [{"title": redact_text(str(p.get("title") or "")) or None, "started": p.get("startedDateTime")}
                      for p in (doc.get("log", {}).get("pages") or [])][:20],
            "creator": (doc.get("log", {}).get("creator") or {}).get("name")}


# --------------------------------------------------------------------------------------- ASR transcript


@router.get("/artifacts/{artifact_id}/transcript")
def artifact_transcript(request: Request, artifact_id: str) -> dict[str, Any]:
    """ASR segments with timecodes (cache written by hydra.genome.aud.transcribe). When ASR did not run, the
    status and reason of the `asr_transcript` gene are returned instead: unknown, not an empty transcript."""
    st = _st(request)
    with st.reader() as conn:
        _row(conn, artifact_id)
        g = conn.execute("SELECT status, evidence FROM genes WHERE kind='asr_transcript' AND"
                         " json_extract(evidence, '$.artifact_id')=? ORDER BY id DESC LIMIT 1",
                         (artifact_id,)).fetchone()
    gene = {"status": g["status"], "note": (_json(g["evidence"], {}) or {}).get("note")} if g else None
    roots = [r for r in (st.settings.cache_dir, st.settings.work_dir) if r]
    for root in roots:
        d = Path(root) / "asr"
        if not d.is_dir():
            continue
        for p in sorted(d.glob(f"{artifact_id}.whisper-*.json")):
            data = _json(p.read_text(encoding="utf-8"), {}) or {}
            segs = [{"start": s.get("start"), "end": s.get("end"), "text": redact_text(s.get("text"))}
                    for s in data.get("segments") or []]
            return {"artifact_id": artifact_id, "available": True, "model": data.get("model"), "segments": segs,
                    "gene": gene}
    return {"artifact_id": artifact_id, "available": False, "model": None, "segments": [], "gene": gene}


# --------------------------------------------------------------------------------------- dashboard strip


@router.get("/materials")
def materials(request: Request, recent: int = Query(18, ge=0, le=100)) -> dict[str, Any]:
    """Counts of stored artefacts by preview type, role and corpus, plus the newest visual artefacts."""
    from hydra.api_visual import _corpus_of

    st = _st(request)
    with st.reader() as conn:
        by_kind: Counter = Counter()
        bytes_by_kind: Counter = Counter()
        for r in conn.execute("SELECT a.mime, a.size, (SELECT path_in_incident FROM incident_artifacts WHERE"
                              " artifact_id=a.id LIMIT 1) AS p, (SELECT role FROM incident_artifacts WHERE"
                              " artifact_id=a.id LIMIT 1) AS role FROM artifacts a"):
            k = kind_of(r["mime"], r["p"], r["role"])
            by_kind[k] += 1
            bytes_by_kind[k] += int(r["size"] or 0)
        by_role = {r[0]: r[1] for r in conn.execute("SELECT role, COUNT(*) FROM incident_artifacts GROUP BY 1")}
        by_corpus: Counter = Counter()
        kind_by_corpus: dict[str, Counter] = defaultdict(Counter)
        for r in conn.execute("SELECT i.source, a.mime, ia.path_in_incident, ia.role FROM incident_artifacts ia"
                              " JOIN incidents i ON i.id=ia.incident_id JOIN artifacts a ON a.id=ia.artifact_id"):
            c = _corpus_of(r["source"])
            by_corpus[c] += 1
            kind_by_corpus[c][kind_of(r["mime"], r["path_in_incident"], r["role"])] += 1
        items = []
        if recent:
            for r in conn.execute(
                "SELECT a.id, a.mime, a.size, a.created_at, ia.incident_id, ia.role, ia.path_in_incident FROM artifacts"
                " a JOIN incident_artifacts ia ON ia.artifact_id=a.id WHERE (a.mime LIKE 'image/%' AND a.mime !="
                " 'image/svg+xml' OR a.mime LIKE 'video/%') AND ia.role != 'landing_asset' GROUP BY a.id"
                " ORDER BY a.created_at DESC, ia.incident_id LIMIT ?", (recent,)):
                items.append({"artifact_id": r["id"], "mime": r["mime"], "size": r["size"],
                              "kind": kind_of(r["mime"]), "created_at": r["created_at"],
                              "incident_id": r["incident_id"], "role": r["role"],
                              "path_in_incident": safe_ref(r["path_in_incident"])})
        total = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM artifacts").fetchone()
    return {"total": total[0], "bytes": total[1], "by_kind": dict(by_kind), "bytes_by_kind": dict(bytes_by_kind),
            "by_role": by_role, "by_corpus": dict(by_corpus),
            "kind_by_corpus": {c: dict(v) for c, v in kind_by_corpus.items()}, "recent": items}
