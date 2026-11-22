"""CORE extractor: exact duplicate artifacts across incidents (sha256 of the original bytes).

The artifact id is already the sha256, so this costs nothing and catches re-posted creatives, copied
landing pages and shared kit files. Not one of the family streams' modules; the pipeline imports it.
Family by role: creatives/screenshots/videos → VIS, landing html/assets/HAR → INF, metadata → META.
Tiny files (< 512 bytes: blank iframes, 1x1 pixels) and public libraries (jQuery, fbevents, GTM, …, by
logical file name) are skipped as boilerplate. Code files (JS/CSS/HTML/fonts) are kit-level evidence
(`kit:code_files`): an identical bundle means a shared toolkit, not necessarily one campaign (CAMPAIGNS.md:
the aio SDK `main.css` is shared across the INV-A umbrella).
"""

from __future__ import annotations

import re

from hydra.contracts import ArtifactRole, EvidencePointer, Family, Gene
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register

VIS_ROLES = {ArtifactRole.AD_CREATIVE, ArtifactRole.FEED_SCREENSHOT, ArtifactRole.SCREEN_RECORDING,
             ArtifactRole.AD_VIDEO, ArtifactRole.LANDING_SCREENSHOT}
INF_ROLES = {ArtifactRole.LANDING_HTML, ArtifactRole.LANDING_ASSET, ArtifactRole.HAR, ArtifactRole.URL}
MIN_SIZE = 512
PUBLIC_LIB_RE = re.compile(
    r"(jquery|bootstrap|fbevents|gtm|gtag|analytics|hls(\.min)?\.js|swiper|sweetalert|embla|vue|react|lodash|"
    r"fontawesome|font-awesome|popper|slick|owl\.carousel|moment|axios|polyfill|recaptcha|saved_resource|"
    r"element\.js|events-stat|ios-app|back-nav|controls\.js|lazysizes|wp-emoji|modernizr)", re.I)
CODE_MIME = ("application/javascript", "text/css", "text/html", "font/", "application/json", "text/plain")


def _inf_present() -> bool:
    from hydra.genome.base import REGISTRY

    return any(name.startswith("inf_") for name in REGISTRY)


@register("artifact_sha256", family=Family.INF, version=3)
def artifact_sha256(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    out = ExtractResult()
    seen: set[str] = set()
    for a in inc.artifacts:
        m = a.meta
        if m.artifact_id in seen or m.size < MIN_SIZE:
            continue
        seen.add(m.artifact_id)
        role = ArtifactRole(m.role)
        name = str(m.meta.get("name") or m.path_in_incident.rsplit("/", 1)[-1])
        if role == ArtifactRole.LANDING_ASSET and (ctx.options.get("skip_landing_assets", _inf_present())
                                                   or PUBLIC_LIB_RE.search(name)):
            continue  # GENES-INF emits tiered asset_sha256 genes for page resources
        fam = Family.VIS if role in VIS_ROLES else Family.INF if role in INF_ROLES else Family.META
        kind, group = "artifact_sha256", f"core:{fam.value.lower()}_files"
        if role == ArtifactRole.LANDING_ASSET:
            if m.mime.startswith(CODE_MIME):
                kind, group = "asset_sha256_code", "kit:code_files"
            else:
                kind, group = "asset_sha256", "core:landing_media"
        out.genes.append(Gene(
            incident_id=inc.incident.id, family=fam, kind=kind, value=m.artifact_id, extractor="artifact_sha256@3",
            evidence=EvidencePointer(artifact_id=m.artifact_id, note=m.path_in_incident[:200]),
            dependency_group=group,
        ))
    return out
