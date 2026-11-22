"""HYDRA_OFFLINE=1 (Settings.offline) is the hard network kill switch (audit A09).

Every public entry point that can reach the network is called with enrichment *requested* (enrich=True) but the
kill switch on (offline=True). Expected: no socket/DNS attempt (tests/conftest.py guard), no spawned dig/whois/
curl/yt-dlp, a clean unavailable/disabled result and no exception. Synthetic inputs only (RFC 2606/5737 names).
"""

from __future__ import annotations

import subprocess
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from hydra.config import Settings

NET_TOOLS = {"dig", "whois", "curl", "wget", "yt-dlp", "nslookup", "host"}


@pytest.fixture
def off(tmp_path: Path) -> Settings:
    s = Settings(root=tmp_path, cache_dir=tmp_path / "cache", state_dir=tmp_path / "state",
                 models_dir=tmp_path / "models", data_dir=tmp_path / "data", offline=True, enrich=True)
    s.ensure_dirs()
    return s


@pytest.fixture(autouse=True)
def trap(monkeypatch, tmp_path: Path):
    """Fail loudly on any egress primitive, even when a module would swallow the error."""
    calls: list[str] = []
    for k in ("HYDRA_MODEL_DOWNLOAD", "HYDRA_WATCH_POLL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HYDRA_OFFLINE", "1")
    monkeypatch.setenv("HYDRA_ENRICH", "1")
    monkeypatch.setenv("HYDRA_ENRICH_CACHE", str(tmp_path / "envcache"))

    def no_urlopen(req, *a, **k):
        calls.append(f"urlopen {getattr(req, 'full_url', req)}")
        raise AssertionError("urlopen while offline")

    real_run = subprocess.run

    def guarded_run(cmd, *a, **k):
        exe = Path(str(cmd[0] if isinstance(cmd, (list, tuple)) else str(cmd).split()[0])).name
        if exe in NET_TOOLS:
            calls.append(f"subprocess {exe}")
            raise AssertionError(f"{exe} spawned while offline")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(urllib.request, "urlopen", no_urlopen)
    monkeypatch.setattr(subprocess, "run", guarded_run)
    # Force the dig branch in hydra.enrich.dns, so a missing gate would spawn it.
    monkeypatch.setattr("hydra.enrich.dns.shutil.which", lambda name: f"/usr/bin/{name}")
    yield calls
    assert not calls, f"egress attempted while offline: {calls}"


def test_default_is_offline(monkeypatch, tmp_path):
    from hydra.enrich.client import live_enabled

    for k in ("HYDRA_OFFLINE", "HYDRA_ENRICH"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(root=tmp_path)
    assert s.offline is True and s.enrich is False and not live_enabled(s)
    assert not live_enabled(Settings(root=tmp_path, enrich=True))  # enrich alone is not enough
    assert live_enabled(Settings(root=tmp_path, enrich=True, offline=False))


def test_enricher_lookups_replay_only(off):
    from hydra.enrich import asn, crtsh, dns, get_enricher, rdap
    from hydra.enrich.client import Enricher

    enr = Enricher(settings=off)
    assert enr.live is False
    assert get_enricher(None, off).live is False
    for r, _ in (rdap.domain(enr, "example.org"), rdap.ip(enr, "192.0.2.10"), crtsh.lookup(enr, "example.org"),
                 asn.lookup(enr, "192.0.2.10")):
        assert r.status == "unavailable"
    for t in dns.TYPES:
        assert dns.query(enr, "example.org", t).status == "unavailable"
    assert asn.is_edge_ip(enr, "198.51.100.7") is False
    assert enr.stats["live"] == 0 and not any(enr.cache_dir.rglob("*.json"))


def test_dns_getaddrinfo_fallback_not_used(off, monkeypatch):
    from hydra.enrich import dns
    from hydra.enrich.client import Enricher

    monkeypatch.setattr("hydra.enrich.dns.shutil.which", lambda name: None)  # dig missing -> getaddrinfo path
    r = dns.query(Enricher(settings=off), "example.net", "A")
    assert r.status == "unavailable" and dns.answers(r) == []


def test_fetch_url_disabled(off):
    from hydra.enrich.fetch import fetch_url

    res = fetch_url("https://example.com/landing?sub1=x", off)
    assert res.status == "disabled" and not res.ok and res.hops == []
    assert res.record().status == "unavailable"


def test_adlibrary_search_offline(off, monkeypatch):
    from hydra.enrich import adlibrary

    monkeypatch.setenv("HYDRA_META_TOKEN", "EAAsynthetic")
    rules = adlibrary.Rules(search_terms=["inwestycja"], max_domain_age_days=None)
    res = adlibrary.search(rules, settings=off)  # default urllib transport: the trap fails if it is used
    assert res.status == "unavailable" and res.message_pl == adlibrary.MESSAGE_OFFLINE_PL and res.ads == []
    # The domain-age rule builds its own Enricher from env (HYDRA_OFFLINE=1): replay only.
    ad = adlibrary.AdRecord(ad_id="1", source="manual_json", link_urls=["https://promo.example.com/x"])
    assert not any(r.startswith("new_domain") for r in adlibrary.evaluate(ad, adlibrary.Rules(), enricher=None))


def test_scheduler_jobs_offline(off, monkeypatch):
    from hydra import scheduler

    monkeypatch.setenv("HYDRA_META_TOKEN", "EAAsynthetic")
    scheduler.watch_add(off, keywords=["inwestycja"], domains=["example.org"])
    wl = scheduler.load_watchlist(off)
    r = scheduler.job_adlibrary_pull(off, None, wl)
    # merge-batch3: the enrich-plus pull replays its cache offline instead of skipping; with an empty cache that is
    # `unavailable` in replay mode, still without any network call (the urllib trap of `off` would fail the test)
    assert r["status"] == "unavailable" and r["mode"] == "replay" and r["new"] == 0 and "HYDRA_OFFLINE" in r["note"]
    assert scheduler.job_reenrich_domains(off, None, wl)["status"] == "skipped"


def test_watch_recorders_offline(tmp_path):
    import asyncio

    from hydra.watch import ct_recorder, recorder

    assert recorder.offline()
    assert recorder._request("https://hole.cert.pl/domains/v2/actions_2026.log", head=True) == (0, {}, b"")
    assert recorder.CertTail(tmp_path).poll() == 0 and not list(tmp_path.glob("*.jsonl"))
    # Non-loopback certstream URL: returns at once instead of reconnecting forever.
    asyncio.run(asyncio.wait_for(ct_recorder.run("wss://certstream.example.com/", tmp_path, 6.0), timeout=2))


def test_model_downloads_off(off):
    from hydra.genome import aud, vid_avsync

    ctx = SimpleNamespace(settings=off, options={}, models={})
    assert vid_avsync.landmarker_path(ctx) is None  # HYDRA_MODEL_DOWNLOAD unset: no fetch_landmarker
    assert aud._asr_model_dir(ctx, "small") is None  # no faster_whisper.download_model


def test_hf_loaders_local_only(off, monkeypatch):
    from hydra.ml import models

    monkeypatch.setattr(models, "get_settings", lambda: off)
    with pytest.raises(models.ModelUnavailable):
        models.model_dir("dinov2-small")  # missing weights -> ModelUnavailable, never a hub download


@pytest.mark.parametrize("backend", ["bedrock", "anthropic"])
def test_llm_cloud_blocked(off, monkeypatch, tmp_path, backend):
    import anthropic

    from hydra.llm.client import LLMClient, LLMConfig

    def boom(*a, **k):
        raise AssertionError("cloud LLM client constructed while offline")

    monkeypatch.setattr(anthropic, "AnthropicBedrock", boom)
    monkeypatch.setattr(anthropic, "Anthropic", boom)
    monkeypatch.setenv("HYDRA_LLM", "1")
    monkeypatch.setenv("HYDRA_LLM_BACKEND", backend)
    monkeypatch.setenv("HYDRA_LLM_CACHE", str(tmp_path / "llm"))
    cfg = LLMConfig.from_env(off)
    assert cfg.offline_blocked and not cfg.enabled
    res = LLMClient(cfg).complete(task="brief", model=cfg.model, system="s",
                                  content=[{"type": "text", "text": "synthetic"}], inputs_digest="d")
    assert res.status == "unavailable" and "HYDRA_OFFLINE" in (res.reason_pl or "")


def test_llm_local_rejects_non_loopback(off, monkeypatch, tmp_path):
    from hydra.llm.client import LLMClient, LLMConfig

    for k, v in {"HYDRA_LLM": "1", "HYDRA_LLM_BACKEND": "local", "HYDRA_LLM_LOCAL_URL": "http://192.0.2.1:8090/v1",
                 "HYDRA_LLM_LOCAL_MODEL": "synthetic-model", "HYDRA_LLM_MAX_RETRIES": "0",
                 "HYDRA_LLM_CACHE": str(tmp_path / "llm")}.items():
        monkeypatch.setenv(k, v)
    cfg = LLMConfig.from_env(off)
    monkeypatch.setattr("hydra.llm.client.time.sleep", lambda s: None)
    res = LLMClient(cfg).complete(task="brief", model=cfg.model, system="s",
                                  content=[{"type": "text", "text": "synthetic"}], inputs_digest="d")
    assert res.status == "unavailable"


def test_ffmpeg_input_is_local_only():
    from hydra import media

    assert media.LOCAL_ONLY == ("-protocol_whitelist", "file,pipe")
