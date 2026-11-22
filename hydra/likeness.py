""""Nadużycie wizerunku" (likeness abuse) analyst triage card: a rule-based aggregator over stored genes.

It is NOT a link rule (it never feeds hydra.synapse) and NOT deepfake detection. Per incident it lists five
signal blocks, each present / absent / unknown, with the evidence pointers of the genes behind it:

1. public_figure      named public figure: TXT `person_named` from the gazetteer (lexicons/persons.py), ID
                      `person_named` / `persona_match` / `persona_renamed` (face gallery limited to names in the
                      material); the capitalised-name heuristic alone is not a public figure;
2. financial_promise  NAR `promise` with a financial term id, or NAR `scheme` = investment;
3. media_brand        NAR `media_brand` / `institution_brand`, VIS `logo` (impersonated media or institution);
4. reused_media       the same image / footage in another incident: VIS reasons of stored pair scores, or an
                      identical VIS hash value (artifact_sha256, pdq, phash) held by another incident;
5. ai_generator       file provenance from hydra.genome.prov / dfk with separated meanings (CR-ASTRA-RES-1):
                      counted premises are only a C2PA AI declaration whose binding validated and a public SD/SDXL
                      generator watermark; AudioSeal (tool mark), a C2PA declaration with validation errors, unsigned
                      generator metadata and StyleGAN eye geometry are shown as not-counted checks. The block is
                      never "absent": nothing found = "nieznane", never evidence of authenticity.
6. manipulation_suspicion  weak "sygnał podejrzenia manipulacji" heuristics: AV-sync (vid_avsync), image
                      representation instability and video trajectory curvature (hydra.genome.dfk). Shown for
                      triage only and NOT counted in the strength (docs/genes/DFK.md section 4: weak or near chance).

Unknown = the extractor did not run or could not check (no genes, or only unavailable / failed genes); it is never
counted against the incident. Strength = number of present blocks among the first five (a count, not a
probability); the sixth block never raises it. The card is
"triggered" only when a public figure AND a financial promise are both present (research 04 §4).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

FINANCIAL_PROMISES = {"money_multiplier", "passive_income", "fast_profit", "guaranteed", "free_state_money"}
FINANCIAL_SCHEMES = {"investment"}
PERSON_KINDS_ID = {"person_named", "persona_match", "persona_renamed"}
VIS_REUSE_KINDS = {"visual_near_duplicate", "artifact_sha256", "pdq", "pdq_canon", "phash", "phash_tiles",
                   "phash_tile", "dinov2", "siglip2", "vis_unit"}
VIS_HASH_KINDS = ("artifact_sha256", "pdq", "pdq_canon", "phash")
MAX_ITEMS = 12

TITLE_PL = "Nadużycie wizerunku — karta triażu analityka"
DISCLAIMER_PL = (
    "Karta zbiera sygnały do ręcznej weryfikacji. Nie jest wykrywaniem deepfake'ów ani regułą łączenia kampanii "
    "i nie zmienia wyniku powiązań. Wymienienie lub wizerunek osoby nie oznacza, że brała ona udział w reklamie "
    "ani że ją popiera. Brak znaku wodnego lub manifestu nie świadczy o autentyczności materiału.")
LABELS = {
    "public_figure": "Osoba publiczna wymieniona lub rozpoznana",
    "financial_promise": "Obietnica finansowa",
    "media_brand": "Podszycie pod markę medialną lub instytucję",
    "reused_media": "Ten sam obraz lub nagranie w innych incydentach",
    "ai_generator": "Sygnał generatora AI w pliku (zweryfikowana deklaracja lub znak generatora)",
    "manipulation_suspicion": "Sygnały podejrzenia manipulacji (słabe, nie wliczane do siły)",
}
COUNTED = ("public_figure", "financial_promise", "media_brand", "reused_media", "ai_generator")
HINTS = {
    "public_figure": "Porównaj z oficjalnymi kanałami osoby; zgodność nazwiska lub twarzy to nie dowód udziału.",
    "financial_promise": "Sprawdź fragment tekstu z obietnicą w materiale źródłowym.",
    "media_brand": "Porównaj z oryginalną stroną marki (domena, szata graficzna).",
    "reused_media": "Porównaj z oryginalnym nagraniem lub zdjęciem: materiał może być użyty poza kontekstem.",
    "ai_generator": "Sygnały pochodzenia znikają po zrzucie ekranu i rekompresji; ich brak niczego nie przesądza.",
    "manipulation_suspicion": "Heurystyki bez kalibracji na reklamach: lektor, dubbing, rekompresja i zrzut ekranu "
                              "dają ten sam sygnał. To nie jest wykrywanie deepfake'ów; porównaj z oryginalnym "
                              "nagraniem.",
}
LIMITS_PL = [
    "Platformy społecznościowe i zrzuty ekranu usuwają manifesty C2PA, metadane i znaki wodne: zwykle "
    "wynik to „nie wykryto”.",
    "Znak wodny SD/SDXL (DWT-DCT) nie przetrwa rekompresji JPEG ani zmiany rozmiaru (pomiar na obrazach "
    "syntetycznych: 0/20 po JPEG q95).",
    "Geometria oczu wskazuje tylko wyrównane portrety typu StyleGAN; nie dotyczy obrazów z modeli dyfuzyjnych.",
    "Osoby publiczne pochodzą z nazw w materiale i z krótkiej listy nazwisk; twarze nie są identyfikowane "
    "poza galerią osób wymienionych w materiale.",
    "Sygnały podejrzenia manipulacji są słabe: stabilność obrazu była w pomiarze bliska losowej, a krzywizna wideo "
    "na jednym zbiorze działała, na drugim odwrotnie, więc obie są domyślnie wyłączone; synchronizacji ust nie "
    "kalibrowano. Pokazujemy je do triażu i nie wliczamy do siły wskazania.",
    "Znak wodny AudioSeal osadzają tylko niektóre narzędzia; większość usług klonowania głosu go nie stosuje, a znak "
    "można osadzić także w prawdziwym nagraniu; czyste tony (np. sygnał testowy) potrafią fałszywie uruchomić "
    "detektor. Wykrycie znaku nie jest wliczane do siły wskazania.",
    "Deklaracja AI w manifeście C2PA liczy się tylko przy poprawnej integralności; integralność nie oznacza zaufania "
    "do podpisującego. Niepodpisane metadane i geometria oczu to słabe wskazówki, niewliczane do siły.",
]


def _item(g: dict[str, Any], label: str | None = None, other: str | None = None) -> dict[str, Any]:
    ev = g.get("evidence") or {}
    return {"family": g.get("family"), "kind": g.get("kind"), "value": g.get("value"), "quality": g.get("quality"),
            "provenance": g.get("provenance"), "label_pl": label, "other_incident": other,
            "evidence": {k: ev.get(k) for k in ("artifact_id", "bbox", "char_range", "timecode_s", "dom_path",
                                                "note") if ev.get(k) is not None}}


def _signal(key: str, state: str, summary: str, items: list[dict[str, Any]],
            checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"key": key, "label_pl": LABELS[key], "state": state, "summary_pl": summary, "hint_pl": HINTS[key],
            "items": items[:MAX_ITEMS], "checks": checks or []}


def _observed(genes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [g for g in genes if g.get("status") == "observed"]


def _ran(genes: list[dict[str, Any]], family: str) -> bool:
    """The family produced at least one observed or not_applicable gene: a check happened."""
    return any(g.get("family") == family and g.get("status") in ("observed", "not_applicable") for g in genes)


def public_figure(genes: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for g in _observed(genes):
        note = str((g.get("evidence") or {}).get("note") or "")
        if g["family"] == "TXT" and g["kind"] == "person_named" and "gazetteer:" in note:
            items.append(_item(g, f"{g['value']} (nazwisko w tekście)"))
        elif g["family"] == "ID" and g["kind"] in PERSON_KINDS_ID:
            how = "podpis obrazu" if g.get("provenance") == "OBS" else "dopasowanie twarzy do galerii"
            items.append(_item(g, f"{g['value']} ({how})"))
    if items:
        names = sorted({str(i["value"]) for i in items})
        return _signal("public_figure", "present", f"Wymienione osoby: {', '.join(names)[:200]}", items)
    if _ran(genes, "TXT") or _ran(genes, "ID"):
        return _signal("public_figure", "absent", "Nie znaleziono nazwiska ani twarzy osoby z listy.", [])
    return _signal("public_figure", "unknown", "Brak tekstu i twarzy do sprawdzenia.", [])


def financial_promise(genes: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for g in _observed(genes):
        if g["family"] != "NAR":
            continue
        if g["kind"] == "promise" and g.get("value") in FINANCIAL_PROMISES:
            items.append(_item(g, f"obietnica: {g['value']}"))
        elif g["kind"] == "scheme" and g.get("value") in FINANCIAL_SCHEMES:
            items.append(_item(g, "schemat: inwestycja"))
    if items:
        return _signal("financial_promise", "present", f"{len(items)} sygnałów obietnicy finansowej.", items)
    if _ran(genes, "NAR"):
        return _signal("financial_promise", "absent", "Reguły narracji nie znalazły obietnicy finansowej.", [])
    return _signal("financial_promise", "unknown", "Brak tekstu do analizy narracji.", [])


def media_brand(genes: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for g in _observed(genes):
        if g["family"] == "NAR" and g["kind"] in ("media_brand", "institution_brand"):
            items.append(_item(g, f"{'media' if g['kind'] == 'media_brand' else 'instytucja'}: {g['value']}"))
        elif g["family"] == "VIS" and g["kind"] == "logo" and g.get("value"):
            items.append(_item(g, f"logo: {g['value']}"))
    if items:
        brands = sorted({str(i["value"]) for i in items})
        return _signal("media_brand", "present", f"Marki: {', '.join(brands)[:200]}", items)
    if _ran(genes, "NAR") or _ran(genes, "VIS"):
        return _signal("media_brand", "absent", "Nie znaleziono nazwy ani logo marki z listy.", [])
    return _signal("media_brand", "unknown", "Brak tekstu i obrazów do sprawdzenia.", [])


def reused_media(genes: list[dict[str, Any]], reuse: list[dict[str, Any]]) -> dict[str, Any]:
    """`reuse`: [{"other": incident_id, "kind": ..., "value": ..., "bits"?: float, "evidence": {...}}]."""
    items = [_item({"family": "VIS", "kind": r.get("kind"), "value": r.get("value"), "quality": r.get("quality"),
                    "provenance": "INF", "evidence": r.get("evidence") or {}},
                   r.get("label_pl") or f"wspólny element wizualny z {r['other']}", r["other"]) for r in reuse]
    if items:
        others = sorted({i["other_incident"] for i in items})
        return _signal("reused_media", "present", f"Ten sam materiał wizualny w {len(others)} innych incydentach: "
                       f"{', '.join(others)[:200]}", items)
    if any(g.get("family") == "VIS" and g.get("status") == "observed" for g in genes):
        return _signal("reused_media", "absent", "Obrazy tego incydentu nie powtarzają się w korpusie.", [])
    return _signal("reused_media", "unknown", "Brak obrazów lub nagrań do porównania.", [])


def _check(key: str, label: str, genes: list[dict[str, Any]], summary_kind: str, present_kinds: set[str],
           present_values: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    hits = [g for g in _observed(genes) if g["kind"] in present_kinds]
    summ = [g for g in genes if g["kind"] == summary_kind]
    obs = [g for g in summ if g.get("status") == "observed"]
    note = str(((summ[0].get("evidence") or {}).get("note")) or "") if summ else ""
    if hits or any(g.get("value") in present_values for g in obs):
        state = "present"
    elif obs:
        state = "absent"
    else:
        state = "unknown"
    summary = note or {"present": "wykryto", "absent": "nie wykryto",
                       "unknown": "nie dało się sprawdzić (moduł nie działał na tym incydencie)"}[state]
    return {"key": key, "label_pl": label, "state": state, "summary_pl": summary[:300]}, hits


VALID_BINDING = {"valid", "trusted"}
AI_LABEL_C2PA = "Manifest C2PA z deklaracją AI (powiązanie z plikiem zweryfikowane)"


def _c2pa_bound(genes: list[dict[str, Any]], g: dict[str, Any]) -> bool:
    """A C2PA AI declaration counts only when its manifest binding to the file validated.

    Uses `c2pa_binding` (prov extractor) and falls back to the raw `c2pa_validation` of the same artifact for genes
    stored by older runs; an error state, an unknown state or a missing validation = not bound."""
    aid = (g.get("evidence") or {}).get("artifact_id")
    for kind in ("c2pa_binding", "c2pa_validation"):
        vals = [str(x.get("value") or "").lower() for x in genes if x.get("kind") == kind
                and (x.get("evidence") or {}).get("artifact_id") == aid]
        if vals:
            return all(v in VALID_BINDING for v in vals)
    return False


def _ai_check(key: str, label: str, state: str, summary: str, role: str) -> dict[str, Any]:
    """role: evidence (counted: validated declaration or generator watermark) | tool_mark (a tool's mark, not the
    content kind; not counted) | hint (mutable metadata or weak morphology; not counted)."""
    return {"key": key, "label_pl": label, "state": state, "summary_pl": summary[:300], "role": role,
            "counted": role == "evidence",
            "state_label_pl": {"present": "wykryto", "absent": "nie wykryto", "unknown": "nieznane"}[state]}


def ai_generator(genes: list[dict[str, Any]]) -> dict[str, Any]:
    """Separated meanings (CR-ASTRA-RES-1). Only two observations are positive premises of the counted block: a C2PA
    AI declaration whose binding to the file validated, and a public SD/SDXL generator watermark. AudioSeal is a tool
    mark (it can be embedded in real speech); a declaration with validation errors, unsigned metadata and StyleGAN eye
    geometry are weak hints. Those are shown, never counted. Nothing found = "nieznane", never "autentyczne"."""
    obs = _observed(genes)
    c2pa_summ = [g for g in genes if g["kind"] == "c2pa_manifest"]
    declared = [g for g in obs if g["kind"] == "c2pa_ai_generated"]
    bound = [g for g in declared if _c2pa_bound(genes, g)]
    unverified = ([g for g in declared if g not in bound]
                  + [g for g in obs if g["kind"] == "c2pa_ai_declared_unverified"])
    has_manifest = any(g.get("value") == "present" for g in c2pa_summ if g.get("status") == "observed")
    if bound:
        trust = {str(g.get("value")) for g in obs if g["kind"] == "c2pa_trust"}
        tr = ("; podpisujący zaufany" if trust == {"trusted"} else
              "; certyfikat spoza listy zaufanych (integralność to nie zaufanie)" if "untrusted" in trust else "")
        vals = ", ".join(sorted({str(g["value"]) for g in bound}))
        c2pa = _ai_check("c2pa", AI_LABEL_C2PA, "present", f"deklaracja {vals}; integralność poprawna{tr}", "evidence")
    elif any(g.get("status") == "observed" for g in c2pa_summ) or declared:
        c2pa = _ai_check("c2pa", AI_LABEL_C2PA, "absent", "nie wykryto zweryfikowanej deklaracji AI" + (
            " (manifest jest, ale bez ważnej deklaracji treści generowanej)" if has_manifest or declared else "")
            + "; brak nie świadczy o autentyczności", "evidence")
    else:
        c2pa = _ai_check("c2pa", AI_LABEL_C2PA, "unknown", "nie dało się sprawdzić", "evidence")
    wm_hits = [g for g in obs if g["kind"] == "wm_generator"]
    wm_obs = [g for g in obs if g["kind"] == "wm_check"]
    wm_state = ("present" if wm_hits or any(g.get("value") == "detected" for g in wm_obs) else
                "absent" if wm_obs else "unknown")
    wm = _ai_check("watermark", "Znak wodny generatora (SD v1 / SDXL)", wm_state,
                   {"present": "znak wodny generatora wykryty", "absent": "znak wodny generatora nie wykryty; brak "
                    "nie świadczy o autentyczności", "unknown": "nie dało się sprawdzić"}[wm_state], "evidence")
    checks = [c2pa, wm]
    if unverified:
        checks.append(_ai_check(
            "c2pa_unverified", "Deklaracja AI w manifeście C2PA bez potwierdzonej integralności", "present",
            "manifest deklaruje treść generowaną, ale walidacja wykazała błędy lub jej nie potwierdzono; deklaracja "
            "nie podnosi pewności, że plik jest generowany (diagnostyka w dowodach)", "hint"))
    aw_hits = [g for g in obs if g["kind"] == "wm_audio"]
    aw_local = [g for g in obs if g["kind"] == "wm_audio_local"]
    aw_obs = [g for g in obs if g["kind"] == "wm_audio_check"]
    aw_state = ("present" if aw_hits or aw_local or any(g.get("value") == "detected" for g in aw_obs) else
                "absent" if aw_obs else "unknown")
    scope = ("znak mówi, że narzędzie osadzające AudioSeal przetworzyło dźwięk; można go osadzić także w nagraniu "
             "prawdziwego głosu, więc nie rozstrzyga, czy treść jest generowana")
    aw_sum = {"present": ("wykryto znak AudioSeal" if aw_hits or not aw_local else
                          "wykryto znak AudioSeal we fragmentach nagrania") + f" ({scope})",
              "absent": "nie wykryto znaku AudioSeal; pochodzenie nieznane (brak nie świadczy o autentyczności)",
              "unknown": "nie dało się sprawdzić"}[aw_state]
    checks.append(_ai_check("audio_watermark", "Znak wodny AudioSeal w ścieżce dźwiękowej", aw_state, aw_sum,
                            "tool_mark"))
    md_hits = [g for g in obs if g["kind"] == "ai_metadata_marker"]
    if md_hits:
        checks.append(_ai_check("metadata", "Niepodpisane metadane wskazujące generator (słaba wskazówka)", "present",
                                ", ".join(sorted({str(g['value']) for g in md_hits})) + "; metadane bez podpisu "
                                "można dopisać lub usunąć", "hint"))
    av_hits = [g for g in obs if g["kind"] == "synth_avatar_eyes"]
    av_obs = [g for g in obs if g["kind"] == "avatar_geometry_check"]
    av_state = ("present" if av_hits or any(g.get("value") == "flagged" for g in av_obs) else
                "absent" if av_obs else "unknown")
    checks.append(_ai_check("avatar_geometry", "Geometria oczu typowa dla StyleGAN (słaba wskazówka)", av_state,
                            {"present": "oczy w pozycjach wyrównanych portretów FFHQ/StyleGAN; zwykłe kadrowane "
                             "portrety mogą dać to samo", "absent": "nie wykryto",
                             "unknown": "nie dało się sprawdzić"}[av_state], "hint"))
    items = ([_item(g, f"C2PA (integralność poprawna): {g['value']}") for g in bound]
             + [_item(g, f"znak wodny: {g['value']}") for g in wm_hits]
             + [_item(g, f"C2PA bez potwierdzonej integralności: {g['value']}") for g in unverified]
             + [_item(g, "znak AudioSeal (narzędzie, nie rodzaj treści)") for g in aw_hits + aw_local]
             + [_item(g, "niepodpisane metadane: " + str(g["value"])) for g in md_hits]
             + [_item(g, "geometria oczu StyleGAN (słaba wskazówka)") for g in av_hits])
    strong = [c["label_pl"] for c in checks if c["counted"] and c["state"] == "present"]
    weak = [c["label_pl"] for c in checks if not c["counted"] and c["state"] == "present"]
    ran = any(c["state"] != "unknown" for c in checks)
    if strong:
        sig = _signal("ai_generator", "present", "Wykryto: " + "; ".join(strong), items, checks)
    elif weak:
        sig = _signal("ai_generator", "unknown", "Pochodzenie nieznane: brak zweryfikowanej deklaracji i znaku "
                      "generatora; tylko obserwacje niewliczane do siły: " + "; ".join(weak), items, checks)
    elif ran:
        sig = _signal("ai_generator", "unknown", "Pochodzenie nieznane: nie wykryto sygnału generatora (brak nie "
                      "świadczy o autentyczności).", items, checks)
    else:
        sig = _signal("ai_generator", "unknown", "Pochodzenia plików nie dało się sprawdzić.", items, checks)
    sig["checked"] = ran
    sig["state_label_pl"] = "wykryto" if strong else "nieznane" if ran else None
    return sig


SUSPICION_CHECKS = (
    ("av_sync", "Niespójność ruchu ust i dźwięku", "av_sync_suspicion", None),
    ("synthetic_image", "Niestabilna reprezentacja obrazu (typowa dla generatorów)", "synthetic_image_suspicion",
     "synthetic_image_check"),
    ("synthetic_video", "Krzywizna trajektorii wideo (typowa dla generatorów)", "synthetic_video_suspicion",
     "synthetic_video_check"),
)


def manipulation_suspicion(genes: list[dict[str, Any]]) -> dict[str, Any]:
    """Weak heuristics; per check present = some file at level "high", absent = measured lower, else unknown."""
    checks, items = [], []
    for key, label, kind, summary_kind in SUSPICION_CHECKS:
        obs = [g for g in _observed(genes) if g["kind"] == kind]
        hits = [g for g in obs if g.get("value") == "high"]
        state = "present" if hits else "absent" if obs else "unknown"
        if hits:
            summary = str((hits[0].get("evidence") or {}).get("note") or "podwyższony")
        elif obs:
            summary = f"zmierzono {len(obs)} plików, bez podwyższonego poziomu"
        else:
            notes = [str((g.get("evidence") or {}).get("note") or "") for g in genes if g["kind"] == summary_kind]
            summary = next((n for n in notes if n), "nie dało się zmierzyć (brak nagrania, twarzy lub modelu)")
        checks.append({"key": key, "label_pl": label, "state": state, "summary_pl": summary[:300]})
        items += [_item(g, f"{label}: poziom podwyższony") for g in hits]
    states = {c["state"] for c in checks}
    if "present" in states:
        names = [c["label_pl"] for c in checks if c["state"] == "present"]
        return _signal("manipulation_suspicion", "present", "Sygnał podejrzenia manipulacji: " + "; ".join(names),
                       items, checks)
    if "absent" in states:
        return _signal("manipulation_suspicion", "absent", "Heurystyki nie dały podwyższonego sygnału (to nie "
                       "świadczy o autentyczności).", items, checks)
    return _signal("manipulation_suspicion", "unknown", "Heurystyk nie dało się zastosować.", items, checks)


def strength_of(present: int) -> str:
    return "brak" if present == 0 else "słabe" if present <= 2 else "umiarkowane" if present == 3 else "silne"


def build_card(incident_id: str, genes: list[dict[str, Any]], reuse: list[dict[str, Any]],
               run_id: str | None = None) -> dict[str, Any]:
    signals = [public_figure(genes), financial_promise(genes), media_brand(genes), reused_media(genes, reuse),
               ai_generator(genes), manipulation_suspicion(genes)]
    for s in signals:
        s["counted"] = s["key"] in COUNTED
    present = sum(s["state"] == "present" for s in signals if s["counted"])
    by = {s["key"]: s["state"] for s in signals}
    limits = list(LIMITS_PL)
    unknown = [s["label_pl"] for s in signals if s["state"] == "unknown" and s.get("checked") is not True]
    if unknown:
        limits.insert(0, "Nie dało się sprawdzić: " + "; ".join(unknown) + ".")
    return {"incident_id": incident_id, "run_id": run_id, "title_pl": TITLE_PL, "disclaimer_pl": DISCLAIMER_PL,
            "triggered": by["public_figure"] == "present" and by["financial_promise"] == "present",
            "present": present, "total": len(COUNTED), "strength": strength_of(present), "signals": signals,
            "limits_pl": limits}
