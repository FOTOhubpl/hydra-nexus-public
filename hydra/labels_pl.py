"""Polish text helpers for analyst-facing reason strings (`reason_pl`, `explanation_pl`), CR-E2E-7.

Only presentation: scheme ids become Polish labels and numbers get a decimal comma ("27,3 bit"), the way the UI
shows them ("+24,0 b"). Numeric values and decisions are never changed here; machine fields keep the ids and floats.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache

# Short labels for reason sentences. Ids from hydra/genome/lexicons (SCHEMES + pl_scam_schemes.json) and the
# LLM narrative enum (hydra/llm/narrative.py); an unknown id falls back to the lexicon `label_pl`, then to the id.
SCHEME_LABELS_PL: dict[str, str] = {
    "investment": "fałszywe inwestycje",
    "crypto": "kryptowaluty",
    "health_supplement": "cudowne leki i suplementy",
    "gambling": "hazard",
    "fake_news_bait": "fałszywy artykuł jako przynęta",
    "funds_recovery": "odzyskiwanie utraconych środków",
    "giveaway": "nagrody i loterie",
    "phishing": "wyłudzenie danych (phishing)",
    "loan_or_debt": "pożyczki i oddłużanie",
    "loan": "pożyczki i oddłużanie",
    "shopping": "zakupy",
    "other": "inny",
    "marketplace_phishing": "fałszywy odbiór płatności (OLX, Vinted)",
    "parcel_fee": "dopłata do przesyłki",
    "gov_refund": "zwrot podatku lub świadczenia",
    "bank_impersonation": "podszycie pod bank",
    "fake_shop": "fałszywy sklep",
    "account_takeover": "przejęcie konta",
    "fake_charity": "fałszywa zbiórka",
}


@cache
def _lexicon_labels() -> dict[str, str]:
    try:
        import json
        from pathlib import Path

        path = Path(__file__).parent / "genome" / "lexicons" / "pl_scam_schemes.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(s["id"]): str(s["label_pl"]) for s in data.get("schemes", []) if s.get("id") and s.get("label_pl")}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def scheme_pl(scheme: str) -> str:
    """Polish label of a scheme id (`gov_refund` → "zwrot podatku lub świadczenia")."""
    return SCHEME_LABELS_PL.get(scheme) or _lexicon_labels().get(scheme) or scheme.replace("_", " ")


def schemes_pl(schemes: Iterable[str]) -> str:
    """Comma-joined Polish labels, in the sorted order of the ids (stable text for the same set)."""
    return ", ".join(scheme_pl(s) for s in sorted(schemes))


def num_pl(value: float, spec: str = ".1f") -> str:
    """`format(value, spec)` with a decimal comma: num_pl(27.31) == "27,3", num_pl(24, "+.1f") == "+24,0"."""
    return format(value, spec).replace(".", ",")
