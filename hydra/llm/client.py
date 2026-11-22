"""Optional LLM client (ADR-008): Claude on Amazon Bedrock or a LOCAL Bielik model (llama.cpp `llama-server`,
OpenAI-compatible, loopback only) behind one replayable on-disk cache.

    from hydra.llm.client import LLMClient
    llm = LLMClient()                       # HYDRA_LLM=1 enables live calls; otherwise replay-only
    res = llm.complete(task="brief", model=llm.cfg.model, system=..., content=[...], inputs_digest=...)
    res.status in ("ok", "unavailable"); res.reason_pl explains "unavailable" in Polish

Rules (mirrors ADR-007 enrichment cache):
- every request/response is stored as one JSON file `<cache>/llm/<task>/<key>.json`,
  key = sha256(model | prompt digest | inputs digest); replay serves that file without network;
- no cache entry and no live access (HYDRA_LLM unset, no AWS credentials, network error) → status
  `unavailable` with a Polish reason, never an exception;
- text is redacted with `hydra.redact` before it is sent; images are sent as downscaled copies and only their
  digests are written to the cache (the response is cached, not the organiser's image bytes);
- cost (estimated from list prices) and latency go to `<cache>/llm/calls.jsonl`.

Configuration is read from the environment (Settings has no LLM fields yet: CR-LLM-1):
HYDRA_LLM (0 / 1 / replay), HYDRA_LLM_MODEL, HYDRA_LLM_FAST_MODEL, HYDRA_LLM_FALLBACK_MODEL, HYDRA_LLM_REGION,
HYDRA_LLM_TIMEOUT_S, HYDRA_LLM_MAX_RETRIES, HYDRA_LLM_CACHE; backend selection: HYDRA_LLM_BACKEND (bedrock |
local), HYDRA_LLM_LOCAL_URL (default http://127.0.0.1:8090/v1), HYDRA_LLM_LOCAL_MODEL (default: the id the server
reports at /v1/models), HYDRA_LLM_LOCAL_FAST_URL / HYDRA_LLM_LOCAL_FAST_MODEL (narrative; default = the main ones).
Cache namespace per backend + model: `<cache>/<bedrock | local-<model>>/<task>/<key>.json`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hydra.config import Settings
from hydra.redact import redact_obj, redact_text

log = logging.getLogger(__name__)

DEFAULT_MODEL = "us.anthropic.claude-sonnet-5-5"        # briefs, pair explanations
DEFAULT_FAST_MODEL = "us.anthropic.claude-haiku-5-5"     # extraction (llm_narrative)
DEFAULT_FALLBACK_MODEL = "us.anthropic.claude-sonnet-5"  # one retry when a model declines (stop_reason=refusal)
DEFAULT_LOCAL_URL = "http://127.0.0.1:8090/v1"
# Direct Anthropic API (paid; opt-in only with HYDRA_LLM_BACKEND=anthropic). The key comes from ANTHROPIC_API_KEY
# in the environment and is handed to the SDK; it is never logged, cached or persisted.
DEFAULT_API_MODEL = "claude-sonnet-5-5"
DEFAULT_API_FAST_MODEL = "claude-haiku-5-5"
DEFAULT_API_FALLBACK_MODEL = "claude-sonnet-5"
BACKEND_PL = {"bedrock": "Bedrock", "anthropic": "Anthropic API", "local": "lokalny model"}
_SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")

# USD per 1M tokens (input, output). Anthropic list prices (claude-api skill, cached 2026-11-03); Bedrock bills
# separately and may differ, so every cost figure is an ESTIMATE. Cache reads are billed at 0.1 x input.
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5-5": (4.0, 20.0),
}


def llm_mode() -> str:
    """HYDRA_LLM=1 → live (cache first, then Bedrock); HYDRA_LLM=replay → cache only (keyless verification);
    unset/0 → off (the UI does not call the layer; the CLI still replays cached answers)."""
    v = os.environ.get("HYDRA_LLM", "").strip().lower()
    if v in ("1", "true", "yes", "on", "live"):
        return "live"
    return "replay" if v == "replay" else "off"


@dataclass
class LLMConfig:
    enabled: bool                    # live calls allowed
    model: str
    fast_model: str
    fallback_model: str | None
    region: str
    timeout_s: float
    max_retries: int
    cache_dir: Path
    backend: str = "bedrock"            # bedrock | local
    offline_blocked: bool = False       # live requested, but HYDRA_OFFLINE=1 forbids egress → replay only
    offline: bool | None = None         # Settings.offline seen by from_env; None (hand-built config) → environment
    local_url: str = DEFAULT_LOCAL_URL
    local_fast_url: str = DEFAULT_LOCAL_URL

    @property
    def is_local(self) -> bool:
        return self.backend == "local"

    def url_for(self, model: str) -> str:
        return self.local_fast_url if (model == self.fast_model and self.local_fast_url) else self.local_url

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> LLMConfig:
        s = settings or Settings()
        assert s.cache_dir is not None
        cache = os.environ.get("HYDRA_LLM_CACHE")
        # Default = local Bielik (open source, no paid API); paid backends only when explicitly selected.
        backend = (os.environ.get("HYDRA_LLM_BACKEND") or "local").strip().lower()
        # HYDRA_OFFLINE=1 (Settings.offline) is the hard network kill switch (audit A09): Bedrock is network egress,
        # so "live" degrades to replay. A local llama-server on 127.0.0.1 is not egress and stays allowed.
        if backend == "local":
            url = os.environ.get("HYDRA_LLM_LOCAL_URL") or DEFAULT_LOCAL_URL
            fast_url = os.environ.get("HYDRA_LLM_LOCAL_FAST_URL") or url
            # Server down and no explicit id: only the default URL maps to the default model name; any other URL gets
            # a URL-derived name, so a dead server never replays another model's cache under the wrong label.
            model = (os.environ.get("HYDRA_LLM_LOCAL_MODEL") or local_model_id(url)
                     or (DEFAULT_LOCAL_MODEL if url == DEFAULT_LOCAL_URL else "unknown@" + url.split("://")[-1]))
            fast = os.environ.get("HYDRA_LLM_LOCAL_FAST_MODEL") or (
                model if fast_url == url else (local_model_id(fast_url) or "local-fast"))
            return cls(enabled=llm_mode() == "live", model=model, fast_model=fast, fallback_model=None,
                       region="local", timeout_s=float(os.environ.get("HYDRA_LLM_TIMEOUT_S") or 600),
                       max_retries=int(os.environ.get("HYDRA_LLM_MAX_RETRIES") or 1),
                       cache_dir=Path(cache) if cache else Path(s.cache_dir) / "llm", backend="local",
                       local_url=url, local_fast_url=fast_url)
        blocked = llm_mode() == "live" and bool(s.offline)
        if backend == "anthropic":
            return cls(
                enabled=llm_mode() == "live" and not blocked, offline_blocked=blocked, backend="anthropic",
                offline=bool(s.offline),
                model=os.environ.get("HYDRA_LLM_API_MODEL") or DEFAULT_API_MODEL,
                fast_model=os.environ.get("HYDRA_LLM_API_FAST_MODEL") or DEFAULT_API_FAST_MODEL,
                fallback_model=(os.environ.get("HYDRA_LLM_API_FALLBACK_MODEL", DEFAULT_API_FALLBACK_MODEL) or None),
                region="api.anthropic.com", timeout_s=float(os.environ.get("HYDRA_LLM_TIMEOUT_S") or 90),
                max_retries=int(os.environ.get("HYDRA_LLM_MAX_RETRIES") or 2),
                cache_dir=Path(cache) if cache else Path(s.cache_dir) / "llm")
        return cls(
            enabled=llm_mode() == "live" and not blocked, offline_blocked=blocked, offline=bool(s.offline),
            model=os.environ.get("HYDRA_LLM_MODEL") or DEFAULT_MODEL,
            fast_model=os.environ.get("HYDRA_LLM_FAST_MODEL") or DEFAULT_FAST_MODEL,
            fallback_model=(os.environ.get("HYDRA_LLM_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL) or None),
            region=os.environ.get("HYDRA_LLM_REGION") or os.environ.get("AWS_REGION") or "us-east-1",
            timeout_s=float(os.environ.get("HYDRA_LLM_TIMEOUT_S") or 90),
            max_retries=int(os.environ.get("HYDRA_LLM_MAX_RETRIES") or 2),
            cache_dir=Path(cache) if cache else Path(s.cache_dir) / "llm",
        )


def _api_key() -> str | None:
    """ANTHROPIC_API_KEY from the process environment only (architect decision 2026-11-21; the operator exports it,
    e.g. `set -a; . ~/.config/hydra/anthropic.env; set +a`, or Docker `env_file:` at runtime). SDK only."""
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None


def scrub(text: str) -> str:
    """Remove anything shaped like an Anthropic API key before an error message is stored or shown."""
    return _SECRET_RE.sub("[REDACTED]", text)


_MODEL_IDS: dict[str, tuple[str | None, float]] = {}
DEFAULT_LOCAL_MODEL = "bielik-4.5b-v3.0-instruct-q8_0"  # llama-server --alias used in docs/genes/LLM.md


def _check_loopback(url: str) -> None:
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    if host not in LOOPBACK:
        raise ValueError(f"HYDRA_LLM_LOCAL_URL must be loopback (127.0.0.1), got {host!r}")


def local_model_id(url: str, timeout: float = 2.0) -> str | None:
    """The model id llama-server reports (GET /v1/models), memoised; None when the server is down."""
    hit = _MODEL_IDS.get(url)
    if hit is not None and (hit[0] is not None or time.monotonic() - hit[1] < 30):
        return hit[0]
    mid = None
    try:
        _check_loopback(url)
        with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=timeout) as r:  # noqa: S310 — loopback
            data = json.loads(r.read())
        items = data.get("data") or data.get("models") or []
        if items:
            mid = Path(str(items[0].get("id") or items[0].get("name") or "")).name or None
    except (OSError, ValueError):
        mid = None
    _MODEL_IDS[url] = (mid, time.monotonic())
    return mid


@dataclass
class LLMResult:
    status: str                      # ok | unavailable
    task: str
    model: str
    key: str
    text: str | None = None
    data: Any = None                 # parsed JSON when a schema was given
    reason_pl: str | None = None
    cached: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    latency_s: float | None = None
    cost_usd: float | None = None
    stop_reason: str | None = None
    fetched_at: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "model": self.model, "key": self.key, "cached": self.cached,
                "usage": self.usage, "latency_s": self.latency_s, "cost_usd": self.cost_usd,
                "reason_pl": self.reason_pl, "fetched_at": self.fetched_at}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def cache_key(model: str, prompt_digest: str, inputs_digest: str) -> str:
    return sha256_text(f"{model}|{prompt_digest}|{inputs_digest}")


def estimate_cost(model: str, usage: dict[str, int]) -> float | None:
    base = model.split(".")[-1].split(":")[0]
    for name, (pin, pout) in sorted(PRICES.items(), key=lambda kv: -len(kv[0])):
        if base.startswith(name):
            inp = usage.get("input_tokens", 0) + 1.25 * usage.get("cache_creation_input_tokens", 0)
            inp += 0.1 * usage.get("cache_read_input_tokens", 0)
            return round((inp * pin + usage.get("output_tokens", 0) * pout) / 1e6, 6)
    return None


def _offline() -> bool:
    """Current kill-switch state (env first, so a running process honours HYDRA_OFFLINE set by the operator)."""
    env = os.environ.get("HYDRA_OFFLINE")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    from hydra.config import get_settings

    return bool(get_settings().offline)


def _redact_result(res: LLMResult) -> LLMResult:
    """Model answers can echo secrets from the evidence (CR-ASTRA-SEC-14): redact before display and caching."""
    res.text = redact_text(res.text)
    res.data = redact_obj(res.data)
    return res


def redact_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact text blocks (secrets, tokens, local user names) before they leave the machine."""
    out = []
    for b in content:
        if b.get("type") == "text":
            out.append({**b, "text": redact_text(b["text"]) or ""})
        else:
            out.append(b)
    return out


def _digest_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cache-safe view of the request content: text kept (already redacted), image bytes replaced by sha256."""
    out = []
    for b in content:
        if b.get("type") == "image":
            data = b.get("source", {}).get("data", "")
            out.append({"type": "image", "media_type": b.get("source", {}).get("media_type"),
                        "sha256": hashlib.sha256(data.encode()).hexdigest(), "b64_chars": len(data)})
        else:
            out.append(b)
    return out


class LLMClient:
    """Cache-first Bedrock / local client. Thread-safe enough for one API process."""

    def __init__(self, cfg: LLMConfig | None = None, settings: Settings | None = None, transport: Any = None):
        self.cfg = cfg or LLMConfig.from_env(settings)
        self._client = transport  # tests inject a fake with .messages.create(**kw)
        self._client_error: str | None = None
        self._lock = threading.Lock()
        self.stats = {"hit": 0, "miss": 0, "live": 0, "error": 0}

    # ------------------------------------------------------------------ paths
    def namespace(self, model: str) -> str:
        if self.cfg.is_local:
            return "local-" + re.sub(r"[^A-Za-z0-9._-]", "_", model)[:100]
        return "anthropic" if self.cfg.backend == "anthropic" else "bedrock"

    def path(self, task: str, key: str, model: str | None = None) -> Path:
        return self.cfg.cache_dir / self.namespace(model or self.cfg.model) / task / f"{key}.json"

    def _legacy_path(self, task: str, key: str) -> Path:
        return self.cfg.cache_dir / task / f"{key}.json"  # Bedrock cache written before namespaces (dc72bd9)

    def _log_call(self, rec: dict[str, Any]) -> None:
        try:
            self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
            with self._lock, (self.cfg.cache_dir / "calls.jsonl").open("a") as f:
                f.write(json.dumps(redact_obj(rec), ensure_ascii=False) + "\n")
        except OSError:
            log.warning("llm call log write failed", exc_info=True)

    # ------------------------------------------------------------------ transport
    def _transport(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_error is not None:
            return None
        if self.cfg.offline if self.cfg.offline is not None else _offline():
            # HYDRA_OFFLINE=1 dominates any explicitly enabled config (CR-ASTRA-SEC-8): no remote LLM socket.
            self._client_error = "HYDRA_OFFLINE=1"
            return None
        try:
            if self.cfg.backend == "anthropic":
                from anthropic import Anthropic

                key = _api_key()
                if not key:
                    raise RuntimeError("brak zmiennej ANTHROPIC_API_KEY")
                self._client = Anthropic(api_key=key, timeout=self.cfg.timeout_s, max_retries=self.cfg.max_retries)
            else:
                from anthropic import AnthropicBedrock

                # Credentials come from the standard AWS chain (env, ~/.aws, instance role); never logged or cached.
                self._client = AnthropicBedrock(aws_region=self.cfg.region, timeout=self.cfg.timeout_s,
                                                max_retries=self.cfg.max_retries)
        except Exception as exc:  # noqa: BLE001 — missing SDK / credentials → unavailable
            self._client_error = scrub(f"{type(exc).__name__}: {str(exc)[:200]}")
            return None
        return self._client

    # ------------------------------------------------------------------ main entry
    def complete(self, *, task: str, model: str, system: str, content: list[dict[str, Any]],
                 inputs_digest: str, schema: dict[str, Any] | None = None, max_tokens: int = 4000,
                 effort: str | None = "low", allow_fallback: bool = True) -> LLMResult:
        """One cached Messages call. `content` = user content blocks (text/image); text is redacted here.
        `inputs_digest` identifies the evidence (artifact ids, facts digest), so the key is stable even when
        image re-encoding differs between machines."""
        system = redact_text(system) or ""
        content = redact_content(content)
        prompt_digest = sha256_text(json.dumps({"system": system, "schema": schema, "effort": effort,
                                                "max_tokens": max_tokens,
                                                "text": [b["text"] for b in content if b.get("type") == "text"]},
                                               ensure_ascii=False, sort_keys=True))
        key = cache_key(model, prompt_digest, inputs_digest)
        p = self.path(task, key, model)
        if not p.is_file() and self.cfg.backend == "bedrock" and self._legacy_path(task, key).is_file():
            p = self._legacy_path(task, key)
        if p.is_file():
            try:
                j = json.loads(p.read_text())
                self.stats["hit"] += 1
                # Replays are redacted too: caches written before CR-ASTRA-SEC-14 may hold raw model output.
                res = LLMResult(status=j["status"], task=task, model=j["model"], key=key,
                                text=redact_text(j.get("text")), data=redact_obj(j.get("data")),
                                reason_pl=j.get("reason_pl"), cached=True,
                                usage=j.get("usage", {}), latency_s=j.get("latency_s"), cost_usd=j.get("cost_usd"),
                                stop_reason=j.get("stop_reason"), fetched_at=j.get("fetched_at"))
                self._log_call({"ts": now_iso(), "task": task, "model": res.model, "key": key, "cached": True,
                                "status": res.status})
                return res
            except (OSError, ValueError, KeyError):
                pass
        if not self.cfg.enabled:
            self.stats["miss"] += 1
            name = BACKEND_PL.get(self.cfg.backend, self.cfg.backend)
            why = (f"Tryb offline (HYDRA_OFFLINE=1): wywołania {name} są zablokowane, a w pamięci podręcznej"
                   " nie ma zapisanej odpowiedzi (tryb odtwarzania)." if self.cfg.offline_blocked else
                   "Analiza LLM jest wyłączona (HYDRA_LLM≠1) i brak zapisanej odpowiedzi w pamięci podręcznej"
                   " (tryb odtwarzania).")
            return LLMResult("unavailable", task, model, key, cached=False, reason_pl=why)
        if self.cfg.is_local:
            res = _redact_result(self._live_local(task, model, system, content, schema, max_tokens, key))
            if res.ok:
                self._store(self.path(task, key, model), res, system, content, schema)
            return res
        client = self._transport()
        if client is None:
            self.stats["error"] += 1
            return LLMResult("unavailable", task, model, key,
                             reason_pl=f"Brak dostępu do modelu (klient {BACKEND_PL.get(self.cfg.backend)} niedostępny:"
                                       f" {self._client_error}).")
        res = self._live(client, task, model, system, content, schema, max_tokens, effort, key)
        if res.stop_reason == "refusal" and allow_fallback and self.cfg.fallback_model \
                and self.cfg.fallback_model != model:
            log.info("llm %s refused on %s; retrying on %s", task, model, self.cfg.fallback_model)
            fb = self._live(client, task, self.cfg.fallback_model, system, content, schema, max_tokens, effort, key)
            if fb.ok:
                res = fb
        res = _redact_result(res)
        if res.ok:
            self._store(self.path(task, key, model), res, system, content, schema)
        return res

    def _post_local(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        _check_loopback(url)
        req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as r:  # noqa: S310 — loopback only
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                last = e
                if e.code < 500:
                    break
            except (OSError, ValueError) as e:
                last = e
            time.sleep(min(4.0, 1.0 + attempt))
        assert last is not None
        raise last

    def _live_local(self, task: str, model: str, system: str, content: list[dict[str, Any]],
                    schema: dict[str, Any] | None, max_tokens: int, key: str) -> LLMResult:
        """llama.cpp `llama-server` /v1/chat/completions. Text only (Bielik reads no images); JSON is enforced by
        the server's grammar from `response_format.json_schema`. Greedy decoding with a fixed seed."""
        text_in = "\n\n".join(b["text"] for b in content if b.get("type") == "text")
        # Greedy decoding loops on small models (measured: Bielik 1.5B hit max_tokens on both briefs); a mild
        # repetition penalty keeps it deterministic and stops the loops.
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "temperature": 0.0, "seed": 7,
                                "repeat_penalty": 1.1, "repeat_last_n": 256,
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": text_in}]}
        if schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": task, "schema": schema, "strict": True}}
        t0 = time.perf_counter()
        self.stats["live"] += 1
        try:
            out = self._post_local(self.cfg.url_for(model), body)
        except Exception as exc:  # noqa: BLE001 — server down / timeout → unavailable
            self.stats["error"] += 1
            lat = round(time.perf_counter() - t0, 3)
            self._log_call({"ts": now_iso(), "task": task, "model": model, "key": key, "cached": False,
                            "backend": "local", "status": "error", "error": type(exc).__name__, "latency_s": lat})
            return LLMResult("unavailable", task, model, key, latency_s=lat,
                             reason_pl=f"Lokalny model (llama-server, {self.cfg.url_for(model)}) nie odpowiada"
                                       f" ({type(exc).__name__}).")
        lat = round(time.perf_counter() - t0, 3)
        ch = (out.get("choices") or [{}])[0]
        text = ((ch.get("message") or {}).get("content") or "").strip()
        u, tm = out.get("usage") or {}, out.get("timings") or {}
        usage: dict[str, Any] = {"input_tokens": int(u.get("prompt_tokens") or 0),
                                 "output_tokens": int(u.get("completion_tokens") or 0)}
        for k_out, k_in in (("prompt_tps", "prompt_per_second"), ("gen_tps", "predicted_per_second")):
            if tm.get(k_in) is not None:
                usage[k_out] = round(float(tm[k_in]), 2)
        stop = ch.get("finish_reason")
        res = LLMResult("ok", task, model, key, text=text, usage=usage, latency_s=lat, cost_usd=0.0,
                        stop_reason=stop, fetched_at=now_iso())
        if stop == "length" and schema is not None:
            res.status, res.reason_pl = "unavailable", "Odpowiedź lokalnego modelu została ucięta (limit tokenów)."
        elif stop == "length":
            pass  # free text: the brief validator rejects it (length/citations) and asks for a shorter answer
        elif schema is not None:
            try:
                res.data = json.loads(text)
            except ValueError:
                res.status, res.reason_pl = "unavailable", "Lokalny model zwrócił niepoprawny JSON."
        self._log_call({"ts": res.fetched_at, "task": task, "model": model, "key": key, "cached": False,
                        "backend": "local", "status": res.status, "stop_reason": stop, "latency_s": lat,
                        "usage": usage, "cost_usd": 0.0})
        return res

    def _live(self, client: Any, task: str, model: str, system: str, content: list[dict[str, Any]],
              schema: dict[str, Any] | None, max_tokens: int, effort: str | None, key: str) -> LLMResult:
        output_config: dict[str, Any] = {}
        if effort:
            output_config["effort"] = effort
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        kw: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
        }
        if output_config:
            kw["output_config"] = output_config
        t0 = time.perf_counter()
        self.stats["live"] += 1
        try:
            msg = client.messages.create(**kw)
        except Exception as exc:  # noqa: BLE001 — any upstream failure is "unavailable", never a crash
            self.stats["error"] += 1
            lat = round(time.perf_counter() - t0, 3)
            self._log_call({"ts": now_iso(), "task": task, "model": model, "key": key, "cached": False,
                            "status": "error", "error": type(exc).__name__, "latency_s": lat})
            return LLMResult("unavailable", task, model, key, latency_s=lat,
                             reason_pl=f"Wywołanie modelu nie powiodło się ({type(exc).__name__}); spróbuj ponownie.")
        lat = round(time.perf_counter() - t0, 3)
        u = getattr(msg, "usage", None)
        usage = {k: int(getattr(u, k, 0) or 0) for k in ("input_tokens", "output_tokens",
                                                          "cache_read_input_tokens", "cache_creation_input_tokens")}
        cost = estimate_cost(model, usage)
        stop = getattr(msg, "stop_reason", None)
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text")
        res = LLMResult("ok", task, model, key, text=text, cached=False, usage=usage, latency_s=lat, cost_usd=cost,
                        stop_reason=stop, fetched_at=now_iso())
        if stop == "refusal":
            res.status, res.reason_pl = "unavailable", "Model odmówił analizy tego materiału (stop_reason=refusal)."
        elif stop == "max_tokens":
            res.status, res.reason_pl = "unavailable", "Odpowiedź modelu została ucięta (limit tokenów)."
        elif schema is not None:
            try:
                res.data = json.loads(text)
            except ValueError:
                res.status, res.reason_pl = "unavailable", "Model zwrócił niepoprawny JSON."
        self._log_call({"ts": res.fetched_at, "task": task, "model": model, "key": key, "cached": False,
                        "status": res.status, "stop_reason": stop, "latency_s": lat, "usage": usage, "cost_usd": cost})
        return res

    def _store(self, p: Path, res: LLMResult, system: str, content: list[dict[str, Any]],
               schema: dict[str, Any] | None) -> None:
        rec = {"task": res.task, "key": res.key, "model": res.model, "fetched_at": res.fetched_at,
               "status": res.status, "stop_reason": res.stop_reason, "text": res.text, "data": res.data,
               "usage": res.usage, "latency_s": res.latency_s, "cost_usd": res.cost_usd,
               "request": redact_obj({"system": system, "schema": schema, "content": _digest_content(content)})}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
            tmp.replace(p)
        except OSError:
            log.warning("llm cache write failed", exc_info=True)


_DEFAULT: LLMClient | None = None


def get_client(settings: Settings | None = None) -> LLMClient:
    """One process-wide client, rebuilt when the environment changes (tests flip HYDRA_LLM)."""
    global _DEFAULT
    cfg = LLMConfig.from_env(settings)
    if _DEFAULT is None or _DEFAULT.cfg != cfg:
        _DEFAULT = LLMClient(cfg)
    return _DEFAULT
