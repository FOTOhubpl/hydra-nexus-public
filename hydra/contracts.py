"""Shared data contracts (owned by the architect; other streams import, never edit).

Unit of analysis = **Incident**: one reported case (the NASK sample ships one folder per incident with ad
creatives, screen recordings, saved landing pages, HAR logs and platform metadata). Every feature is a
**Gene** of an incident, pointing back to the artifact and the exact place it came from. Pairs of incidents
get a **PairScore** with per-family evidence bits. Campaigns are groups of incidents. A new incident gets an
**Assignment**: a known campaign, a new pattern, or abstain.

Rules encoded here (CLAUDE.md, SPEC_ERRATA, PLAN_MAX):
- A missing observation has status `unavailable` / `failed` / `not_applicable`. It is unknown, never evidence
  against a link.
- Provenance: OBS = observed in the material, INF = inferred by a model or computation, HYP = hypothesis
  from the scoring layer. Enrichment facts are OBS from an external source and carry `source` + `fetched_at`.
- Bits are evidence weights (log2 likelihood ratio contributions). `probability` is calibrated only when
  `calibrated=True`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CONTRACTS_VERSION = "2.2.0"  # 2.1: campaign groups; 2.2: meta on PairScore/Assignment (CR-CORE-4)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, use_enum_values=True)


# --------------------------------------------------------------------------------------- enums


class ArtifactRole(StrEnum):
    AD_CREATIVE = "ad_creative"            # screenshot/photo of the ad itself
    FEED_SCREENSHOT = "feed_screenshot"    # phone/desktop screenshot of a feed/app containing the ad
    SCREEN_RECORDING = "screen_recording"  # video of the user journey
    AD_VIDEO = "ad_video"                  # the ad's own video (e.g. FB ad + .info.json)
    LANDING_HTML = "landing_html"          # saved landing page (html / mhtml)
    LANDING_ASSET = "landing_asset"        # resource saved with the page (_files/: images, css, js)
    LANDING_SCREENSHOT = "landing_screenshot"
    HAR = "har"                            # network log
    PLATFORM_METADATA = "platform_metadata"  # .info.json and similar
    TEXT = "text"                          # pasted text, chat/communication log
    URL = "url"                            # URL given as the starting hint
    OTHER = "other"


class Family(StrEnum):
    VIS = "VIS"    # visual: hashes, embeddings, geometry, logos
    TXT = "TXT"    # text: OCR, page text, normalised phrases, entities, embeddings
    INF = "INF"    # infrastructure: domains, URL skeletons, redirects, IPs, ASN, NS, certs, trackers, DOM
    ID = "ID"      # identity: faces, named persons
    NAR = "NAR"    # narrative: scheme type, motifs, persona, promise, CTA
    META = "META"  # platform metadata: ad/page IDs, uploader, dates


class GeneStatus(StrEnum):
    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"        # source missing / page unreachable / lookup failed upstream
    FAILED = "failed"                  # our extractor crashed on this input
    NOT_APPLICABLE = "not_applicable"  # e.g. faces on a page without people


class Provenance(StrEnum):
    OBS = "OBS"
    INF = "INF"
    HYP = "HYP"


class Hypothesis(StrEnum):
    SAME_CAMPAIGN = "same_campaign"
    SHARED_TEMPLATE = "shared_template"  # same kit/template/agency, not proven same campaign
    SHARED_ASSET = "shared_asset"        # same image/video/person reused, nothing else
    INDEPENDENT = "independent"
    ABSTAIN = "abstain"                  # evidence weak or contradictory → analyst queue


class AssignmentDecision(StrEnum):
    ASSIGN = "assign"              # joins an existing campaign
    NEW_CAMPAIGN = "new_campaign"  # no known campaign is supported → new pattern
    ABSTAIN = "abstain"            # ambiguous between campaigns or too little evidence


class ReviewAction(StrEnum):
    ACCEPT_LINK = "accept_link"
    REJECT_LINK = "reject_link"
    MERGE_CAMPAIGNS = "merge_campaigns"
    SPLIT_CAMPAIGN = "split_campaign"
    MOVE_INCIDENT = "move_incident"
    UNDO = "undo"


# --------------------------------------------------------------------------------------- input side


class IncidentArtifact(_Model):
    artifact_id: str = Field(description="sha256 of the original bytes (CAS id)")
    role: ArtifactRole
    path_in_incident: str = Field(description="relative path inside the incident folder / archive")
    mime: str
    size: int
    meta: dict[str, Any] = Field(default_factory=dict)  # e.g. video duration, image size, page title


class Incident(_Model):
    id: str = Field(description="stable id, e.g. 'nask-61' for sample folder 61, or 'new-<sha8>'")
    source: str = Field(description="dataset name or input channel: nask-sample, upload, url, api")
    source_ref: str
    title: str | None = None
    observed_at: str | None = None  # earliest date known from the material (ISO-8601), if any
    artifacts: list[IncidentArtifact] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    label: str | None = Field(default=None, description="ground-truth campaign label if known (eval only)")
    group_label: str | None = Field(default=None, description="ground-truth campaign-group (umbrella) label")


# --------------------------------------------------------------------------------------- genes


REDACTED = "[REDACTED]"  # secrets found in material (tokens, API keys, local usernames) never leave as-is


class EvidencePointer(_Model):
    """Where exactly a gene was observed, so the UI can show it."""

    artifact_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # x, y, w, h in source pixels
    char_range: tuple[int, int] | None = None
    timecode_s: float | None = None
    har_entry: int | None = None       # index in HAR log.entries
    dom_path: str | None = None        # CSS/XPath-like path in the saved page
    enrichment_key: str | None = None  # EnrichmentRecord.key
    note: str | None = None


class Gene(_Model):
    incident_id: str
    family: Family
    kind: str = Field(description="e.g. pdq, phash_tile, dinov2, siglip2, ocr_line, phrase, entity_phone, "
                      "domain, host, url_skeleton, redirect_chain, ip, asn, ns, cert_san, tracker_id, "
                      "tds_param, dom_skeleton, asset_sha256, favicon_mmh3, face, logo, person_named, "
                      "scheme, platform_ad_id")
    value: str | None = Field(default=None, description="discrete value / hash hex / normalised text")
    vector_ref: str | None = Field(default=None, description="key into gene_vectors for embeddings")
    quality: float = Field(default=1.0, ge=0.0, le=1.0)
    status: GeneStatus = GeneStatus.OBSERVED
    provenance: Provenance = Provenance.OBS
    evidence: EvidencePointer = Field(default_factory=EvidencePointer)
    extractor: str = Field(description="name@version, e.g. 'har_hosts@1'")
    dependency_group: str | None = Field(default=None, description="genes in one group share one bit budget")


class EnrichmentRecord(_Model):
    key: str = Field(description="sha256(source|query)")
    source: str = Field(description="rdap, dns, crtsh, asn, cert_pl_list, http_redirects, ...")
    query: str
    fetched_at: str
    status: GeneStatus
    response_sha256: str | None = None
    cache_path: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------- scoring


class GeneMatch(_Model):
    """One reason in the bit waterfall."""

    family: Family
    kind: str
    value_a: str | None = None
    value_b: str | None = None
    similarity: float | None = None
    doc_freq: int | None = Field(default=None, description="incidents in the corpus carrying this value")
    bits: float = Field(description="signed contribution; negative = evidence against")
    provenance: Provenance = Provenance.INF
    evidence_a: EvidencePointer | None = None
    evidence_b: EvidencePointer | None = None
    explanation_pl: str = Field(description="one-line Polish explanation for the analyst")


class PairScore(_Model):
    a: str
    b: str
    family_bits: dict[str, float] = Field(default_factory=dict)  # Family -> capped bits after grouping
    missing_families: list[str] = Field(default_factory=list)    # unknown, not penalised
    contradictions: list[GeneMatch] = Field(default_factory=list)
    reasons: list[GeneMatch] = Field(default_factory=list)       # top reasons, ordered by |bits|
    total_bits: float
    probability: float = Field(ge=0.0, le=1.0)
    calibrated: bool = False
    hypothesis: Hypothesis
    candidate_sources: list[str] = Field(default_factory=list)   # which indexes proposed the pair
    model: str = Field(description="scorer name@version, e.g. 'lr_isotonic@1' or 'rarity_bits@1'")
    meta: dict[str, Any] = Field(default_factory=dict, description="e.g. robustness, robust_link, levels")


# --------------------------------------------------------------------------------------- campaigns


class Campaign(_Model):
    id: str
    label: str = Field(description="short human label, e.g. 'Fałszywy artykuł: zamach na …'")
    members: list[str] = Field(default_factory=list)  # incident ids
    core_genome: list[GeneMatch] = Field(default_factory=list)  # genes shared by most members (pivots)
    scheme: str | None = None  # investment / health / gambling / fake-news-bait / ...
    first_seen: str | None = None
    last_seen: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: str = "active"  # active | merged | split | archived
    group_id: str | None = Field(default=None, description="campaign group (umbrella: shared kit/operator)")
    meta: dict[str, Any] = Field(default_factory=dict)


class CampaignGroup(_Model):
    """Group of similar campaigns (task §2: 'grupy podobnych kampanii'): shared toolkit, trackers or
    infrastructure, but different stories/brands. Linked by weaker or kit-level evidence than campaigns."""

    id: str
    label: str
    campaign_ids: list[str] = Field(default_factory=list)
    shared_genes: list[GeneMatch] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class AssignmentCandidate(_Model):
    campaign_id: str
    probability: float = Field(ge=0.0, le=1.0)
    best_pair: PairScore | None = None
    support: int = Field(default=0, description="member incidents with a supporting pair")


class Assignment(_Model):
    incident_id: str
    decision: AssignmentDecision
    campaign_id: str | None = None
    group_id: str | None = Field(default=None, description="set when a new campaign still fits a known group")
    candidates: list[AssignmentCandidate] = Field(default_factory=list)
    reason_pl: str
    model: str
    meta: dict[str, Any] = Field(default_factory=dict, description="e.g. conformal set and p-values")


class Review(_Model):
    id: str
    action: ReviewAction
    target: dict[str, Any]  # e.g. {"a": "...", "b": "..."} or {"campaign_ids": [...]}
    analyst: str = "operator"
    note: str | None = None
    created_at: str
    undoes: str | None = None
