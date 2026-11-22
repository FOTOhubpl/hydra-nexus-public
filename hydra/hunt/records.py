"""HUNT records (INNOWACJE §11). Kept outside `hydra/contracts.py` (architect-owned); proposed there via
docs/CONTRACT_REQUESTS.md (CR-HUNT-1).

Three times on every Observation (INNOWACJE §11):
- `event_time`   when the event happened according to the source;
- `available_at` from when the information was available to our process (the replay gate uses ONLY this);
- `collected_at` when we actually fetched it (acquisition; for a store incident: its import time);
- `completed_at` when extraction / enrichment of THIS evidence version finished (CR-ASTRA-RES-4);
- `evidence_version` identifies the exact evidence content; a later version of the same `meta.evidence_series`
  supersedes the earlier one in a `KnowledgeView` only from its own `available_at` on.
`available_at` is never earlier than acquisition or completion, and an unknown time is never back-dated.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hydra.events import canonical_json


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class Status(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNVERIFIABLE = "unverifiable"


class DatasetKind(StrEnum):
    REAL = "REAL"
    SYNTHETIC = "SYNTHETIC"


class Mode(StrEnum):
    TEMPORAL_REPLAY = "TEMPORAL_REPLAY"
    LIVE = "LIVE"          # issued now against the local store; verified when later material is imported


class Tier(StrEnum):
    OPERATOR = "operator"        # rare operator marker (tracker/account id, phone, name generator) - link evidence
    KIT = "kit"                  # shared tool / template / page structure - kit evidence, not operator
    DECORATION = "decoration"    # brand, person, headline, domain, image: exchangeable between variants
    BOILERPLATE = "boilerplate"  # CDN, fonts, libraries, list labels: 0 weight


class Trait(_M):
    """One gene as the hunt layer sees it. `group` is the independent evidence group (rule 6: several genes from
    one script are one group). `source` is the artefact kind the action catalogue can read."""

    kind: str
    value: str
    tier: Tier
    group: str = Field(description="coarse evidence group: operator | page_structure | name_pattern | decoration")
    dep: str = Field(default="", description="dependency group: traits with one dep are ONE independent source")
    source: str = Field(description="har | dom | ocr | image | metadata | name | text | url")
    provenance: str = "OBS"  # OBS | INF | HYP (HYP never confirms anything, rule 2)

    @property
    def key(self) -> str:
        return f"{self.kind}={self.value}"


class SignatureGene(_M):
    kind: str
    value: str
    tier: Tier
    group: str
    dep: str
    source: str
    role: str = Field(description="invariant | decoration | kit | boilerplate")
    members_with: int
    members: int
    stability: float
    background_df: int
    background_n: int
    background_basis: str = Field(description="kit_users (conditional) | corpus")
    rarity: float
    reliability: float
    utility: float = Field(description="stability x rarity x reliability; heuristic, NOT a probability")


class Signature(_M):
    campaign_id: str
    campaign_revision: str
    approval_basis: str = Field(description="analyst (must-link/added) | published_revision | replay_seed")
    members: list[str]
    genes: list[SignatureGene]
    invariants: list[str]          # trait keys
    allowed_mutations: list[str]   # kinds observed to vary between approved members
    mutations_basis: str           # observed | experimental_rule
    required_evidence_groups: list[str]
    hard_conflicts: list[str]
    kit_users: list[str] = Field(default_factory=list)
    score_kind: str = "heuristic_priority"
    priority: float = 0.0
    sha256: str = ""
    dataset_kind: DatasetKind = DatasetKind.REAL

    def seal(self) -> Signature:
        body = self.model_dump(mode="json", exclude={"sha256"})
        self.sha256 = hashlib.sha256(canonical_json(body).encode()).hexdigest()
        return self


class Observation(_M):
    observation_id: str
    source: str
    ref: str = Field(description="host or incident id")
    event_time: str | None = None
    available_at: str
    available_at_basis: str = Field(description="how available_at was obtained (recorded / assumed lag / import)")
    collected_at: str | None = None
    completed_at: str | None = None
    evidence_version: str | None = None
    payload_sha256: str
    evidence_pointer: str
    status: str = "observed"  # observed | unavailable (rule 3: unknown, never evidence)
    traits: list[Trait] = Field(default_factory=list)
    hidden: list[Trait] = Field(default_factory=list,
                                description="traits present in EXISTING artefacts but not read yet (next-evidence)")
    dataset_kind: DatasetKind = DatasetKind.REAL
    derived_from_hypothesis: str | None = None  # set => never confirms that hypothesis (rule 2)
    meta: dict[str, Any] = Field(default_factory=dict)


class Hypothesis(_M):
    hypothesis_id: str = ""
    campaign_revision: str
    campaign_id: str
    signature_sha256: str
    issued_at: str
    knowledge_cutoff: str
    corpus_manifest_sha256: str
    target: str = "new_observed_variant"
    statement_pl: str
    horizon_hours: float
    horizon_end: str
    invariants: list[str]
    invariant_groups: dict[str, str] = Field(default_factory=dict)  # invariant key -> coarse evidence group
    invariant_sources: dict[str, str] = Field(default_factory=dict)  # invariant key -> artefact source it came from
    conflict_kinds: list[str] = Field(default_factory=list, description="operator-id kinds: another value = conflict")
    decoration_keys: list[str] = Field(default_factory=list, description="members' decoration (brand, person...)")
    allowed_mutations: list[str]
    required_evidence_groups: list[str]
    hard_conflicts: list[str]
    sources: list[str]
    query_budget: int
    score_kind: str = "heuristic_priority"
    priority: float = 0.0
    status: Status = Status.PENDING
    evidence_ids_at_issue: list[str] = Field(default_factory=list)
    known_refs: list[str] = Field(default_factory=list, description="approved members at issue: never a new variant")
    evaluation_protocol: str = "temporal_replay_v1"
    dataset_kind: DatasetKind = DatasetKind.REAL
    mode: Mode = Mode.TEMPORAL_REPLAY
    versions: dict[str, str] = Field(default_factory=dict)

    def content_id(self) -> str:
        body = self.model_dump(mode="json", exclude={"hypothesis_id", "status"})
        return "H-" + hashlib.sha256(canonical_json(body).encode()).hexdigest()[:16]


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()
