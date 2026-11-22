"""The finite list of allowed evidence actions (INNOWACJE §5). Every action reads an EXISTING artefact or cache;
none of them opens a network connection or scans anything.

| id | reads | cost |
|---|---|---|
| `reread_har` | the incident's stored HAR (network log) | low |
| `read_dom_fragment` | a DOM fragment / script of the saved page | low |
| `ocr_region` | OCR of one region of a stored image | medium |
| `same_asset_geometry` | local geometric check (same_asset) of one image pair | medium |
| `cached_metadata` | an approved metadata source, from the local enrichment cache only | low |
| `ask_analyst_pair` | the analyst decides one pair (creates a question; never auto-resolves) | high |
"""

from __future__ import annotations

from dataclasses import dataclass

from hydra.hunt.records import Observation


@dataclass(frozen=True)
class Action:
    id: str
    source: str
    cost: str
    label_pl: str


ACTIONS: tuple[Action, ...] = (
    Action("reread_har", "har", "low", "Ponowny odczyt zapisanego HAR"),
    Action("read_dom_fragment", "dom", "low", "Odczyt fragmentu DOM lub skryptu"),
    Action("ocr_region", "ocr", "medium", "OCR wskazanego regionu obrazu"),
    Action("same_asset_geometry", "image", "medium", "Lokalna weryfikacja geometrii pary (same_asset)"),
    Action("cached_metadata", "metadata", "low", "Zatwierdzone źródło metadanych z lokalnego cache"),
    Action("ask_analyst_pair", "analyst", "high", "Pytanie do analityka o jedną parę"),
)
BY_ID = {a.id: a for a in ACTIONS}
COST = {"low": 1, "medium": 2, "high": 3}


def unread_sources(obs: Observation) -> set[str]:
    """Artefact kinds that EXIST for the candidate but were not read into the decision yet. Only the existence of
    the artefact is used for planning, never the content it would reveal."""
    return set(obs.meta.get("unread_sources") or {t.source for t in obs.hidden})


def execute(action: Action, obs: Observation) -> tuple[Observation, list[str]]:
    """Read the action's source into the observation. Returns the new observation and the revealed trait keys."""
    if action.id == "ask_analyst_pair":
        return obs, []  # a question is recorded by the caller; the analyst's answer is a Review, not a gene
    revealed = [t for t in obs.hidden if t.source == action.source]
    rest = [t for t in obs.hidden if t.source != action.source]
    unread = sorted(unread_sources(obs) - {action.source})
    new = obs.model_copy(update={"traits": [*obs.traits, *revealed], "hidden": rest,
                                 "meta": {**obs.meta, "unread_sources": unread,
                                          "read": [*obs.meta.get("read", []), action.source]}})
    return new, [t.key for t in revealed]
