"""Grounded Polish campaign briefs and pair explanations (ADR-008, CR-UI-5).

    from hydra import llm
    llm.brief_for_campaign(card, settings=s)   # card = GET /v1/campaigns/{id} body
    llm.explain_pair(pair, settings=s)         # pair = GET /v1/pairs/{a}/{b} body
    → {"status": "ok", "text_pl", "citations": [{"n", "section", "index", "label"}], "model", "generated_at", ...}
    → {"status": "unavailable" | "error", "reason_pl", "facts": [...]}

Grounding: the model gets ONLY stored evidence as a numbered fact list and must cite facts as [n]. Each fact
points at a row of the input body: `core_genome`, `internal_pairs`, `cross_links` (brief) or `reasons`,
`contradictions` (explanation), which the UI links to (`ev-<section>-<index>`). Context facts that have no
evidence row in the UI use the sections `campaign`, `member_incidents`, `infrastructure`, `persons`,
`narrative` (brief) and `incidents`, `score` (explanation); their citation still carries a label.
Beyond the body, the facts include (read-only, from the store) the NAR genes of the members (rule-based `nar@1`
and `llm_narrative@1` inferences, marked as such) and the top bit-waterfall reasons of each internal pair.

`validate()` rejects an answer that cites nothing, cites a fact that does not exist, falls outside the word
range or contains a number that does not occur in the facts; a rejected answer is regenerated with the
validator's feedback (max `MAX_ATTEMPTS`). Nothing the model writes feeds the scorer.

Results also go to `<state_dir>/llm/<run_id>.json` (no schema change), keyed by campaign id / pair.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hydra.config import Settings
from hydra.llm.client import LLMClient, get_client, now_iso
from hydra.redact import redact_text

MAX_ATTEMPTS = 3
MIN_CITED_SENTENCES = 0.5
BRIEF_WORDS = (120, 200)
PAIR_WORDS = (60, 140)
MAX_FACTS = 60
MAX_FACTS_LOCAL = 35  # small local models: fewer, stronger facts (shorter prompt, faster CPU prefill)
INFRA_SUMMARY_KINDS = ("landing_host", "registrable_domain", "registrar", "as_org", "asn", "ns_set", "server_ip",
                       "cdn_provider", "tracker_id", "operator_id")
NAR_RULE_KINDS = ("scheme", "promise", "cta", "institution_brand", "media_brand", "persona", "urgency", "motif")
NAR_LLM_KINDS = ("llm_scheme", "llm_brand", "llm_person", "llm_product", "llm_promise", "llm_promise_amount",
                 "llm_cta", "llm_trick", "llm_audience")
KIND_PL = {
    "landing_host": "host strony docelowej", "registrable_domain": "domena", "registrar": "rejestrator",
    "as_org": "operator sieci (AS)", "asn": "numer AS", "ns_set": "serwery DNS", "server_ip": "adres IP serwera",
    "cdn_provider": "CDN", "tracker_id": "identyfikator trackera", "operator_id": "identyfikator operatora",
    "person_named": "osoba wymieniona z nazwiska", "scheme": "typ schematu", "promise": "obietnica",
    "cta": "wezwanie do działania", "institution_brand": "podszycie pod instytucję",
    "media_brand": "podszycie pod medium", "persona": "persona", "urgency": "presja czasu", "motif": "motyw",
    "llm_scheme": "typ schematu", "llm_brand": "podszywana marka", "llm_person": "wymieniona osoba",
    "llm_product": "produkt", "llm_promise": "obietnica", "llm_promise_amount": "obiecana kwota",
    "llm_cta": "wezwanie do działania", "llm_trick": "technika manipulacji", "llm_audience": "grupa docelowa",
}

SYSTEM_BRIEF = """Jesteś asystentem analityka zespołu CERT. Piszesz krótką notatkę o kampanii oszukańczych \
reklam WYŁĄCZNIE na podstawie ponumerowanej listy faktów z systemu HYDRA. Zasady:
1. Pisz po polsku, rzeczowo, w trzeciej osobie, bez nagłówków i list, {lo}–{hi} słów.
2. Każde zdanie z informacją kończ odwołaniem do faktów w osobnych nawiasach kwadratowych, np. [2] lub [3][7].
3. Nie dodawaj żadnych liczb, dat, kwot, nazw ani wniosków, których nie ma w faktach. Liczby przepisuj \
dokładnie tak, jak w faktach.
4. Fakty oznaczone „(wnioskowanie LLM)” opisuj jako wnioskowanie, nie jako obserwację.
5. Bity to wagi dowodowe, nie prawdopodobieństwa. Nie wskazuj sprawcy ani operatora.
6. Napisz, co łączy incydenty (najmocniejsze wspólne cechy), jaki jest schemat i przekaz, oraz czego brakuje."""

SYSTEM_PAIR = """Jesteś asystentem analityka zespołu CERT. Wyjaśniasz, dlaczego system HYDRA ocenił parę \
incydentów tak, jak ocenił, WYŁĄCZNIE na podstawie ponumerowanej listy faktów. Zasady:
1. Pisz po polsku, rzeczowo, bez nagłówków i list, {lo}–{hi} słów.
2. Każde zdanie z informacją kończ odwołaniem do faktów w osobnych nawiasach kwadratowych, np. [1] lub [2][4].
3. Nie dodawaj liczb, nazw ani wniosków spoza faktów; liczby przepisuj dokładnie.
4. Bity to wagi dowodowe, nie prawdopodobieństwa. Brak cechy to brak informacji, nie dowód przeciw.
5. Wymień najmocniejsze dowody za, dowody przeciw lub sprzeczności, oraz czego brakuje."""

# Local backend (Bielik): shorter, stricter rules; same validator.
SYSTEM_BRIEF_LOCAL = """Piszesz po polsku notatkę analityczną o kampanii oszukańczych reklam. Używaj WYŁĄCZNIE \
podanych faktów. Zasady: {lo}–{hi} słów, jeden lub dwa akapity, bez list i nagłówków. Po każdym zdaniu podaj \
numer faktu w nawiasie kwadratowym, np. [3]. Nie wymyślaj liczb, dat ani nazw: przepisuj je z faktów. Bity to wagi \
dowodowe, nie prawdopodobieństwa. Nie wskazuj sprawcy."""

SYSTEM_PAIR_LOCAL = """Wyjaśniasz po polsku ocenę pary incydentów. Używaj WYŁĄCZNIE podanych faktów. Zasady: \
{lo}–{hi} słów, jeden akapit, bez list. Po każdym zdaniu podaj numer faktu w nawiasie kwadratowym, np. [2]. Nie \
wymyślaj liczb ani nazw. Bity to wagi dowodowe. Brak cechy to brak informacji, nie dowód przeciw."""

_LOCK = threading.Lock()


@dataclass
class Fact:
    text: str
    section: str
    index: int

    @property
    def label(self) -> str:
        return _short(self.text, 140)


# --------------------------------------------------------------------------------------- facts


def _fmt_bits(b: Any) -> str:
    try:
        return f"{float(b):+.1f} bit"
    except (TypeError, ValueError):
        return "? bit"


def _short(v: Any, n: int = 90) -> str:
    s = " ".join(str(v or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _conn(settings: Settings | None) -> sqlite3.Connection | None:
    if settings is None or not Path(settings.db_path).is_file():
        return None
    from hydra.db import connect

    c = connect(settings.db_path, readonly=True)
    c.row_factory = sqlite3.Row
    return c


def _who(incs: list[str], n_members: int) -> str:
    return "wszystkie" if len(incs) == n_members and n_members > 1 else ", ".join(sorted(incs))


def _summary_line(kind: str, ents: list[dict[str, Any]], n_members: int, prefix: str, per_kind: int = 3) -> str:
    parts = [f"„{_short(e['value'], 60)}” ({_who(e['incidents'], n_members)})" for e in ents[:per_kind]]
    return f"{prefix}{KIND_PL.get(kind, kind)}: " + "; ".join(parts)


def _gene_groups(conn: sqlite3.Connection | None, ids: list[str], kinds: tuple[str, ...]) -> dict[str, list[dict]]:
    """kind → [{value, incidents}] most shared first (observed genes of the given incidents)."""
    if conn is None or not ids:
        return {}
    rows = conn.execute(
        f"SELECT incident_id, kind, value FROM genes WHERE status='observed' AND value IS NOT NULL"
        f" AND incident_id IN ({','.join('?' * len(ids))}) AND kind IN ({','.join('?' * len(kinds))})",
        [*ids, *kinds]).fetchall()
    acc: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for r in rows:
        acc[r["kind"]][r["value"]].add(r["incident_id"])
    return {k: [{"value": v, "incidents": sorted(i)} for v, i in sorted(vals.items(), key=lambda kv: (-len(kv[1]),
                                                                                                         kv[0]))]
            for k, vals in acc.items()}


def _narrative_facts(conn: sqlite3.Connection | None, ids: list[str], n: int) -> list[Fact]:
    groups = _gene_groups(conn, ids, NAR_RULE_KINDS + NAR_LLM_KINDS)
    out: list[Fact] = []
    for k in NAR_RULE_KINDS:
        if k in groups:
            out.append(Fact(_summary_line(k, groups[k], n, "Narracja (reguły), "), "narrative", len(out)))
    for k in NAR_LLM_KINDS:
        if k in groups:
            out.append(Fact(_summary_line(k, groups[k], n, "Narracja, ") + " (wnioskowanie LLM)", "narrative",
                            len(out)))
    return out


def _pair_reasons(conn: sqlite3.Connection | None, run_id: str | None, a: str, b: str, top: int = 2) -> str:
    if conn is None or not run_id:
        return ""
    lo, hi = sorted((a, b))
    r = conn.execute("SELECT detail FROM pairs WHERE run_id=? AND a=? AND b=?", (run_id, lo, hi)).fetchone()
    if r is None:
        return ""
    ps = json.loads(r["detail"])
    rs = [_short(x.get("explanation_pl"), 120) for x in (ps.get("reasons") or []) if x.get("kind") != "cap_correction"]
    return ("; najmocniejsze powody: " + " | ".join(rs[:top])) if rs else ""


def campaign_facts(card: dict[str, Any], conn: sqlite3.Connection | None = None,
                   max_facts: int = MAX_FACTS) -> list[Fact]:
    members = list(card.get("members") or [])
    n = len(members)
    run_id = card.get("run_id")
    head = (f"Kampania {card.get('id')} „{_short(card.get('label'), 80)}”: {n} incydenty ({', '.join(members)});"
            f" pewność grupowania {float(card.get('confidence') or 0):.2f} (nieskalibrowana)")
    if card.get("scheme"):
        head += f"; typ schematu wg reguł: {card['scheme']}"
    if card.get("group_id"):
        head += f"; należy do grupy kampanii {card['group_id']}"
    if card.get("first_seen") or card.get("last_seen"):
        head += f"; okres obserwacji {card.get('first_seen') or '?'} – {card.get('last_seen') or '?'}"
    facts = [Fact(head + ".", "campaign", 0)]
    for i, m in enumerate(card.get("member_incidents") or []):
        obs = f", obserwacja {str(m['observed_at'])[:10]}" if m.get("observed_at") else ""
        facts.append(Fact(f"Incydent {m.get('id')}: tytuł „{_short(m.get('title'), 90)}”{obs}.", "member_incidents", i))
    for i, gm in enumerate((card.get("core_genome") or [])[:15]):
        facts.append(Fact(f"Wspólna cecha ({gm.get('family')}): {_short(gm.get('explanation_pl'), 160)}"
                          f" [{_fmt_bits(gm.get('bits'))}]", "core_genome", i))
    for i, e in enumerate((card.get("internal_pairs") or [])[:10]):
        facts.append(Fact(f"Para wewnętrzna {e['a']}–{e['b']}: hipoteza {e.get('hypothesis')}, suma"
                          f" {_fmt_bits(e.get('total_bits'))}, dominująca rodzina {e.get('dominant_family') or '—'}"
                          + _pair_reasons(conn, run_id, e["a"], e["b"]), "internal_pairs", i))
    for i, e in enumerate((card.get("cross_links") or [])[:6]):
        facts.append(Fact(f"Powiązanie zewnętrzne {e['a']}–{e['b']} (druga kampania: {e.get('other_campaign') or '—'}):"
                          f" hipoteza {e.get('hypothesis')}, suma {_fmt_bits(e.get('total_bits'))}", "cross_links", i))
    infra = card.get("infrastructure") or {}
    k_i = 0
    for k in INFRA_SUMMARY_KINDS:
        if infra.get(k):
            facts.append(Fact(_summary_line(k, infra[k], n, "Infrastruktura, "), "infrastructure", k_i))
            k_i += 1
    pers = (card.get("persons") or {}).get("person_named") or []
    if pers:
        facts.append(Fact(_summary_line("person_named", pers, n, "Tożsamość, "), "persons", 0))
    facts += _narrative_facts(conn, members, n)
    return [Fact(redact_text(f.text) or "", f.section, f.index) for f in facts[:max_facts]]


def pair_facts(pair: dict[str, Any], conn: sqlite3.Connection | None = None,
               max_facts: int = MAX_FACTS) -> list[Fact]:
    sc = pair.get("score") or {}
    facts: list[Fact] = []
    sides = [pair.get("incident_a") or {"id": sc.get("a")}, pair.get("incident_b") or {"id": sc.get("b")}]
    for i, side in enumerate(sides):
        camp = side.get("campaign_id") or side.get("campaign")
        facts.append(Fact(f"Incydent {side.get('id')}: tytuł „{_short(side.get('title'), 90)}”"
                          + (f", kampania {camp}" if camp else "") + ".", "incidents", i))
    p = sc.get("probability")
    facts.append(Fact(f"Hipoteza systemu: {sc.get('hypothesis')}; suma {_fmt_bits(sc.get('total_bits'))};"
                      f" p={float(p or 0):.2f} ({'skalibrowane' if sc.get('calibrated') else 'nieskalibrowane'})"
                      + ("; w przebiegu ta sama kampania" if pair.get("same_campaign_in_run") else ""), "score", 0))
    fb = sc.get("family_bits") or {}
    if fb:
        facts.append(Fact("Bity wg rodzin cech: " + ", ".join(f"{k} {float(v):.1f}" for k, v in sorted(fb.items())),
                          "score", 1))
    for i, m in enumerate((sc.get("reasons") or [])[:15]):
        facts.append(Fact(f"Powód ({m.get('family')}): {_short(m.get('explanation_pl'), 180)}"
                          f" [{_fmt_bits(m.get('bits'))}]", "reasons", i))
    for i, m in enumerate(sc.get("contradictions") or []):
        facts.append(Fact(f"Sprzeczność ({m.get('family')}): {_short(m.get('explanation_pl'), 180)}"
                          f" [{_fmt_bits(m.get('bits'))}]", "contradictions", i))
    missing = sc.get("missing_families") or []
    if missing:
        facts.append(Fact("Rodziny cech nieobecne u co najmniej jednego incydentu (brak informacji, nie dowód"
                          " przeciw): " + ", ".join(missing), "score", 2))
    ids = [str(s.get("id")) for s in sides if s.get("id")]
    groups = _gene_groups(conn, ids, ("llm_scheme", "llm_brand", "llm_person", "llm_promise"))
    for k in ("llm_scheme", "llm_brand", "llm_person", "llm_promise"):
        if k in groups:
            facts.append(Fact(_summary_line(k, groups[k], len(ids), "Narracja, ") + " (wnioskowanie LLM)",
                              "narrative", len([f for f in facts if f.section == "narrative"])))
    return [Fact(redact_text(f.text) or "", f.section, f.index) for f in facts[:max_facts]]


# --------------------------------------------------------------------------------------- validation


CITE_RE = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
WORD_RE = re.compile(r"[^\W\d_]+(?:[-'][^\W\d_]+)*|\d+(?:[.,]\d+)?", re.UNICODE)


def _nums(text: str) -> set[str]:
    out = set()
    for m in NUM_RE.findall(text):
        s = m.replace(",", ".")
        out.add(s)
        if "." in s:
            out.add(s.rstrip("0").rstrip("."))
    return out


def citations(text: str) -> list[int]:
    return [int(x) for m in CITE_RE.findall(text) for x in re.split(r"\s*[,;]\s*", m) if x.isdigit()]


def split_citations(text: str) -> str:
    """'[1, 3]' → '[1][3]' (the UI links single [n] markers)."""
    return CITE_RE.sub(lambda m: "".join(f"[{x}]" for x in re.split(r"\s*[,;]\s*", m.group(1))), text)


def validate(text: str, facts: list[str], words: tuple[int, int]) -> list[str]:
    """Polish error messages; empty list = valid."""
    errs = []
    cites = citations(text)
    if not cites:
        errs.append("odpowiedź nie cytuje żadnego faktu [n]")
    bad = sorted({c for c in cites if c < 1 or c > len(facts)})
    if bad:
        errs.append(f"cytuje nieistniejące fakty: {bad}")
    body = CITE_RE.sub(" ", text)
    n_words = len(WORD_RE.findall(body))
    if not (words[0] <= n_words <= words[1]):
        errs.append(f"długość {n_words} słów poza zakresem {words[0]}–{words[1]}")
    # Citations must sit in the sentences they support: at least half of the informative sentences carry one
    # (measured 2026-11-21: Bielik-PL 11B appended "[1], [2], … [35]" after an uncited text to pass the check).
    sents = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if len(WORD_RE.findall(CITE_RE.sub(" ", x))) >= 4]
    if sents:
        cited = sum(bool(CITE_RE.search(x)) for x in sents)
        if cited / len(sents) < MIN_CITED_SENTENCES:
            errs.append(f"za mało zdań z odwołaniem do faktów ({cited}/{len(sents)}); cytuj [n] w każdym zdaniu")
    allowed = _nums(" ".join(facts))
    extra = sorted(n for n in _nums(body) if n not in allowed)
    if extra:
        errs.append(f"liczby spoza faktów: {extra[:8]}")
    return errs


# --------------------------------------------------------------------------------------- generation


SENTENCES_HINT = ("\n\nZwróć JSON: lista \"sentences\"; każde zdanie ma \"text\" (jedno krótkie zdanie po polsku, bez"
                  " numerów faktów) i \"facts\" (numery faktów, na których opiera się zdanie).")


def sentence_schema(n_facts: int, words: tuple[int, int]) -> dict[str, Any]:
    """Grammar for small local models: a bounded list of sentences, each tied to 1–4 existing fact numbers, so
    every sentence carries a citation and the length stays near the word range (≈ 25 words per sentence)."""
    # measured: with ≥ 4 short sentences Bielik 1.5B wrote 86 words; ≥ 6 sentences of ≥ 60 characters reach the range
    lo, hi = max(3, -(-words[0] // 20)), max(4, words[1] // 18)
    return {"type": "object", "additionalProperties": False, "required": ["sentences"],
            "properties": {"sentences": {"type": "array", "minItems": lo, "maxItems": hi, "items": {
                "type": "object", "additionalProperties": False, "required": ["text", "facts"],
                "properties": {"text": {"type": "string", "minLength": 60, "maxLength": 220},
                               "facts": {"type": "array", "minItems": 1, "maxItems": 4,
                                         "items": {"type": "integer", "enum": list(range(1, n_facts + 1))}}}}}}}


def render_sentences(data: Any) -> str:
    """{"sentences": [{"text", "facts"}]} → 'Zdanie [1][3]. Zdanie [2].' (citation before the full stop)."""
    out = []
    for snt in (data or {}).get("sentences") or []:
        t = " ".join(str(snt.get("text") or "").split()).rstrip(" .")
        t = CITE_RE.sub("", t).strip()
        nums = [int(n) for n in snt.get("facts") or [] if isinstance(n, int)]
        if t:
            out.append(f"{t} {''.join(f'[{n}]' for n in dict.fromkeys(nums))}.")
    return " ".join(out)


def _facts_block(facts: list[Fact]) -> str:
    return "\n".join(f"[{i}] {f.text}" for i, f in enumerate(facts, 1))


def _generate(llm: LLMClient, task: str, system: str, facts: list[Fact], instruction: str,
              words: tuple[int, int]) -> dict[str, Any]:
    texts = [f.text for f in facts]
    digest = hashlib.sha256("\n".join(texts).encode()).hexdigest()
    rec: dict[str, Any] = {"status": "unavailable", "facts": [{"n": i, "section": f.section, "index": f.index,
                                                               "text": f.text} for i, f in enumerate(facts, 1)],
                           "facts_sha256": digest, "model": llm.cfg.model, "attempts": [], "generated_at": now_iso()}
    if not facts:
        rec["reason_pl"] = "Brak zapisanych dowodów dla tego obiektu w przebiegu."
        return rec
    feedback = ""
    for attempt in range(MAX_ATTEMPTS):
        prompt = f"Fakty:\n{_facts_block(facts)}\n\n{instruction}{feedback}"
        local = llm.cfg.is_local
        schema = sentence_schema(len(facts), words) if local else None
        if local:
            prompt += SENTENCES_HINT
        res = llm.complete(task=task, model=llm.cfg.model, system=system,
                           content=[{"type": "text", "text": prompt}], inputs_digest=digest, schema=schema,
                           max_tokens=1800 if local else 4000, effort=None if local else "low")
        att = {"n": attempt + 1, **res.summary()}
        if not res.ok:
            rec["reason_pl"] = res.reason_pl
            rec["attempts"].append(att)
            break
        text = render_sentences(res.data) if local else split_citations((res.text or "").strip())
        errs = validate(text, texts, words)
        att["errors"] = errs
        rec["attempts"].append(att)
        rec["model"] = res.model
        if not errs:
            cited = sorted(set(citations(text)))
            rec.update(status="ok", text_pl=text, reason_pl=None, generated_at=res.fetched_at or rec["generated_at"],
                       citations=[{"n": n, "section": facts[n - 1].section, "index": facts[n - 1].index,
                                   "label": facts[n - 1].label} for n in cited])
            break
        rec["rejected_text"] = text
        feedback = ("\n\nPoprzednia wersja została odrzucona przez walidator: " + "; ".join(errs)
                    + ". Napisz ją od nowa, przestrzegając wszystkich zasad.")
        rec["reason_pl"] = "Odpowiedź modelu nie przeszła walidacji: " + "; ".join(errs)
    rec["validation"] = {"ok": rec["status"] == "ok",
                         "errors": rec["attempts"][-1].get("errors", []) if rec["attempts"] else []}
    live = [a for a in rec["attempts"] if not a.get("cached")]
    rec["usage"] = {"input_tokens": sum(int(a["usage"].get("input_tokens", 0)) for a in live),
                    "output_tokens": sum(int(a["usage"].get("output_tokens", 0)) for a in live),
                    "latency_s": round(sum(float(a.get("latency_s") or 0) for a in live), 3),
                    "cost_usd_estimated": round(sum(float(a.get("cost_usd") or 0) for a in live), 6)}
    rec["cached"] = bool(rec["attempts"]) and not live
    return rec


# --------------------------------------------------------------------------------------- store


def store_path(settings: Settings, run_id: str) -> Path:
    assert settings.state_dir is not None
    return Path(settings.state_dir) / "llm" / f"{re.sub(r'[^A-Za-z0-9._-]', '_', run_id)}.json"


def stored(run_id: str, settings: Settings | None = None) -> dict[str, Any]:
    try:
        return json.loads(store_path(settings or Settings(), run_id).read_text())
    except (OSError, ValueError):
        return {"run_id": run_id, "briefs": {}, "pairs": {}}


def _save(settings: Settings, run_id: str | None, section: str, key: str, rec: dict[str, Any]) -> None:
    if not run_id:
        return
    with _LOCK:
        data = stored(run_id, settings)
        if rec["status"] != "ok" and (data.get(section, {}).get(key) or {}).get("status") == "ok":
            return  # keep the last good text; an outage does not erase it
        data.setdefault(section, {})[key] = rec
        p = store_path(settings, run_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(p)


def _section(name: str, llm: LLMClient) -> str:
    """Bedrock results keep the plain section names; each local model gets its own (`briefs@local-<model>`)."""
    return name if not llm.cfg.is_local else f"{name}@{llm.namespace(llm.cfg.model)}"


def brief_for_campaign(card: dict[str, Any], *, settings: Settings | None = None,
                       client: LLMClient | None = None) -> dict[str, Any]:
    """CR-UI-5: card = GET /v1/campaigns/{id} body."""
    s = settings or Settings()
    llm = client or get_client(s)
    conn = _conn(s)
    try:
        facts = campaign_facts(card, conn, MAX_FACTS_LOCAL if llm.cfg.is_local else MAX_FACTS)
    finally:
        if conn is not None:
            conn.close()
    lo, hi = BRIEF_WORDS
    system = (SYSTEM_BRIEF_LOCAL if llm.cfg.is_local else SYSTEM_BRIEF).format(lo=lo, hi=hi)
    rec = _generate(llm, "brief", system, facts,
                    f"Napisz notatkę o kampanii {card.get('id')} ({lo}–{hi} słów, cytuj fakty [n]).", BRIEF_WORDS)
    rec.update(run_id=card.get("run_id"), campaign_id=card.get("id"), backend=llm.cfg.backend)
    _save(s, card.get("run_id"), _section("briefs", llm), str(card.get("id")), rec)
    return rec


def explain_pair(pair: dict[str, Any], *, settings: Settings | None = None,
                 client: LLMClient | None = None) -> dict[str, Any]:
    """CR-UI-5: pair = GET /v1/pairs/{a}/{b} body."""
    s = settings or Settings()
    llm = client or get_client(s)
    sc = pair.get("score") or {}
    a, b = sorted((str(sc.get("a")), str(sc.get("b"))))
    conn = _conn(s)
    try:
        facts = pair_facts(pair, conn, MAX_FACTS_LOCAL if llm.cfg.is_local else MAX_FACTS)
    finally:
        if conn is not None:
            conn.close()
    lo, hi = PAIR_WORDS
    system = (SYSTEM_PAIR_LOCAL if llm.cfg.is_local else SYSTEM_PAIR).format(lo=lo, hi=hi)
    rec = _generate(llm, "pair", system, facts,
                    f"Wyjaśnij ocenę pary {a} i {b} ({lo}–{hi} słów, cytuj fakty [n]).", PAIR_WORDS)
    rec.update(run_id=pair.get("run_id"), a=a, b=b, backend=llm.cfg.backend)
    _save(s, pair.get("run_id"), _section("pairs", llm), f"{a}|{b}", rec)
    return rec


# --------------------------------------------------------------------------------------- store-side helpers


class _Shim:
    """Minimal stand-in for a FastAPI Request so the CLI builds exactly the API's card/pair bodies."""

    def __init__(self, settings: Settings):
        from types import SimpleNamespace

        from hydra.db import connect

        def reader():
            c = connect(settings.db_path, readonly=True)
            c.row_factory = sqlite3.Row
            return closing(c)

        self.app = SimpleNamespace(state=SimpleNamespace(hydra=SimpleNamespace(settings=settings, reader=reader)))


def campaign_card(settings: Settings, run_id: str, campaign_id: str) -> dict[str, Any]:
    from hydra.api_task import _campaign_card

    with _Shim(settings).app.state.hydra.reader() as conn:
        return _campaign_card(conn, run_id, campaign_id)


def pair_body(settings: Settings, run_id: str, a: str, b: str) -> dict[str, Any]:
    from hydra.api_task import get_pair

    return get_pair(_Shim(settings), a, b, run_id)  # type: ignore[arg-type]


__all__ = ["brief_for_campaign", "campaign_card", "campaign_facts", "citations", "explain_pair", "pair_body",
           "pair_facts", "stored", "validate"]
