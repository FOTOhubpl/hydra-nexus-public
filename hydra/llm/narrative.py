"""`llm_narrative@1`: optional NAR extractor that asks Claude to read the ad creative and the landing text.

Input: up to two ad creative images (downscaled, JPEG) or, without creatives, a feed screenshot / video frame /
landing screenshot, plus the visible text already extracted by `txt_sources.collect` (ad OCR, platform metadata,
landing title/headline/lead/body). Text is redacted before sending (hydra.llm.client).
Output: STRICT JSON (structured output, `NARRATIVE_SCHEMA`) → NAR genes, provenance INF, dependency group
`nar:llm`, kinds prefixed `llm_` (so they never feed the rule-based `scheme` contradiction check).

How the scorer sees them: NAR genes are kit/style level and the NAR campaign cap is 0
(`hydra.synapse.FAMILY_CAMPAIGN_CAP`), so these genes can never create or support a campaign link. They are
inputs for campaign groups (style bits, capped), briefs and search. The LLM is never a linking rule.

The extractor is NOT in `base.load_family_modules`, so the default pipeline neither imports nor runs it.
Run it with `annotate(...)` (CLI: `python -m hydra.llm narrative`), which appends `llm_narrative@1` genes to
the stored genome without touching other genes. Disabled or unreachable → one `unavailable` gene with a Polish
reason; replay mode serves the cache.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from hydra.config import Settings
from hydra.contracts import ArtifactRole, EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedArtifact, ResolvedIncident, register
from hydra.llm.client import LLMClient, LLMResult, get_client

log = logging.getLogger(__name__)

NAME = "llm_narrative"
VERSION = 1
TAG = f"{NAME}@{VERSION}"
GROUP = "nar:llm"
IMAGE_MAX_SIDE = 1024
JPEG_QUALITY = 85
MAX_IMAGES = 2
MAX_AD_CHARS = 3000
MAX_PAGE_CHARS = 6000
SCHEMES = ["investment", "health_supplement", "gambling", "fake_news_bait", "giveaway", "phishing",
           "loan_or_debt", "shopping", "other"]
NOISE_ZONES = {"feed", "library_panel"}

NARRATIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scheme": {"type": "string", "enum": SCHEMES},
        "impersonated_brands": {"type": "array", "items": {"type": "string"}},
        "named_persons": {"type": "array", "items": {"type": "string"}},
        "product": {"type": "string"},
        "promise": {"type": "string"},
        "promise_amount": {"type": "string"},
        "cta": {"type": "string"},
        "urgency": {"type": "array", "items": {"type": "string"}},
        "language_tricks": {"type": "array", "items": {"type": "string"}},
        "target_audience": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["scheme", "impersonated_brands", "named_persons", "product", "promise", "promise_amount", "cta",
                 "urgency", "language_tricks", "target_audience", "confidence"],
    "additionalProperties": False,
}

SYSTEM = """You assist fraud analysts at a national CERT. You read evidence of one reported online scam \
advertisement: images of the ad creative and the visible text of the ad and of its landing page. Describe only \
what the material shows. Do not guess, do not attribute the scam to any operator, do not browse.

Return one JSON object with these fields:
- scheme: the scam type. investment = fake trading/crypto/"AI platform" investing; health_supplement = miracle \
cures, supplements, fake doctors; gambling = casino/betting apps; fake_news_bait = sensational fake news article \
used as bait (choose it only when no other scheme is visible); giveaway = prizes, lotteries, free products; \
phishing = credential/payment data theft (fake bank, courier, government login); loan_or_debt = loans, debt \
relief; shopping = fake shop / heavy discount; other = none of these.
- impersonated_brands: media outlets, banks, companies, institutions or platforms the material pretends to be \
or to be endorsed by (as written in the material).
- named_persons: names of real people that are visible or stated in the material (e.g. a celebrity or \
politician used as endorser). Never identify people from their faces alone; empty list when no name is shown.
- product: the advertised product, platform or app name, "" if none.
- promise: the main promise in a few words (e.g. "zarobek bez wysiłku"), "" if none.
- promise_amount: the amount, percent or return as written (e.g. "4000 zł tygodniowo", "90%"), "" if none.
- cta: the call to action as written (e.g. "zarejestruj się"), "" if none.
- urgency: urgency or scarcity cues as written (e.g. "tylko dziś", "ostatnie miejsca").
- language_tricks: manipulation techniques, each as a short label in the language of the material (e.g. \
"fałszywy autorytet", "fałszywe komentarze", "presja czasu", "sensacyjny nagłówek", "obietnica bez ryzyka").
- target_audience: who the ad addresses, a few words, "" if unclear.
- confidence: 0..1, how clearly the material supports the scheme.

Write every value in the language of the material (Polish material → Polish values), short, without quotes."""


# Local backend (Bielik via llama-server): text only, shorter Polish prompt, bounded arrays/strings so the grammar
# cannot run away on a small model.
SYSTEM_LOCAL = """Jesteś analitykiem CERT. Czytasz tekst jednej oszukańczej reklamy i jej strony docelowej. \
Opisz tylko to, co jest w tekście. Zwróć jeden obiekt JSON:
scheme: investment (fałszywe inwestycje, krypto, platforma AI) | health_supplement (cudowne leki, suplementy) | \
gambling (kasyno, zakłady) | fake_news_bait (sensacyjny fałszywy artykuł, tylko gdy brak innego schematu) | \
giveaway (nagrody, loterie) | phishing (kradzież danych logowania lub płatności) | loan_or_debt | shopping | other;
impersonated_brands: media, banki, firmy, instytucje, pod które podszywa się materiał;
named_persons: imiona i nazwiska osób wymienionych w tekście (pusta lista, gdy brak);
product, promise, promise_amount (kwota lub procent dokładnie jak w tekście), cta, target_audience: krótkie frazy \
albo "";
urgency: zwroty presji czasu; language_tricks: techniki manipulacji, np. "fałszywy autorytet";
confidence: liczba 0..1. Wartości po polsku, krótko, bez cudzysłowów."""


def schema_for_local(schema: dict[str, Any]) -> dict[str, Any]:
    props = {}
    for k, v in schema["properties"].items():
        if v.get("type") == "array":
            v = {**v, "maxItems": 5, "items": {"type": "string", "maxLength": 60}}
        elif v.get("type") == "string" and "enum" not in v:
            v = {**v, "maxLength": 80}
        props[k] = v
    return {**schema, "properties": props}


# --------------------------------------------------------------------------------------- inputs


def _image_size(a: ResolvedArtifact) -> int:
    w, h = a.meta.meta.get("width"), a.meta.meta.get("height")
    if w and h:
        return int(w) * int(h)
    try:
        from PIL import Image

        with Image.open(a.path) as im:
            return im.size[0] * im.size[1]
    except Exception:  # noqa: BLE001
        return 0


def _is_image(a: ResolvedArtifact) -> bool:
    return a.meta.mime.startswith("image/") and not a.meta.mime.endswith("svg+xml")


def pick_images(inc: ResolvedIncident, ctx: ExtractContext | None = None) -> list[tuple[str, Path]]:
    """Best visual evidence: ad creatives (largest first), else feed screenshot, else an ad-video frame,
    else a landing screenshot. Returns (artifact_id or artifact_id#t=sec, path)."""
    for roles in ((ArtifactRole.AD_CREATIVE,), (ArtifactRole.FEED_SCREENSHOT,)):
        imgs = [a for a in inc.by_role(*roles) if _is_image(a)]
        if imgs:
            imgs.sort(key=lambda a: (-_image_size(a), a.meta.artifact_id))
            return [(a.meta.artifact_id, a.path) for a in imgs[:MAX_IMAGES]]
    if ctx is not None:
        for a in inc.by_role(ArtifactRole.AD_VIDEO, ArtifactRole.SCREEN_RECORDING):
            try:
                from hydra.genome.txt_sources import video_frames

                frames = video_frames(a, ctx, 8)
            except Exception:  # noqa: BLE001 — no ffmpeg / broken video: skip
                continue
            if frames:
                fp, tc = frames[len(frames) // 2]
                return [(f"{a.meta.artifact_id}#t={tc:.1f}", fp)]
    imgs = [a for a in inc.by_role(ArtifactRole.LANDING_SCREENSHOT) if _is_image(a)]
    return [(a.meta.artifact_id, a.path) for a in imgs[:1]]


def encode_image(path: Path, max_side: int = IMAGE_MAX_SIDE) -> str | None:
    """Downscaled RGB JPEG, base64. None when the file is not a readable image."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception:  # noqa: BLE001
        return None
    return base64.standard_b64encode(buf.getvalue()).decode()


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + " […tekst skrócony]"


def visible_texts(inc: ResolvedIncident, ctx: ExtractContext) -> tuple[str, str]:
    """(ad text, landing text) from the shared text collection (OCR is cached on disk)."""
    from hydra.genome.txt_sources import collect

    texts = collect(inc, ctx)
    ad: list[str] = []
    page: list[str] = []
    seen: set[str] = set()
    for b in texts.blocks:
        target = None
        if b.channel == "ad" and b.source in ("ocr_image", "ocr_frame"):
            target = ad
        elif b.channel == "meta" and b.source in ("meta_title", "meta_description"):
            target = ad
        elif b.channel == "page" and b.source in ("html_title", "html_headline", "html_lead", "html_body",
                                                   "ocr_image"):
            target = page
        elif b.channel == "text":
            target = ad
        if target is None:
            continue
        for s in b.segments:
            if not s.text or s.chrome or s.zone in NOISE_ZONES or s.text in seen:
                continue
            seen.add(s.text)
            target.append(s.text)
    return _clip("\n".join(ad), MAX_AD_CHARS), _clip("\n".join(page), MAX_PAGE_CHARS)


# --------------------------------------------------------------------------------------- normalisation


_EDGE = re.compile(r"^[\s\"'„”“»«.,;:!?()\[\]-]+|[\s\"'„”“»«.,;:!?()\[\]-]+$")


def norm(value: Any, n: int = 120) -> str:
    """Lowercase, single spaces, edge punctuation removed; diacritics kept."""
    s = " ".join(str(value or "").lower().split())
    s = _EDGE.sub("", s)
    return s[:n]


def _list(v: Any) -> list[str]:
    items = v if isinstance(v, list) else ([v] if v else [])
    out: list[str] = []
    for x in items:
        s = norm(x)
        if s and s not in out:
            out.append(s)
    return out[:12]


FIELD_KINDS = (  # json field → gene kind, list?
    ("impersonated_brands", "llm_brand", True),
    ("named_persons", "llm_person", True),
    ("product", "llm_product", False),
    ("promise", "llm_promise", False),
    ("promise_amount", "llm_promise_amount", False),
    ("cta", "llm_cta", False),
    ("urgency", "llm_urgency", True),
    ("language_tricks", "llm_trick", True),
    ("target_audience", "llm_audience", False),
)


def genes_from_json(incident_id: str, data: dict[str, Any], res: LLMResult, artifact_id: str | None) -> list[Gene]:
    conf = data.get("confidence")
    try:
        q = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        q = 0.5
    note = f"Claude ({res.model}), wnioskowanie z obrazu i tekstu; klucz cache {res.key[:16]}; pewność {q:.2f}"

    def gene(kind: str, value: str) -> Gene:
        return Gene(incident_id=incident_id, family=Family.NAR, kind=kind, value=value, quality=round(q, 3),
                    status=GeneStatus.OBSERVED, provenance=Provenance.INF, extractor=TAG, dependency_group=GROUP,
                    evidence=EvidencePointer(artifact_id=artifact_id, note=note[:300]))

    scheme = norm(data.get("scheme")) or "other"
    out = [gene("llm_scheme", scheme if scheme in SCHEMES else "other")]
    for field_name, kind, is_list in FIELD_KINDS:
        vals = _list(data.get(field_name)) if is_list else [norm(data.get(field_name))]
        out.extend(gene(kind, v) for v in vals if v)
    return out


def status_gene(incident_id: str, status: GeneStatus, reason_pl: str) -> Gene:
    return Gene(incident_id=incident_id, family=Family.NAR, kind=NAME, value=None, quality=0.0, status=status,
                provenance=Provenance.INF, extractor=TAG, dependency_group=GROUP,
                evidence=EvidencePointer(note=reason_pl[:300]))


# --------------------------------------------------------------------------------------- extractor


def analyse(inc: ResolvedIncident, ctx: ExtractContext, client: LLMClient | None = None) -> tuple[ExtractResult,
                                                                                              LLMResult | None]:
    iid = inc.incident.id
    llm = client or ctx.models.get("llm_client") or get_client(ctx.settings)
    local = llm.cfg.is_local
    images = [] if local else pick_images(inc, ctx)  # Bielik is a text model: no images on the local backend
    ad_text, page_text = visible_texts(inc, ctx)
    content: list[dict[str, Any]] = []
    sent_ids: list[str] = []
    for aid, path in images:
        b64 = encode_image(path)
        if b64 is None:
            continue
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
        sent_ids.append(aid)
    if not content and not ad_text and not page_text:
        return ExtractResult(genes=[status_gene(iid, GeneStatus.NOT_APPLICABLE,
                                                "Brak obrazu reklamy i tekstu do analizy LLM.")]), None
    title = inc.incident.title or ""
    content.append({"type": "text", "text": (
        f"Images above: {len(sent_ids)} (ad creative first).\n\n"
        f"<ad_text>\n{ad_text or '(brak)'}\n</ad_text>\n\n<landing_text>\n{page_text or '(brak)'}\n</landing_text>\n\n"
        f"<report_title>{title}</report_title>\n\nReturn the JSON object.")})
    digest = hashlib.sha256(json.dumps({"images": sent_ids, "max_side": IMAGE_MAX_SIDE, "q": JPEG_QUALITY,
                                        "v": VERSION}, sort_keys=True).encode()).hexdigest()
    if local:
        content[-1]["text"] = (f"<tekst_reklamy>\n{ad_text[:2000] or '(brak)'}\n</tekst_reklamy>\n\n<strona>\n"
                               f"{page_text[:3000] or '(brak)'}\n</strona>\n\n<tytul>{title}</tytul>\n\nZwróć JSON.")
    res = llm.complete(task="narrative", model=llm.cfg.fast_model, system=SYSTEM_LOCAL if local else SYSTEM,
                       content=content, inputs_digest=digest,
                       schema=schema_for_local(NARRATIVE_SCHEMA) if local else NARRATIVE_SCHEMA,
                       max_tokens=1000 if local else 4000, effort=None if local else "low")
    if not res.ok or not isinstance(res.data, dict):
        return ExtractResult(genes=[status_gene(iid, GeneStatus.UNAVAILABLE,
                                                res.reason_pl or "Analiza LLM niedostępna.")]), res
    first = sent_ids[0].split("#")[0] if sent_ids else None
    return ExtractResult(genes=genes_from_json(iid, res.data, res, first)), res


@register(NAME, family=Family.NAR, version=VERSION)
def llm_narrative(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    return analyse(inc, ctx)[0]


# --------------------------------------------------------------------------------------- standalone run


def _persist(conn: Any, iid: str, genes: list[Gene]) -> None:
    from hydra import events
    from hydra.pipeline import _clean_gene

    now = events.utcnow()
    conn.execute("DELETE FROM genes WHERE incident_id = ? AND extractor LIKE ?", (iid, f"{NAME}@%"))
    conn.executemany(
        "INSERT INTO genes (incident_id, family, kind, value, vector_ref, quality, status, provenance, evidence,"
        " extractor, dependency_group, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(iid, g.family, g.kind, g.value, g.vector_ref, g.quality, g.status, g.provenance,
          events.canonical_json(g.evidence.model_dump(exclude_none=True)), g.extractor, g.dependency_group, now)
         for g in (_clean_gene(x) for x in genes)],
    )
    events.append(conn, "LLM_NARRATIVE", {"incident_id": iid, "genes": len(genes), "extractor": TAG})


def annotate(incident_ids: list[str], settings: Settings, writer: Any, store: Any,
             client: LLMClient | None = None) -> list[dict[str, Any]]:
    """Run llm_narrative on stored incidents and replace their `llm_narrative@*` genes (other genes untouched).
    Returns per-incident stats (status, cached, tokens, latency, estimated cost)."""
    from contextlib import closing

    from hydra.adapters.nask_folder import load_incident, resolve
    from hydra.db import connect

    llm = client or get_client(settings)
    out = []
    for iid in incident_ids:
        with closing(connect(settings.db_path, readonly=True)) as conn:
            conn.row_factory = __import__("sqlite3").Row
            inc = load_incident(conn, iid)
        if inc is None:
            out.append({"incident_id": iid, "status": "missing"})
            continue
        work = settings.work_dir / "incidents" / iid
        work.mkdir(parents=True, exist_ok=True)
        ctx = ExtractContext(settings=settings, work_dir=work, models={"llm_client": llm})
        try:
            ex, res = analyse(resolve(inc, store), ctx, llm)
        except Exception as exc:  # noqa: BLE001 — never fail the batch on one incident
            log.warning("llm_narrative failed on %s", iid, exc_info=True)
            ex, res = ExtractResult(genes=[status_gene(iid, GeneStatus.FAILED, f"{type(exc).__name__}: {exc}")]), None
        writer.run(lambda conn, i=iid, g=ex.genes: _persist(conn, i, g), timeout=120)
        out.append({"incident_id": iid, "genes": len(ex.genes),
                    **({"status": "not_applicable"} if res is None else res.summary())})
    return out


__all__ = ["NARRATIVE_SCHEMA", "SCHEMES", "analyse", "annotate", "genes_from_json", "llm_narrative", "norm"]
