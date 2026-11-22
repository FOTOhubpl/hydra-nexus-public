"""Optional LLM analyst layer (ADR-008): Claude on Amazon Bedrock, cached and replayable, never required.

UI/API contract (CR-UI-5, hydra/api_task.py):
    is_enabled(settings) -> bool                          HYDRA_LLM=1 (live) or HYDRA_LLM=replay (cache only)
    brief_for_campaign(card: dict, *, settings) -> dict   card = GET /v1/campaigns/{id} body
    explain_pair(pair: dict, *, settings) -> dict          pair = GET /v1/pairs/{a}/{b} body
    → {"status": "ok", "text_pl", "citations": [{"n", "section", "index", "label"}], "model", "generated_at"}
    → {"status": "unavailable" | "error", "reason_pl"}

Modules: `client` (cache-first Bedrock client), `narrative` (`llm_narrative@1` NAR extractor; not in the
default pipeline, run `python -m hydra.llm narrative`), `brief` (grounded briefs + validator), `api` (optional
router). The LLM is never a linking rule: its genes are NAR (campaign cap 0) and its text is never scored.
"""

from __future__ import annotations

from typing import Any

from hydra.llm.client import LLMClient, LLMConfig, LLMResult, get_client, llm_mode

__all__ = ["LLMClient", "LLMConfig", "LLMResult", "brief_for_campaign", "explain_pair", "get_client", "is_enabled",
           "status"]


def is_enabled(settings: Any = None) -> bool:
    return llm_mode() in ("live", "replay")


def _guard(fn, payload: dict[str, Any], settings: Any) -> dict[str, Any]:
    try:
        rec = fn(payload, settings=settings)
    except Exception as exc:  # noqa: BLE001 — the evidence views never depend on the LLM
        return {"status": "error", "reason_pl": f"Warstwa AI zwróciła błąd ({type(exc).__name__})."}
    if rec.get("status") != "ok":
        rec.setdefault("reason_pl", "Warstwa AI niedostępna.")
    return rec


def brief_for_campaign(card: dict[str, Any], *, settings: Any = None) -> dict[str, Any]:
    from hydra.llm import brief

    return _guard(brief.brief_for_campaign, card, settings)


def explain_pair(pair: dict[str, Any], *, settings: Any = None) -> dict[str, Any]:
    from hydra.llm import brief

    return _guard(brief.explain_pair, pair, settings)


def status() -> dict[str, Any]:
    """Configuration visible to the UI (no credentials)."""
    c = get_client().cfg
    mode = "replay" if c.offline_blocked else llm_mode()
    out = {"enabled": is_enabled(), "mode": mode, "backend": c.backend, "model": c.model,
           "fast_model": c.fast_model, "cache_dir": str(c.cache_dir)}
    if c.offline_blocked:
        out["note_pl"] = "HYDRA_OFFLINE=1: Bedrock zablokowany, odpowiedzi tylko z pamięci podręcznej."
    out.update({"local_url": c.local_url, "local_fast_url": c.local_fast_url} if c.is_local else {"region": c.region})
    return out
