"""Meta Ad Library contract (docs/integrations/META_API_CONTRACT.md).

Verified against the public reference pages on 2026-11-22 (Europe/Warsaw):
- S1 https://developers.facebook.com/docs/graph-api/reference/ads_archive/ (version selector v21.0-v27.0)
- S2 https://developers.facebook.com/docs/graph-api/reference/archived-ad/ (page rendered as v27.0)
- S3 https://developers.facebook.com/docs/graph-api/overview/rate-limiting/ (v27.0)

`ad_creative_link_urls` is NOT in the S2 field list and is never requested: a landing URL is taken from the text or
from an analyst-attached / opt-in snapshot only, with its provenance. The pinned version is a documentation fact, not
an observation of our token (the live diagnostics have not been run when this module was written).
"""

from __future__ import annotations

import os

GRAPH_VERSION_DEFAULT = "v26.0"
VERIFIED_ON = "2026-11-22"
GRAPH_HOST = "https://graph.facebook.com"

# Minimal importer contract (diagnostics layer 3): the collector depends on every one of these.
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "page_id",
    "page_name",
    "ad_creation_time",
    "ad_creative_bodies",
    "ad_creative_link_titles",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "publisher_platforms",
    "languages",
)
# Optional (EU transparency) fields: requested only when diagnostics found them accepted; never required.
OPTIONAL_FIELDS: tuple[str, ...] = (
    "eu_total_reach",
    "beneficiary_payers",
    "target_ages",
    "target_gender",
    "target_locations",
    "age_country_gender_reach_breakdown",
    "total_reach_by_location",
)
# Values the advertiser declares (shown as declarations, never as facts about the operator).
DECLARED_FIELDS = frozenset({"beneficiary_payers", "target_ages", "target_gender", "target_locations"})
ESTIMATE_FIELDS = frozenset({"eu_total_reach", "total_reach_by_location", "age_country_gender_reach_breakdown"})

SEARCH_TERMS_MAX = 100
PAGE_IDS_MAX = 10
AD_TYPE = "ALL"
# Graph error codes (S1 error table, S3): throttling / transient, token, parameter.
THROTTLE_CODES = frozenset({4, 17, 32, 341, 613, 80004})
TRANSIENT_CODES = frozenset({1, 2})
TOKEN_CODES = frozenset({190, 102, 463, 467})
PERMISSION_CODES = frozenset({10, 200, 2332})  # e.g. identity confirmation / terms not accepted
PARAM_CODES = frozenset({100, 1009, 2500})


def graph_version() -> str:
    v = (os.environ.get("HYDRA_META_GRAPH_VERSION") or GRAPH_VERSION_DEFAULT).strip()
    return v if v.startswith("v") and v[1:].replace(".", "").isdigit() else GRAPH_VERSION_DEFAULT


def archive_url() -> str:
    return f"{GRAPH_HOST}/{graph_version()}/ads_archive"


def library_link(ad_id: str) -> str:
    """Public Ad Library page of one ad (no token): what the analyst opens."""
    return f"https://www.facebook.com/ads/library/?id={ad_id}"


LAYERS_PL = {
    "token": "Token przyjęty przez Graph API",
    "query_pl_all": "Zapytanie PL, ad_type=ALL obsługiwane",
    "required_fields": "Pełny minimalny zestaw pól importera obsługiwany",
    "optional_fields": "Pola opcjonalne (UE) rozpoznane",
    "normalise_store": "Rekord przechodzi normalizację i zapis",
}
