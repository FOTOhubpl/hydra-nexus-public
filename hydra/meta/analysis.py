"""Outbox worker: one queued revision -> materials -> HYDRA incident -> extraction -> open-set decision -> triage ->
manipulation status -> stored analysis + report id + audit event. Runs outside SQLite transactions (each step that
writes uses the single Writer); a job is leased, so a crash or restart re-runs it and the idempotency key
(ad, revision content, extraction version, correlation context) makes the effects land once:

- incident id `metaad-<mode>-<ad>-<sha8 of revision content>` (import is idempotent for the same bytes);
- corpus `ext:meta-adlibrary` (live) / `ext:meta-adlibrary:recorded` / `ext:meta-adlibrary:synthetic`: never `nask`
  (hydra.corpus.corpus_of), so the default NASK mode never sees these incidents;
- decision through the public pipeline path `extract` + `assign_stored` (open set + conformal against the base run;
  scoring code untouched), persisted as a normal `assign` run (`assignments` row, so the existing explain views work)
  in the same transaction as the analysis row; `meta_analyses.idem_key` is UNIQUE.

Three independent results, never merged into one score: correlation (the assigner's own decision and evidence),
triage of fraud indications (rule-based reasons, never a confirmation), manipulation signals per modality (no
validated detector in HYDRA: `unavailable`, never "authentic").
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from hydra.db import connect
from hydra.meta import normalize, store

log = logging.getLogger("hydra.meta")
TRIAGE_RULES = "meta-triage@2"
SOURCE_BY_MODE = {"live": "ext:meta-adlibrary", "recorded": "ext:meta-adlibrary:recorded",
                  "synthetic": "ext:meta-adlibrary:synthetic"}
PROFIT = ("zarabiaj", "zarobek", "zarobić", "pasywny dochód", "inwestycj", "platforma inwestycyjna", "kryptowalut",
          "bitcoin", "zysk", "gwarantowany", "bez ryzyka")
TRIAGE_NOTE_PL = ("Przesłanki to sygnały do weryfikacji przez analityka, nie potwierdzenie oszustwa. Słowo kluczowe, "
                  "znana osoba czy młoda domena same niczego nie przesądzają; przypisanie do kampanii HYDRY nie "
                  "oznacza, że kampania została zweryfikowana jako oszustwo.")
CORRELATION_NOTE_PL = ("Decyzja otwartego zbioru HYDRY (przypisanie / nowy wzorzec / abstencja) względem bazowego "
                       "przebiegu; identyfikator strony, nazwa strony, deklarowany płatnik ani identyczne zdjęcie nie "
                       "są samodzielnym dowodem wspólnego operatora.")
NO_DETECTOR_PL = ("HYDRA nie ma zwalidowanego detektora manipulacji dla tej modalności; eksperymentalne sygnały DFK są "
                  "domyślnie wyłączone (docs/genes/DFK.md). Brak wyniku nie oznacza autentyczności.")


def incident_id(mode: str, ad_id: str, content_sha: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in ad_id)[:40]
    return f"metaad-{mode[:3]}-{safe}-{content_sha[:8]}"


def extractors_from_env() -> list[str] | None:
    v = os.environ.get("HYDRA_META_EXTRACTORS", "").strip()
    return [x.strip() for x in v.split(",") if x.strip()] or None


# ------------------------------------------------------------------------------------------------ folder
def build_folder(root: Path, content: dict[str, Any], files: list[tuple[str, str, str]], cas_dir: Path
                 ) -> dict[str, Any]:
    """Incident folder for the normal importer. Roles are explicit (never sniffed from names). `ad.json` holds the
    normalised metadata as plain JSON (no Ad Library URL, no `.info.json` shape): page id / name are shown, not turned
    into operator genes (CR-META-COLLECT-1)."""
    from hydra.contracts import ArtifactRole
    from hydra.storage import ContentStore

    cr, meta = content["creative"], content["meta"]
    root.mkdir(parents=True, exist_ok=True)
    roles: dict[str, Any] = {}
    doc = {"source": "meta-adlibrary", "source_ad_id": cr["source_ad_id"], "source_page_id": cr.get("source_page_id"),
           "source_page_name": cr.get("source_page_name"), "publisher_platforms": meta.get("publisher_platforms"),
           "languages": meta.get("languages"), "source_delivery_start": meta.get("source_delivery_start"),
           "source_delivery_stop": meta.get("source_delivery_stop"), "title": (cr.get("creative_link_titles") or
                                                                              cr.get("creative_bodies") or [None])[0]}
    (root / "ad.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    roles["ad.json"] = ArtifactRole.PLATFORM_METADATA
    text = normalize.text_of(cr)
    if text:
        (root / "creative.txt").write_text(text, encoding="utf-8")
        roles["creative.txt"] = ArtifactRole.TEXT
    urls = [u["url"] for u in cr.get("landing_urls") or []]
    if urls:
        (root / "landing_urls.txt").write_text("\n".join(urls) + "\n", encoding="utf-8")
        roles["landing_urls.txt"] = ArtifactRole.URL if len(urls) == 1 else ArtifactRole.TEXT
    cas = ContentStore(cas_dir)
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "video/mp4": ".mp4",
           "video/quicktime": ".mov", "video/webm": ".webm", "text/html": ".html"}
    for i, (dg, role, mime) in enumerate(files):
        name = f"{role}-{i:02d}-{dg[:12]}{ext.get(mime, '.bin')}"
        (root / name).write_bytes(cas.read_bytes(dg))
        roles[name] = {"ad_creative": ArtifactRole.AD_CREATIVE, "ad_video": ArtifactRole.AD_VIDEO,
                       "landing_html": ArtifactRole.LANDING_HTML}.get(role, ArtifactRole.OTHER)
    return roles


# ------------------------------------------------------------------------------------------------ results
def correlation_view(a: Any, base_run: str | None, assign_run: str | None, n_media: int) -> dict[str, Any]:
    cands, evidence = [], []
    for c in a.candidates[:5]:
        bp = c.best_pair
        cands.append({"campaign_id": c.campaign_id, "probability": round(c.probability, 4), "support": c.support,
                      "total_bits": round(bp.total_bits, 2) if bp else None,
                      "hypothesis": str(bp.hypothesis) if bp else None})
        if bp and not evidence:
            for r in bp.reasons[:12]:
                evidence.append({"kind": r.kind, "value": (r.value_a or r.value_b or "")[:200],
                                 "bits": round(r.bits, 2), "level": (bp.meta.get("levels") or {}).get(r.kind)
                                 if isinstance(bp.meta.get("levels"), dict) else None,
                                 "family": str(r.family), "other": r.value_b if r.value_b != r.value_a else None})
    meta = a.meta or {}
    return {"decision": str(a.decision), "decision_pl": store.DECISION_PL.get(str(a.decision)),
            "campaign_id": a.campaign_id, "group_id": a.group_id, "reason_pl": a.reason_pl, "model": a.model,
            "candidates": cands, "evidence": evidence, "base_run_id": base_run, "assign_run_id": assign_run,
            "conformal": meta.get("conformal"), "onboarding": bool(meta.get("onboarding")),
            "no_features": bool(meta.get("no_features")), "note_pl": CORRELATION_NOTE_PL,
            "coverage_pl": None, "media_analysed": n_media}


BRANDS = ("orlen", "pkn", "lotos", "pge", "baltic pipe", "baltica", "gpw", "knf", "zus", "nfz", "pko", "pekao",
          "mbank", "santander", "ing ", "inpost", "poczta polska", "allegro", "olx", "blik", "ministerstw", "rząd",
          "gov", "nbp", "biedronka", "lidl")
_INCOME_RE = re.compile(r"\d[\d\s.,]{1,9}\s?(?:zł|pln|euro|eur|€|usd|\$)\b[^.\n]{0,40}?"
                        r"(?:dzienn|tygodniow|miesięczn|miesiąc|dzień|tydzień|na start|pasywn)", re.I)
_ENDORSE_RE = re.compile(r"celebryt|znan[ya]\s+(?:aktor|aktork|dziennikar|ekonomist|milioner|prezenter|piosenkar)|"
                         r"wywiad|ujawni[łl]|sekretn\w* spos|tajemnic\w* sukces|zdradzi[łl]", re.I)
_MESSENGER_RE = re.compile(r"\b(?:wa\.me|t\.me|m\.me|telegram|whatsapp)\b", re.I)
PATTERN_CODES = {"brand_in_page_name", "state_company_investment", "endorsement_story", "specific_income_promise",
                 "same_text_many_pages", "messenger_contact", "page_name_pattern"}


def page_name_hits(page_name: str | None, patterns: list[str]) -> list[str]:
    """Operator rule "wzorzec nazwy strony" (case-insensitive regular expressions, validated on save)."""
    out = []
    for p in patterns or []:
        try:
            if page_name and re.search(p, page_name, re.I):
                out.append(p)
        except re.error:
            continue
    return out


def pattern_reasons(content: dict[str, Any], same_text_pages: list[str], rules: dict[str, Any] | None = None
                    ) -> list[dict[str, Any]]:
    """Field-based indicators beyond single keywords (meta-triage@2). Each one is a pattern to verify, explained in
    Polish; none confirms fraud. Page age is not in the Ad Library API (ArchivedAd has no page creation date)."""
    cr = content["creative"]
    text = normalize.text_of(cr)
    low = " " + text.lower() + " "
    page = (cr.get("source_page_name") or "").lower()
    profit = any(p in low for p in PROFIT)
    out: list[dict[str, Any]] = []

    def add(code: str, desc: str, src: str = "pola reklamy") -> None:
        out.append({"code": code, "description_pl": desc, "artifact": "ad.json", "source": src,
                    "rule_version": TRIAGE_RULES})

    hit = next((b.strip() for b in BRANDS if b in f" {page} "), None)
    if hit:
        add("brand_in_page_name", f"Nazwa strony reklamodawcy zawiera markę lub instytucję „{hit}” "
                                  + ("i reklama obiecuje zysk: możliwe podszycie (sprawdź, czy to oficjalna strona)."
                                     if profit else "(sprawdź, czy to oficjalna strona marki)."))
    st = next((b.strip() for b in ("orlen", "baltic pipe", "baltica", "pge", "lotos", "gpw", "pkn") if b in low), None)
    if st and profit:
        add("state_company_investment", f"Inwestycja „w spółkę państwową / energetyczną” ({st}) z obietnicą zysku: "
                                        "motyw znany z ostrzeżeń CSIRT KNF / CERT Polska.", "tekst reklamy")
    if _ENDORSE_RE.search(text) and profit:
        add("endorsement_story", "Historia „znanej osoby / ujawnionego sposobu” połączona z obietnicą zysku.",
            "tekst reklamy")
    m = _INCOME_RE.search(text)
    if m:
        add("specific_income_promise", f"Konkretna obietnica dochodu w czasie: „{m.group(0)[:60]}”.", "tekst reklamy")
    if _MESSENGER_RE.search(text):
        add("messenger_contact", "Kontakt przez komunikator (WhatsApp / Telegram / Messenger) zamiast strony.",
            "tekst reklamy")
    for pat in page_name_hits(cr.get("source_page_name"), (rules or {}).get("page_name_patterns") or []):
        add("page_name_pattern", f"Nazwa strony reklamodawcy pasuje do reguły operatora „{pat}”.",
            "reguła operatora (konfiguracja)")
    if len(same_text_pages) >= max(1, int((rules or {}).get("same_text_min_pages") or 3) - 1):
        add("same_text_many_pages", f"Ten sam tekst reklamy na {len(same_text_pages) + 1} różnych stronach "
                                    "reklamodawców (wzorzec, nie dowód wspólnego operatora).", "obserwacje HYDRY")
    return out


def body_key(cr: dict[str, Any]) -> str | None:
    import hashlib

    body = " ".join(" ".join(cr.get("creative_bodies") or []).lower().split())
    return hashlib.sha256(body.encode()).hexdigest() if len(body) >= 30 else None


def same_text_pages(conn: Any, mode: str, cr: dict[str, Any]) -> list[str]:
    """Other page ids (same data space) whose current revision has the same normalised body text."""
    key = body_key(cr)
    if key is None:
        return []
    pages = set()
    for page_id, content in conn.execute("SELECT a.page_id, r.content FROM meta_ads a JOIN meta_revisions r ON "
                                         "r.id=a.current_rev_id WHERE a.mode=? AND a.page_id IS NOT NULL", (mode,)):
        if page_id != cr.get("source_page_id") and body_key(json.loads(content)["creative"]) == key:
            pages.add(page_id)
    return sorted(pages)


def triage(content: dict[str, Any], corr: dict[str, Any], known_hosts: set[str],
           same_text_pages: list[str] | None = None, rules: dict[str, Any] | None = None) -> dict[str, Any]:
    from hydra.enrich import adlibrary as al

    cr, meta = content["creative"], content["meta"]
    ad = al.AdRecord(ad_id=cr["source_ad_id"], source="meta_api", page_id=cr.get("source_page_id"),
                     page_name=cr.get("source_page_name"), bodies=list(cr.get("creative_bodies") or []),
                     link_urls=[u["url"] for u in cr.get("landing_urls") or []],
                     link_captions=list(cr.get("creative_link_captions") or []),
                     link_titles=list(cr.get("creative_link_titles") or []))
    own = rules or {}
    own_kw = [k for k in own.get("keywords") or [] if k not in al.DEFAULT_KEYWORDS]
    own_set = {*own_kw, *(own.get("celebrities") or [])}
    al_rules = al.Rules(known_hosts=known_hosts, max_domain_age_days=None,  # no RDAP here: no network from triage
                        keywords=[*al.DEFAULT_KEYWORDS, *own_kw], celebrities=list(own.get("celebrities") or []))
    reasons: list[dict[str, Any]] = []
    for r in al.evaluate(ad, al_rules):
        kind, _, val = r.partition(":")
        src = "tekst reklamy · reguła operatora" if val in own_set else "tekst reklamy"
        if kind == "keyword":
            prof = any(val.startswith(p) or p.startswith(val) for p in PROFIT)
            reasons.append({"code": "promise_of_profit" if prof else "bait_phrase",
                            "description_pl": ("Obietnica zysku: " if prof else "Słowo-przynęta: ") + f"„{val}”",
                            "artifact": "creative.txt", "source": src, "rule_version": TRIAGE_RULES})
        elif kind == "celebrity":
            reasons.append({"code": "possible_impersonation", "description_pl": f"Możliwe podszycie: wzmianka „{val}”",
                            "artifact": "creative.txt", "source": src, "rule_version": TRIAGE_RULES})
        elif kind in ("known_host", "url_skeleton"):
            reasons.append({"code": "known_case_infrastructure",
                            "description_pl": f"Adres zgodny z infrastrukturą znanej sprawy: {val}",
                            "artifact": "landing_urls.txt", "source": "lista obserwowanych / kampanie",
                            "rule_version": TRIAGE_RULES})
    hosts = {normalize.hosts({"landing_urls": [u]})[0] for u in cr.get("landing_urls") or []
             if normalize.hosts({"landing_urls": [u]})}
    shown = {d["domain"] for d in meta.get("display_domains") or []}
    if hosts and shown and not any(any(h == s or h.endswith("." + s) for s in shown) for h in hosts):
        reasons.append({"code": "caption_landing_mismatch",
                        "description_pl": "Podpis linku wskazuje inną domenę niż adres w treści reklamy.",
                        "artifact": "ad.json", "source": "pola reklamy", "rule_version": TRIAGE_RULES})
    if corr.get("decision") == "assign":
        reasons.append({"code": "linked_to_known_campaign",
                        "description_pl": (f"Przypisanie do kampanii {corr.get('campaign_id')} bazowego przebiegu "
                                           "HYDRY (poziom weryfikacji kampanii: nieznany, dopóki analityk jej nie "
                                           "potwierdzi)."), "artifact": None, "source": "korelacja HYDRA",
                        "rule_version": TRIAGE_RULES})
    reasons += pattern_reasons(content, same_text_pages or [], own)
    codes = [r["code"] for r in reasons]
    strong = any(c in codes for c in ("known_case_infrastructure", "linked_to_known_campaign"))
    bait = any(c in codes for c in ("promise_of_profit", "possible_impersonation"))
    pattern = {c for c in codes if c in PATTERN_CODES}
    if not reasons:
        pr = "unknown"
    elif strong and (bait or pattern):
        pr = "high"
    elif len(pattern) >= 3 and "brand_in_page_name" in pattern and "promise_of_profit" in codes:
        pr = "high"  # independent indicator families: page impersonation + income promise + endorsement / reuse
    elif strong or len(pattern) >= 2 or (bait and len(reasons) >= 3) or (
            "possible_impersonation" in codes and "promise_of_profit" in codes):
        pr = "medium"
    else:
        pr = "low"
    return {"review_status": "unreviewed", "triage_priority": pr, "triage_priority_pl": store.PRIORITY_PL[pr],
            "reasons": reasons, "note_pl": TRIAGE_NOTE_PL, "rule_version": TRIAGE_RULES}


def manipulation(files: list[tuple[str, str, str]], dfk: list[dict[str, Any]]) -> list[dict[str, Any]]:
    imgs = [f for f in files if f[1] == "ad_creative"]
    vids = [f for f in files if f[1] == "ad_video"]
    out = []
    for modality, fs in (("image", imgs), ("video", vids), ("audio", vids)):
        exp = [g for g in dfk if g.get("modality") == modality]
        if not fs:
            out.append({"modality": modality, "status": "not_applicable", "status_pl": "nie dotyczy (brak materiału)",
                        "method": "none", "version": None, "scope": None, "quality_and_coverage": "brak pliku",
                        "evidence_refs": [], "limitations_pl": "Brak pliku tej modalności w pozyskanych materiałach."})
        elif exp:
            out.append({"modality": modality, "status": "inconclusive",
                        "status_pl": "niejednoznaczny (eksperymentalny)",
                        "method": "dfk-experimental", "version": exp[0].get("extractor"),
                        "scope": "pliki reklamy", "quality_and_coverage": f"{len(fs)} plik(i)",
                        "evidence_refs": [f[0] for f in fs],
                        "limitations_pl": "Sygnał eksperymentalny o słabej walidacji; poza oceną oszustwa i korelacji. "
                                          + NO_DETECTOR_PL})
        else:
            out.append({"modality": modality, "status": "unavailable", "status_pl": "niedostępny (brak detektora)",
                        "method": "none", "version": None, "scope": "pliki reklamy" if modality != "audio"
                        else "ścieżka audio wideo", "quality_and_coverage": f"{len(fs)} plik(i), nieanalizowane",
                        "evidence_refs": [f[0] for f in fs], "limitations_pl": NO_DETECTOR_PL})
    return out


def evidence_tasks(a: Any, files: list[tuple[str, str, str]], content: dict[str, Any], cfg: dict[str, Any],
                   mode: str) -> list[dict[str, Any]]:
    """P1 evidence prioritisation for an ambiguous result: which kind of observation is missing and whether an
    available adapter can obtain it (else an analyst task). A fixed heuristic order; no advantage over another
    collection order has been measured."""
    if str(a.decision) == "assign":
        return []
    out = []
    has_media = any(f[1] in ("ad_creative", "ad_video") for f in files)
    has_landing = any(f[1] == "landing_html" for f in files)
    urls = (content.get("creative") or {}).get("landing_urls") or []
    if not has_media:
        from hydra.meta.media import media_enabled

        auto = mode == "live" and media_enabled(cfg)
        out.append({"kind": "ad_media", "adapter": "snapshot_media" if auto else None,
                    "task_pl": "Materiał reklamy (obraz/wideo): " + ("pobierany w kolejnej rewizji przez opcję "
                                                                     "media_fetch." if auto else
                                                                     "dołącz plik ręcznie (Dołącz plik).")})
    if urls and not has_landing:
        auto = mode == "live" and bool(cfg.get("landing_fetch"))
        out.append({"kind": "landing_page", "adapter": "landing_fetch" if auto else None,
                    "task_pl": "Strona docelowa: " + ("pobierana przez opcję landing_fetch." if auto else
                                                      "zapisz stronę ręcznie i dołącz ją jako materiał analizy "
                                                      "(import pliku) albo włącz landing_fetch.")})
    if not urls:
        out.append({"kind": "landing_url", "adapter": None,
                    "task_pl": "Brak potwierdzonego adresu docelowego: sprawdź podgląd reklamy w bibliotece Meta."})
    return out


def coverage_pl(files: list[tuple[str, str, str]], content: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    n_img = sum(1 for f in files if f[1] == "ad_creative")
    n_vid = sum(1 for f in files if f[1] == "ad_video")
    n_land = sum(1 for f in files if f[1] == "landing_html")
    text = bool(normalize.text_of(content["creative"]))
    gaps = [r.get("reason") for r in rows if r.get("status") != "asset_available" and r.get("reason")]
    s = (f"Pokrycie: tekst {'tak' if text else 'brak'}, obrazy {n_img}, wideo {n_vid}, strona docelowa "
         f"{n_land}.")
    if not n_img and not n_vid:
        s += " Analiza obejmuje metadane i tekst, bez materiału wizualnego (nie jest to pełna analiza obrazu/wideo)."
    if gaps:
        s += " Braki: " + " | ".join(dict.fromkeys(gaps))[:600]
    return s


# ------------------------------------------------------------------------------------------------ worker
class MetaWorker:
    """Leases outbox jobs and runs them one at a time (bounded by `max_analyses_per_h`)."""

    def __init__(self, settings: Any, writer: Any, *, only: list[str] | None = None, clock: Any = time.time,
                 transport_factory: Any = None, emit: Any = None) -> None:
        self.settings, self.writer, self.clock = settings, writer, clock
        self.only = only if only is not None else extractors_from_env()
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.transport_factory = transport_factory
        self.emit = emit
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    # ---------------------------------------------------------------- loop
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hydra-meta-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=30)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                did = self.run_once()
            except Exception as exc:  # noqa: BLE001 — the worker must survive any job failure
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("meta worker step failed", exc_info=True)
                did = False
            if not did:
                self._wake.wait(5.0)
                self._wake.clear()

    def run_until_idle(self, max_jobs: int = 100) -> int:
        n = 0
        while n < max_jobs and self.run_once():
            n += 1
        return n

    # ---------------------------------------------------------------- one job
    def run_once(self) -> bool:
        now = self.clock()

        def lease(c: Any) -> dict[str, Any] | None:
            if not store.has_schema(c):
                return None
            return store.claim(c, self.owner, now)

        job = self.writer.run(lease)
        if job is None:
            return False
        try:
            self._run(job)
        except Exception as exc:  # noqa: BLE001 — a failed job is a status, retried by a later analyst request
            msg = f"{type(exc).__name__}: {exc}"[:500]
            log.warning("meta analysis failed for %s", job["ad_id"], exc_info=True)
            self.writer.run(lambda c: store.finish_job(c, job["id"], self.owner, self.clock(), status="failed",
                                                       error=msg))
            self._emit(job, "failed")
        return True

    def _emit(self, job: dict[str, Any], status: str, analysis_id: int | None = None, **extra: Any) -> None:
        if self.emit is not None:
            with contextlib.suppress(Exception):
                self.emit("meta.analysis", {"job_id": job["id"], "ad_id": job["ad_id"], "mode": job["mode"],
                                            "status": status, "analysis_id": analysis_id, **extra})

    def _run(self, job: dict[str, Any]) -> None:
        from hydra.adapters.nask_folder import import_incident, load_incident
        from hydra.pipeline import _write_run, assign_stored, extract, new_run_id
        from hydra.storage import ContentStore

        s = self.settings
        with closing(connect(s.db_path, readonly=True)) as conn:
            cfg = store.config(conn)
            r = conn.execute("SELECT content, content_sha256 FROM meta_revisions WHERE id=?",
                             (job["revision_id"],)).fetchone()
            done_last_h = store.analyses_last_hour(conn, self.clock(), job["mode"])
            # CR-ASTRA-SW-08: run the context the job was keyed to at claim time, not a later re-read
            context = job.get("context") or store.base_context(conn)
            known_hosts = {row[0] for row in conn.execute(
                "SELECT value FROM watch_watchlist WHERE kind IN ('host', 'domain')")} if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='watch_watchlist'").fetchone() else set()
        if r is None:
            raise KeyError(f"revision {job['revision_id']}")
        if cfg["max_analyses_per_h"] and done_last_h >= int(cfg["max_analyses_per_h"]):
            self.writer.run(lambda c: store.finish_job(c, job["id"], self.owner, self.clock(), status="deferred",
                                                       error="Limit analiz na godzinę: odroczono.",
                                                       not_before=self.clock() + 600))
            self._emit(job, "deferred")
            return
        content, sha = json.loads(r[0]), r[1]
        mode = job["mode"]
        transport = None
        from hydra.meta.media import media_enabled

        if mode == "live" and media_enabled(cfg):
            from hydra.meta.transport import MetaTransport

            transport = (self.transport_factory() if self.transport_factory else
                         MetaTransport(s, self.writer, budget_per_h=int(cfg["budget_per_h"]), clock=self.clock))
        acq = __import__("hydra.meta.media", fromlist=["acquire"]).acquire(
            s, self.writer, mode=mode, revision_id=job["revision_id"], content=content, cfg=cfg, now=self.clock(),
            transport=transport)
        files = acq["files"]
        iid = incident_id(mode, job["ad_id"], sha)
        tmp = Path(tempfile.mkdtemp(prefix="meta-inc-", dir=str(s.state_dir)))
        try:
            folder = tmp / iid
            extra = acq.get("snapshot_landing") or []
            built = {**content, "creative": {**content["creative"],
                                             "landing_urls": [*content["creative"].get("landing_urls", []), *extra]}}
            roles = build_folder(folder, built, files, s.cas_dir)
            cas = ContentStore(s.cas_dir)
            import_incident(folder, self.writer, cas, s, incident_id=iid, source=SOURCE_BY_MODE[mode], roles=roles,
                            prefix="metaad")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        with closing(connect(s.db_path, readonly=True)) as conn:
            inc = load_incident(conn, iid)
        assert inc is not None
        extract([inc], self.writer, ContentStore(s.cas_dir), s, only=self.only)
        base_run = None if context == "none" else context
        res = assign_stored(iid, s, base_run=base_run)
        a = res.assignment
        a.meta["base_run"] = base_run
        a.meta["meta_collect"] = {"mode": mode, "ad_id": job["ad_id"], "revision_id": job["revision_id"]}
        run_id = new_run_id("assign")
        corr = correlation_view(a, base_run, run_id, sum(1 for f in files if f[1] in ("ad_creative", "ad_video")))
        if base_run is None:
            corr["reason_pl"] = ("Brak przeanalizowanego korpusu bazowego: decyzja bez kontekstu kampanii. "
                                 + (corr["reason_pl"] or ""))
        with closing(connect(s.db_path, readonly=True)) as conn:
            dfk = [{"modality": "image" if "image" in g[0] else "video" if "video" in g[0] else "audio",
                    "extractor": g[1]} for g in conn.execute(
                "SELECT kind, extractor FROM genes WHERE incident_id=? AND kind LIKE 'dfk%' AND status='observed'",
                (iid,))]
        with closing(connect(s.db_path, readonly=True)) as conn:
            peers = same_text_pages(conn, mode, content["creative"])
        tri = triage(content, corr, known_hosts, peers, cfg.get("rules"))
        man = manipulation(files, dfk)
        cov = coverage_pl(files, content, acq["rows"])
        corr["coverage_pl"] = cov
        corr["evidence_tasks"] = evidence_tasks(a, files, content, cfg, mode)
        has_media = any(f[1] in ("ad_creative", "ad_video") for f in files)
        status = "done" if has_media else "partial"
        reason = cov
        key = job["idem_key"]
        report_id = "MR-" + key[:12]
        params = {"base_run": base_run, "item": f"meta:{mode}:{job['ad_id']}", "source": SOURCE_BY_MODE[mode]}

        def persist(c: Any) -> int:
            if c.execute("SELECT 1 FROM meta_analyses WHERE idem_key=?", (key,)).fetchone() is None:
                _write_run(c, run_id, "assign", params, {"decision": str(a.decision), "conformal": res.conformal},
                           res.pairs, None, [a], conformal={iid: res.conformal} if res.conformal else None)
                rid = run_id
            else:  # re-delivery after a crash between persist and finish: no second run, no second report
                rid = c.execute("SELECT assign_run_id FROM meta_analyses WHERE idem_key=?", (key,)).fetchone()[0]
                corr["assign_run_id"] = rid
            aid = store.save_analysis(
                c, key=key, mode=mode, ad_id=job["ad_id"], revision_id=job["revision_id"], now=self.clock(),
                incident_id=iid, base_run_id=base_run, context_version=context, assign_run_id=rid, status=status,
                reason=reason, decision=str(a.decision), hydra_campaign_id=a.campaign_id, correlation=corr,
                triage=tri, manipulation=man, triage_priority=tri["triage_priority"], report_id=report_id)
            store.finish_job(c, job["id"], self.owner, self.clock(), status=status, analysis_id=aid)
            return aid

        aid = self.writer.run(persist, timeout=600)
        # BONUS-AUTO: the stream event carries the decision, so the top bar can say "nowa reklama powiązana z
        # kampanią X" (a known campaign of the base run) without another request
        self._emit(job, status, aid, decision=str(a.decision), campaign_id=a.campaign_id,
                   triage_priority=tri["triage_priority"], page_name=content["creative"].get("source_page_name"),
                   incident_id=iid)
