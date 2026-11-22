# ADR-008: Optional LLM analyst layer (local Bielik by default; Claude on Bedrock optional), cached and replayable
Status: Accepted (architect + operator authorisation to process organiser data in the cloud, 2026-11-21); amends
ADR-003 for this layer only.

Context: the task allows optional external services if they are documented and verifiable without our keys
(internal notes, not published, update 2026-11-21 ~14:45). Interpretability is 15% of the score. Rule-based NAR genes
(`nar@1`) miss narrative facts that are only visible in the creative image (brand logos, quoted names, amounts),
and analysts want a short Polish summary of a campaign card.

Decision:
1. `hydra/llm/` calls Claude through `anthropic.AnthropicBedrock` (SDK `anthropic[bedrock]` 2.12, Apache-2.0),
   region `us-east-1`, inference profiles from env: `HYDRA_LLM_MODEL` (default `us.anthropic.claude-sonnet-5-5`,
   briefs), `HYDRA_LLM_FAST_MODEL` (default `us.anthropic.claude-haiku-5-5`, extraction),
   `HYDRA_LLM_FALLBACK_MODEL` (default `us.anthropic.claude-sonnet-5`, one retry on `stop_reason=refusal`).
2. Off by default. `HYDRA_LLM=1`: live (cache first). `HYDRA_LLM=replay`: cache only, no network; this is how
   a reviewer verifies the outputs without our AWS account. Unset: the UI does not call the layer.
3. Every request/response is one JSON file `<cache_dir>/llm/<task>/<sha256(model|prompt digest|inputs digest)>.json`
   (same pattern as the ADR-007 enrichment cache); calls, tokens, latency and estimated cost go to
   `<cache_dir>/llm/calls.jsonl`. No cache entry and no access → status `unavailable` with a Polish reason,
   never an exception.
4. Text is redacted with `hydra.redact` before sending; images are downscaled (max side 1024 px, JPEG) and only
   their digests are cached. AWS credentials come from the standard AWS chain and are never logged or cached.
5. The LLM is never a linking rule. `llm_narrative@1` genes are family NAR, provenance INF, dependency group
   `nar:llm`, kinds `llm_*`; the NAR campaign cap is 0 (`hydra/synapse.py`), so they cannot create or support a
   campaign link; they only add capped style bits to campaign-group evidence and feed briefs and search. The
   extractor is not in the default pipeline (`base.load_family_modules`); `python -m hydra.llm narrative` appends
   its genes to the stored genome.
6. Briefs and pair explanations (`hydra.llm.brief_for_campaign(card)`, `explain_pair(pair)`, CR-UI-5) are generated
   only from a numbered list of stored facts; a validator rejects answers that cite nothing, cite a missing fact,
   miss the word range or contain a number that is not in the facts, and the model regenerates (max 3 attempts).
   Results are also stored in `<state_dir>/llm/<run_id>.json` (no schema change).

Amendment 2026-11-21 ~17:00 (operator priority: open source, local, no paid API):
7. Default backend is `local`: a Bielik model (SpeakLeash, Apache-2.0, GGUF) served by llama.cpp `llama-server`
   (MIT) on 127.0.0.1 (`HYDRA_LLM_BACKEND` unset or `local`). Paid backends (`bedrock`) are used only when
   `HYDRA_LLM_BACKEND` selects them explicitly. Cache namespace per backend and model.
8. `HYDRA_OFFLINE=1` (audit A09) degrades paid backends from live to replay; the loopback server stays allowed,
   because it is not network egress.
9. Bielik reads no images: the local `llm_narrative` uses the extracted text only. Prompts are shorter, JSON is
   grammar-constrained (`response_format.json_schema`), briefs get at most 35 facts; the validator is the same.

10. Packaging (architect 2026-11-21 ~18:00): HYDRA_LLM stays off in docker compose and the demo; no weights in the
   repo or image; `scripts/llm_fetch.py` downloads one pinned GGUF (4.5B Q4_K_M, or 1.5B Q4_K_M); the opt-in
   `anthropic` backend takes its key from `ANTHROPIC_API_KEY` only; the replay cache built on NASK data is never
   committed.

Consequences: the demo and the hidden-set run never depend on the layer. Organiser text and images leave the
machine only with `HYDRA_LLM=1`; the cost is measured per call internal notes, not published. Bedrock prices differ
from Anthropic list prices, so cost figures are estimates. Generated text is labelled as generated in the UI and
its citations link to the evidence rows.
