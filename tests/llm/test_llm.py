"""Optional LLM layer (ADR-008): cache/replay, degradation, redaction, NAR genes, grounded briefs.

All tests run offline with a fake transport; no Bedrock call is ever made."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hydra.contracts import ArtifactRole, Family, Gene, GeneStatus, Incident, IncidentArtifact
from hydra.genome.base import REGISTRY, ExtractContext, ResolvedArtifact, ResolvedIncident
from hydra.llm import brief, narrative
from hydra.llm.client import LLMClient, LLMConfig
from tests.ui_api.synthetic import RUN_ID, seed

SECRET = "EAA" + "B" * 40  # FB-token shape: must be redacted before sending


class FakeTransport:
    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(
            stop_reason=r.get("stop", "end_turn"),
            content=[SimpleNamespace(type="text", text=r["text"])],
            usage=SimpleNamespace(input_tokens=r.get("in", 100), output_tokens=r.get("out", 50),
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )


def cfg(tmp: Path, enabled: bool = True) -> LLMConfig:
    return LLMConfig(enabled=enabled, model="us.anthropic.claude-sonnet-5-5",
                     fast_model="us.anthropic.claude-haiku-5-5",
                     fallback_model="us.anthropic.claude-sonnet-5", region="us-east-1", timeout_s=5, max_retries=0,
                     cache_dir=tmp / "llm")


def call(llm: LLMClient, text: str = "hello", schema=None):
    return llm.complete(task="t", model=llm.cfg.model, system="sys", content=[{"type": "text", "text": text}],
                        inputs_digest="d", schema=schema)


# --------------------------------------------------------------------------------------- client


def test_disabled_without_cache_is_unavailable_with_polish_reason(tmp_path):
    res = call(LLMClient(cfg(tmp_path, enabled=False), transport=FakeTransport([])))
    assert res.status == "unavailable" and "HYDRA_LLM" in res.reason_pl and "pamięci podręcznej" in res.reason_pl


def test_live_call_is_cached_and_replayed_offline(tmp_path):
    t = FakeTransport([{"text": '{"a": 1}'}])
    res = call(LLMClient(cfg(tmp_path), transport=t), schema={"type": "object"})
    assert res.ok and res.data == {"a": 1} and not res.cached and res.cost_usd is not None
    assert t.calls[0]["output_config"]["format"]["type"] == "json_schema"
    # replay: disabled, transport would raise if used
    again = call(LLMClient(cfg(tmp_path, enabled=False), transport=FakeTransport([])), schema={"type": "object"})
    assert again.ok and again.cached and again.data == {"a": 1} and again.key == res.key
    assert (tmp_path / "llm" / "calls.jsonl").is_file()


def test_text_is_redacted_and_image_bytes_not_cached(tmp_path):
    t = FakeTransport([{"text": "ok"}])
    llm = LLMClient(cfg(tmp_path), transport=t)
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD" * 50}}
    res = llm.complete(task="t", model=llm.cfg.model, system="s", inputs_digest="d",
                       content=[img, {"type": "text", "text": f"link ?access_token={SECRET} i {SECRET}"}])
    sent = json.dumps(t.calls[0]["messages"])
    assert SECRET not in sent and "[REDACTED]" in sent
    stored = (tmp_path / "llm" / "bedrock" / "t" / f"{res.key}.json").read_text()
    assert "QUJDQUJD" not in stored and "sha256" in stored and SECRET not in stored


def test_upstream_error_is_unavailable_and_not_cached(tmp_path):
    llm = LLMClient(cfg(tmp_path), transport=FakeTransport([RuntimeError("boom")]))
    res = call(llm)
    assert res.status == "unavailable" and "RuntimeError" in res.reason_pl
    assert not list((tmp_path / "llm").glob("**/t/*.json"))


def test_refusal_falls_back_once(tmp_path):
    t = FakeTransport([{"text": "", "stop": "refusal"}, {"text": "fallback answer"}])
    res = call(LLMClient(cfg(tmp_path), transport=t))
    assert res.ok and res.text == "fallback answer" and res.model.endswith("claude-sonnet-5")
    assert [c["model"] for c in t.calls] == ["us.anthropic.claude-sonnet-5-5", "us.anthropic.claude-sonnet-5"]


def test_no_credentials_is_unavailable(tmp_path, monkeypatch):
    llm = LLMClient(cfg(tmp_path))
    llm._client_error = "NoCredentialsError: missing"
    res = call(llm)
    assert res.status == "unavailable" and "Bedrock" in res.reason_pl


# --------------------------------------------------------------------------------------- narrative


LLM_JSON = {"scheme": "investment", "impersonated_brands": ["TVN24", " tvn24 ", "Bank Pekao"],
            "named_persons": ["Łukasz Nowak"], "product": "Quantum AI", "promise": "„Zarobek bez wysiłku!”",
            "promise_amount": "4000 zł tygodniowo", "cta": "Zarejestruj się", "urgency": ["Tylko dziś"],
            "language_tricks": ["fałszywy autorytet"], "target_audience": "emeryci", "confidence": 0.9}


def test_extractor_registered_as_nar_and_not_in_default_modules():
    ex = REGISTRY["llm_narrative"]
    assert ex.family == Family.NAR and ex.tag == "llm_narrative@1"
    import inspect

    from hydra.genome import base
    assert "llm" not in inspect.getsource(base.load_family_modules)


def test_genes_from_json_normalised():
    res = SimpleNamespace(model="m", key="k" * 64)
    genes = narrative.genes_from_json("inc-1", LLM_JSON, res, "art")
    by = {(g.kind, g.value) for g in genes}
    assert ("llm_scheme", "investment") in by and ("llm_brand", "tvn24") in by and ("llm_brand", "bank pekao") in by
    assert ("llm_person", "łukasz nowak") in by  # lowercase, diacritics kept
    assert ("llm_promise", "zarobek bez wysiłku") in by and ("llm_promise_amount", "4000 zł tygodniowo") in by
    assert sum(k == "llm_brand" for k, _ in by) == 2
    assert all(g.family == "NAR" and g.provenance == "INF" and g.dependency_group == "nar:llm" for g in genes)
    assert all(g.kind.startswith("llm_") for g in genes)  # never the rule `scheme` kind (contradiction check)


def test_llm_genes_never_make_campaign_bits():
    from hydra.pipeline import rarity_for
    from hydra.synapse import build_genome, level, score_pair

    res = SimpleNamespace(model="m", key="k" * 64)
    ga = narrative.genes_from_json("a", LLM_JSON, res, None)
    gb = narrative.genes_from_json("b", LLM_JSON, res, None)
    assert {level(g) for g in ga} == {"kit"}
    genomes = {"a": build_genome("a", ga, {}), "b": build_genome("b", gb, {})}
    for i in range(6):  # background so the shared values are rare
        genomes[f"x{i}"] = build_genome(f"x{i}", narrative.genes_from_json(
            f"x{i}", {**LLM_JSON, "scheme": "gambling", "impersonated_brands": [f"b{i}"], "named_persons": [],
                      "product": f"p{i}", "promise": "", "promise_amount": "", "cta": "graj"}, res, None), {})
    pe = score_pair(genomes["a"], genomes["b"], rarity_for(genomes))
    assert pe.campaign_bits == 0.0 and pe.score.hypothesis != "same_campaign"


def _incident(tmp_path: Path, make_png) -> ResolvedIncident:
    p = tmp_path / "ad.png"
    p.write_bytes(make_png(size=(2000, 1000)))
    art = IncidentArtifact(artifact_id="a" * 64, role=ArtifactRole.AD_CREATIVE, path_in_incident="ad.png",
                           mime="image/png", size=p.stat().st_size)
    return ResolvedIncident(Incident(id="inc-1", source="test", source_ref="x", artifacts=[art]),
                            [ResolvedArtifact(art, p)])


def test_analyse_sends_downscaled_image_and_emits_genes(tmp_path, settings, make_png, monkeypatch):
    monkeypatch.setattr(narrative, "visible_texts", lambda inc, ctx: (f"Reklama {SECRET}", "Strona"))
    t = FakeTransport([{"text": json.dumps(LLM_JSON)}])
    llm = LLMClient(cfg(tmp_path), transport=t)
    ctx = ExtractContext(settings=settings, work_dir=tmp_path)
    res, r = narrative.analyse(_incident(tmp_path, make_png), ctx, llm)
    assert r is not None and r.ok and t.calls[0]["model"].endswith("haiku-5-5")
    blocks = t.calls[0]["messages"][0]["content"]
    import base64
    import io

    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(blocks[0]["source"]["data"])))
    assert max(im.size) == narrative.IMAGE_MAX_SIDE
    assert SECRET not in blocks[-1]["text"]
    assert any(g.kind == "llm_scheme" and g.evidence.artifact_id == "a" * 64 for g in res.genes)
    # replay offline gives the same genes
    llm2 = LLMClient(cfg(tmp_path, enabled=False), transport=FakeTransport([]))
    res2, r2 = narrative.analyse(_incident(tmp_path, make_png), ctx, llm2)
    assert r2.cached and [g.value for g in res2.genes] == [g.value for g in res.genes]


def test_analyse_disabled_gives_unavailable_gene(tmp_path, settings, make_png, monkeypatch):
    monkeypatch.setattr(narrative, "visible_texts", lambda inc, ctx: ("Reklama", ""))
    llm = LLMClient(cfg(tmp_path, enabled=False), transport=FakeTransport([]))
    res, _ = narrative.analyse(_incident(tmp_path, make_png), ExtractContext(settings=settings, work_dir=tmp_path),
                               llm)
    assert len(res.genes) == 1 and res.genes[0].status == GeneStatus.UNAVAILABLE
    assert "HYDRA_LLM" in res.genes[0].evidence.note


def test_annotate_appends_only_llm_genes(tmp_path, settings, monkeypatch):
    seed(settings.db_path, settings.cas_dir, with_eval=False)
    from hydra.db import Writer, connect
    from hydra.storage import ContentStore

    monkeypatch.setattr(narrative, "visible_texts", lambda inc, ctx: ("Reklama", ""))
    t = FakeTransport([{"text": json.dumps(LLM_JSON)}] * 2)
    llm = LLMClient(cfg(tmp_path), transport=t)
    w = Writer(settings.db_path)
    try:
        with connect(settings.db_path, readonly=True) as c:
            before = c.execute("SELECT count(*) FROM genes WHERE incident_id='syn-1'").fetchone()[0]
        for _ in range(2):  # idempotent: second run replaces, does not duplicate
            out = narrative.annotate(["syn-1"], settings, w, ContentStore(settings.cas_dir), llm)
        with connect(settings.db_path, readonly=True) as c:
            n_llm = c.execute("SELECT count(*) FROM genes WHERE incident_id='syn-1' AND extractor='llm_narrative@1'"
                              ).fetchone()[0]
            after = c.execute("SELECT count(*) FROM genes WHERE incident_id='syn-1'").fetchone()[0]
    finally:
        w.close()
    assert out[0]["status"] == "ok" and out[0]["cached"] is True and len(t.calls) == 1
    assert n_llm == out[0]["genes"] and after == before + n_llm


# --------------------------------------------------------------------------------------- briefs


FACTS = ["Kampania C1: 3 incydenty (nask-1, nask-2, nask-3).", "Wspólna cecha (INF): ten sam piksel [+12.5 bit]"]


def _words(n: int) -> str:
    return " ".join(["słowo"] * n)


def test_validate_rules():
    ok = f"Kampania obejmuje 3 incydenty [1]. Łączy je piksel o wadze 12,5 bit [2]. {_words(120)}"
    assert brief.validate(ok, FACTS, (120, 200)) == []
    errs = brief.validate(_words(130), FACTS, (120, 200))
    assert any("nie cytuje" in e for e in errs)
    errs = brief.validate(f"Kampania ma 7 incydentów [1][9]. {_words(130)}", FACTS, (120, 200))
    assert any("nieistniejące" in e for e in errs) and any("liczby spoza" in e and "7" in e for e in errs)
    assert any("długość" in e for e in brief.validate("Krótko [1].", FACTS, (120, 200)))
    assert brief.citations("a [1, 3] b [2][4]") == [1, 3, 2, 4]
    dump = "Kampania ma trzy incydenty. Łączy je wspólny piksel. Ma też wspólny host strony. " * 15 + "[1], [2]"
    assert any("za mało zdań" in e for e in brief.validate(dump, FACTS, (120, 200)))


def _cfg_for(settings, enabled=True) -> LLMConfig:
    return replace(cfg(Path(settings.cache_dir), enabled), cache_dir=Path(settings.cache_dir) / "llm")


def test_brief_grounded_regenerates_and_stores(tmp_path, settings):
    seed(settings.db_path, settings.cas_dir, with_eval=False)
    card = brief.campaign_card(settings, RUN_ID, "syn-c1")
    good = (f"Kampania syn-c1 obejmuje 3 incydenty [1]. Łączy je ten sam identyfikator piksela [5, 2]."
            f" {_words(120)}")
    t = FakeTransport([{"text": f"Kampania ma 99 incydentów. {_words(130)}"}, {"text": good}])
    rec = brief.brief_for_campaign(card, settings=settings, client=LLMClient(_cfg_for(settings), transport=t))
    assert rec["status"] == "ok" and len(rec["attempts"]) == 2 and "[5][2]" in rec["text_pl"]
    assert rec["attempts"][0]["errors"] and "odrzucona" in t.calls[1]["messages"][0]["content"][0]["text"]
    facts_text = t.calls[0]["messages"][0]["content"][0]["text"]
    assert "[1] Kampania syn-c1" in facts_text and "Ten sam identyfikator piksela" in facts_text
    assert "[2] Incydent syn-1: tytuł „Fałszywy artykuł" in facts_text
    cites = {c["n"]: c for c in rec["citations"]}
    assert cites[5]["section"] == "core_genome" and cites[5]["index"] == 0 and cites[1]["section"] == "campaign"
    assert cites[2] == {"n": 2, "section": "member_incidents", "index": 0, "label": cites[2]["label"]}
    assert any(f["section"] == "internal_pairs" for f in rec["facts"])
    assert brief.stored(RUN_ID, settings)["briefs"]["syn-c1"]["text_pl"] == rec["text_pl"]
    # replay offline: no transport calls, same text
    offline = LLMClient(_cfg_for(settings, False), transport=FakeTransport([]))
    rec2 = brief.brief_for_campaign(card, settings=settings, client=offline)
    assert rec2["status"] == "ok" and rec2["text_pl"] == rec["text_pl"] and rec2["cached"]


def test_api_endpoint_replays_cached_brief(tmp_path, settings, monkeypatch):
    from fastapi.testclient import TestClient

    from hydra.api import create_app

    seed(settings.db_path, settings.cas_dir, with_eval=False)
    monkeypatch.setenv("HYDRA_LLM_BACKEND", "bedrock")  # the fake transport below is a Bedrock-shaped client
    good = f"Kampania syn-c2 obejmuje 2 incydenty [1]. {_words(125)}"
    with TestClient(create_app(settings)) as c:
        monkeypatch.delenv("HYDRA_LLM", raising=False)
        off = c.get("/v1/campaigns/syn-c2/brief").json()
        assert off["status"] == "unavailable" and off["reason_pl"]
        card = c.get("/v1/campaigns/syn-c2").json()
        live = LLMClient(_cfg_for(settings), transport=FakeTransport([{"text": good}]))
        assert brief.brief_for_campaign(card, settings=settings, client=live)["status"] == "ok"
        monkeypatch.setenv("HYDRA_LLM", "replay")  # keyless verification: cache only
        got = c.get("/v1/campaigns/syn-c2/brief").json()
    assert got["status"] == "ok" and got["text_pl"] == good and got["citations"][0]["section"] == "campaign"
    assert got["model"].endswith("sonnet-5-5") and got["generated_at"]


def test_brief_unavailable_keeps_facts(tmp_path, settings):
    seed(settings.db_path, settings.cas_dir, with_eval=False)
    card = brief.campaign_card(settings, RUN_ID, "syn-c2")
    rec = brief.brief_for_campaign(card, settings=settings,
                                   client=LLMClient(_cfg_for(settings, False), transport=FakeTransport([])))
    assert rec["status"] == "unavailable" and rec["facts"] and rec["reason_pl"]
    empty = brief.brief_for_campaign({}, settings=settings, client=LLMClient(_cfg_for(settings, False)))
    assert empty["status"] == "unavailable" and empty["reason_pl"]


def test_public_guard_turns_exceptions_into_error(monkeypatch, settings):
    import hydra.llm as llm

    monkeypatch.setattr(brief, "brief_for_campaign", lambda *a, **k: 1 / 0)
    assert llm.brief_for_campaign({}, settings=settings)["status"] == "error"
    monkeypatch.delenv("HYDRA_LLM", raising=False)
    assert not llm.is_enabled(settings)
    monkeypatch.setenv("HYDRA_LLM", "replay")
    assert llm.is_enabled(settings) and not LLMConfig.from_env(settings).enabled


def test_explain_pair_facts(tmp_path, settings):
    seed(settings.db_path, settings.cas_dir, with_eval=False)
    body = brief.pair_body(settings, RUN_ID, "syn-2", "syn-1")
    t = FakeTransport([{"text": f"Para ma sumę +26.7 bit [3]. Powód [5]. {_words(70)}"}])
    rec = brief.explain_pair(body, settings=settings, client=LLMClient(_cfg_for(settings), transport=t))
    assert rec["a"] == "syn-1" and rec["status"] == "ok", rec
    assert any("Hipoteza systemu: same_campaign" in f["text"] for f in rec["facts"])
    assert {c["section"] for c in rec["citations"]} == {"score", "reasons"}
    assert next(c for c in rec["citations"] if c["section"] == "reasons")["index"] == 0
    assert "syn-1|syn-2" in brief.stored(RUN_ID, settings)["pairs"]


def test_config_from_env(monkeypatch, settings):
    monkeypatch.setenv("HYDRA_LLM", "1")
    monkeypatch.setenv("HYDRA_LLM_MODEL", "m1")
    monkeypatch.delenv("HYDRA_LLM_FAST_MODEL", raising=False)
    monkeypatch.setenv("HYDRA_LLM_BACKEND", "bedrock")
    settings = settings.model_copy(update={"offline": False})
    c = LLMConfig.from_env(settings)
    assert c.enabled and c.model == "m1" and c.fast_model.endswith("haiku-5-5")
    assert c.cache_dir == Path(settings.cache_dir) / "llm"
    monkeypatch.setenv("HYDRA_LLM", "0")
    assert not replace(c, enabled=LLMConfig.from_env(settings).enabled).enabled


def test_gene_model_roundtrip():
    g = narrative.status_gene("i", GeneStatus.UNAVAILABLE, "powód")
    assert Gene.model_validate(g.model_dump()).extractor == "llm_narrative@1"


@pytest.mark.parametrize("v,exp", [("  „Tylko DZIŚ!” ", "tylko dziś"), ("Żółć", "żółć"), (None, "")])
def test_norm(v, exp):
    assert narrative.norm(v) == exp


# --------------------------------------------------------------------------------------- local backend (mock server)


class _MockOpenAI:
    """OpenAI-compatible mock of llama-server on 127.0.0.1 (GET /v1/models, POST /v1/chat/completions)."""

    def __init__(self, replies: list[str], model: str = "bielik-test-q8_0"):
        import http.server
        import threading

        self.replies, self.requests, mock = list(replies), [], self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send({"object": "list", "data": [{"id": model}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                mock.requests.append(body)
                self._send({"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
                                                                               "content": mock.replies.pop(0)}}],
                            "usage": {"prompt_tokens": 321, "completion_tokens": 54},
                            "timings": {"prompt_per_second": 400.5, "predicted_per_second": 31.25}})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def local_env(monkeypatch, settings):
    def make(replies):
        m = _MockOpenAI(replies)
        monkeypatch.setenv("HYDRA_LLM", "1")
        monkeypatch.setenv("HYDRA_LLM_BACKEND", "local")
        monkeypatch.setenv("HYDRA_LLM_LOCAL_URL", m.url)
        for k in ("HYDRA_LLM_LOCAL_MODEL", "HYDRA_LLM_LOCAL_FAST_URL", "HYDRA_LLM_LOCAL_FAST_MODEL", "HYDRA_LLM_CACHE"):
            monkeypatch.delenv(k, raising=False)
        return m, LLMClient(LLMConfig.from_env(settings))
    servers = []
    yield lambda replies: servers.append(make(replies)) or servers[-1]
    for m, _ in servers:
        m.close()


def test_local_backend_json_schema_cache_namespace(local_env, settings):
    m, llm = local_env(['{"a": 1}'])
    assert llm.cfg.is_local and llm.cfg.model == "bielik-test-q8_0"  # id read from GET /v1/models
    res = llm.complete(task="t", model=llm.cfg.model, system="sys", content=[{"type": "text", "text": f"x {SECRET}"}],
                       inputs_digest="d", schema={"type": "object"})
    assert res.ok and res.data == {"a": 1} and res.cost_usd == 0.0
    assert res.usage["gen_tps"] == 31.25 and res.usage["input_tokens"] == 321
    req = m.requests[0]
    assert req["response_format"]["type"] == "json_schema" and req["temperature"] == 0.0
    assert req["messages"][0] == {"role": "system", "content": "sys"} and SECRET not in json.dumps(req)
    p = Path(settings.cache_dir) / "llm" / "local-bielik-test-q8_0" / "t" / f"{res.key}.json"
    assert p.is_file()
    again = llm.complete(task="t", model=llm.cfg.model, system="sys", content=[{"type": "text", "text": f"x {SECRET}"}],
                         inputs_digest="d", schema={"type": "object"})
    assert again.cached and len(m.requests) == 1


def test_local_narrative_is_text_only(local_env, settings, tmp_path, make_png, monkeypatch):
    monkeypatch.setattr(narrative, "visible_texts", lambda inc, ctx: ("Zarabiaj z AI", "Platforma inwestycyjna"))
    m, llm = local_env([json.dumps(LLM_JSON)])
    res, r = narrative.analyse(_incident(tmp_path, make_png), ExtractContext(settings=settings, work_dir=tmp_path),
                               llm)
    assert r.ok and any(g.kind == "llm_scheme" and g.value == "investment" for g in res.genes)
    req = m.requests[0]
    assert "image" not in json.dumps(req["messages"]) and "Zarabiaj z AI" in req["messages"][1]["content"]
    sch = req["response_format"]["json_schema"]["schema"]
    assert sch["properties"]["named_persons"]["maxItems"] == 5 and req["max_tokens"] == 1000


def test_local_brief_uses_sentence_grammar(local_env, settings):
    seed(settings.db_path, settings.cas_dir, with_eval=False)
    sent = [{"text": "Kampania syn-c1 obejmuje 3 incydenty o wspólnym celu i podobnym przekazie reklamowym "
                     + " ".join(["słowo"] * 18) + ".", "facts": [1, 5]} for _ in range(6)]
    m, llm = local_env([json.dumps({"sentences": sent})])
    card = brief.campaign_card(settings, RUN_ID, "syn-c1")
    rec = brief.brief_for_campaign(card, settings=settings, client=llm)
    assert rec["status"] == "ok", rec.get("reason_pl")
    assert rec["text_pl"].count("[1][5].") == 6 and rec["backend"] == "local"
    req = m.requests[0]
    assert "WYŁĄCZNIE" in req["messages"][0]["content"]
    sch = req["response_format"]["json_schema"]["schema"]["properties"]["sentences"]
    assert sch["items"]["properties"]["facts"]["items"]["enum"][-1] == len(rec["facts"])
    assert rec["usage"]["cost_usd_estimated"] == 0.0
    assert "briefs@local-bielik-test-q8_0" in brief.stored(RUN_ID, settings)


def test_render_sentences():
    out = brief.render_sentences({"sentences": [{"text": "Zdanie pierwsze [9].", "facts": [2, 2, 3]},
                                                {"text": "  ", "facts": [1]}]})
    assert out == "Zdanie pierwsze [2][3]."


def test_local_server_down_and_non_loopback(monkeypatch, settings):
    monkeypatch.setenv("HYDRA_LLM", "1")
    monkeypatch.setenv("HYDRA_LLM_BACKEND", "local")
    monkeypatch.setenv("HYDRA_LLM_LOCAL_URL", "http://127.0.0.1:9/v1")  # nothing listens
    monkeypatch.setenv("HYDRA_LLM_LOCAL_MODEL", "bielik-x")
    monkeypatch.setenv("HYDRA_LLM_MAX_RETRIES", "0")
    llm = LLMClient(LLMConfig.from_env(settings))
    res = call(llm)
    assert res.status == "unavailable" and "Lokalny model" in res.reason_pl
    monkeypatch.setenv("HYDRA_LLM_LOCAL_URL", "http://10.0.0.5:8090/v1")
    res = call(LLMClient(LLMConfig.from_env(settings)))
    assert res.status == "unavailable"  # non-loopback URL refused before any connection


def test_offline_kill_switch_degrades_bedrock_to_replay(monkeypatch, settings, tmp_path):
    monkeypatch.setenv("HYDRA_LLM", "1")
    monkeypatch.setenv("HYDRA_LLM_BACKEND", "bedrock")
    monkeypatch.setenv("HYDRA_LLM_CACHE", str(tmp_path / "llm"))
    off = settings.model_copy(update={"offline": True})
    c = LLMConfig.from_env(off)
    assert not c.enabled and c.offline_blocked
    t = FakeTransport([{"text": "should not be called"}])
    res = call(LLMClient(c, transport=t))
    assert res.status == "unavailable" and "HYDRA_OFFLINE=1" in res.reason_pl and not t.calls
    # a cached answer is still served (replay)
    live = LLMClient(replace(c, enabled=True, offline_blocked=False), transport=FakeTransport([{"text": "ok"}]))
    assert call(live).ok
    assert call(LLMClient(c, transport=t)).cached
    on = settings.model_copy(update={"offline": False})
    assert LLMConfig.from_env(on).enabled


def test_offline_allows_local_loopback_backend(local_env, settings):
    assert settings.offline  # the fixture default: hard kill switch on
    m, llm = local_env(["tekst"])
    assert llm.cfg.enabled and not llm.cfg.offline_blocked
    assert call(llm).ok and len(m.requests) == 1


def test_default_backend_is_local(monkeypatch, settings):
    for k in ("HYDRA_LLM_BACKEND", "HYDRA_LLM_LOCAL_URL", "HYDRA_LLM_LOCAL_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HYDRA_LLM_LOCAL_URL", "http://127.0.0.1:9/v1")  # nothing listens
    c = LLMConfig.from_env(settings)
    assert c.backend == "local" and c.model == "unknown@127.0.0.1:9/v1"  # never another model's cache label
    monkeypatch.setenv("HYDRA_LLM_LOCAL_MODEL", "bielik-4.5b-v3.0-instruct-q8_0")  # replay without a server
    assert LLMConfig.from_env(settings).model == "bielik-4.5b-v3.0-instruct-q8_0"


def test_anthropic_backend_opt_in(monkeypatch, settings, tmp_path):
    from hydra.llm import client as cl

    fake = "sk-ant-test-" + "x" * 20  # never a real key
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake)
    monkeypatch.setenv("HYDRA_LLM", "1")
    monkeypatch.setenv("HYDRA_LLM_BACKEND", "anthropic")
    monkeypatch.setenv("HYDRA_LLM_CACHE", str(tmp_path / "llm"))
    c = LLMConfig.from_env(settings.model_copy(update={"offline": False}))
    assert c.backend == "anthropic" and c.model == "claude-sonnet-5-5" and c.fast_model == "claude-haiku-5-5"
    assert cl._api_key() == fake and fake not in cl.scrub(f"error {fake}")
    t = FakeTransport([{"text": "ok"}])
    res = call(LLMClient(c, transport=t))
    assert res.ok and t.calls[0]["model"] == "claude-sonnet-5-5"
    assert (tmp_path / "llm" / "anthropic" / "t" / f"{res.key}.json").is_file()
    assert fake not in (tmp_path / "llm" / "calls.jsonl").read_text()
    real = LLMClient(c)._transport()  # SDK client built without network
    assert real is not None and type(real).__name__ == "Anthropic"
    off = LLMConfig.from_env(settings)  # HYDRA_OFFLINE=1: paid backend degrades to replay
    assert off.offline_blocked and not off.enabled
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    nokey = LLMClient(c)
    assert call(nokey, "other").status == "unavailable" and "ANTHROPIC_API_KEY" in nokey._client_error


def test_anthropic_never_default(monkeypatch, settings):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-" + "y" * 20)
    monkeypatch.delenv("HYDRA_LLM_BACKEND", raising=False)
    monkeypatch.setenv("HYDRA_LLM_LOCAL_URL", "http://127.0.0.1:9/v1")
    assert LLMConfig.from_env(settings).backend == "local"
