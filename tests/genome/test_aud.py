"""Audio genes on synthetic audio generated here (harmonic 'voice' sweeps, chirps, noise; no organiser data)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Incident, IncidentArtifact
from hydra.entropy import build_rarity
from hydra.genome import aud
from hydra.genome.base import ExtractContext, ResolvedArtifact, ResolvedIncident
from hydra.synapse import AUDIO_REUSE_CONTAINMENT, build_genome, score_pair

SR = aud.SR


def track(seed: int, seconds: float = 20.0) -> np.ndarray:
    """Synthetic 'voice-over + music': harmonic stack with a random-walk pitch, syllable-like amplitude
    envelope, a few chirps and a melody of sine notes. Distinct per seed."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    f0 = 120 + 60 * np.cumsum(rng.normal(0, 0.002, n)).clip(-1, 1) + 30 * np.sin(2 * np.pi * rng.uniform(0.1, 0.4) * t)
    phase = 2 * np.pi * np.cumsum(f0) / SR
    env = (0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(3, 5) * t + rng.uniform(0, 6))) ** 2
    voice = sum((1.0 / h) * np.sin(h * phase) for h in range(1, 12)) * env
    notes = rng.uniform(300, 2500, int(seconds * 2))
    melody = np.sin(2 * np.pi * np.repeat(notes, n // len(notes) + 1)[:n] * t) * 0.3
    chirp = np.zeros(n)
    for _ in range(int(seconds)):
        s = int(rng.integers(0, n - SR // 2))
        tt = np.arange(SR // 2) / SR
        f1, f2 = rng.uniform(400, 3500, 2)
        chirp[s:s + SR // 2] += 0.4 * np.sin(2 * np.pi * (f1 * tt + (f2 - f1) * tt ** 2))
    x = voice + melody + chirp
    return (x / np.abs(x).max() * 0.8).astype(np.float32)


def degrade(x: np.ndarray, seed: int, snr_db: float = 20.0, gain: float = 0.5) -> np.ndarray:
    rng = np.random.default_rng(seed)
    p = float(np.mean(x ** 2))
    noise = rng.normal(0, np.sqrt(p / 10 ** (snr_db / 10)), x.size)
    return (gain * (x + noise)).astype(np.float32)


def sig_of(x: np.ndarray) -> tuple[int, np.ndarray]:
    u = aud.fingerprint(x).unique
    return int(u.size), aud.minhash(u)


def contain(x: np.ndarray, y: np.ndarray) -> float:
    (na, sa), (nb, sb) = sig_of(x), sig_of(y)
    return aud.containment(aud.jaccard(sa, sb), na, nb)


def test_fingerprint_survives_noise_gain_and_crop_but_not_other_tracks():
    a = track(1)
    same_noisy = degrade(a, 7)
    cropped = degrade(a[int(5.3 * SR):int(14.1 * SR)], 8, snr_db=25)  # 9 s cut at a non-frame offset
    others = [track(s) for s in (2, 3, 4)]
    assert aud.fingerprint(a).unique.size >= aud.MIN_HASHES
    assert contain(a, same_noisy) >= 0.4
    assert contain(a, cropped) >= AUDIO_REUSE_CONTAINMENT
    for o in others:
        assert contain(a, o) < 0.1
        assert aud.jaccard(sig_of(a)[1], sig_of(o)[1]) < 0.03


def test_silence_and_pure_tone_give_too_few_hashes():
    assert aud.fingerprint(np.zeros(SR * 5, np.float32)).unique.size == 0
    tone = np.sin(2 * np.pi * 440 * np.arange(SR * 5) / SR).astype(np.float32)
    assert aud.fingerprint(tone).unique.size < aud.MIN_HASHES


def test_minhash_jaccard_estimate():
    rng = np.random.default_rng(0)
    base = rng.choice(1 << 22, 4000, replace=False).astype(np.uint32)
    other = np.concatenate([base[:2000], rng.choice(1 << 22, 2000, replace=False).astype(np.uint32) + (1 << 22)])
    j = aud.jaccard(aud.minhash(base), aud.minhash(other))
    assert abs(j - 2000 / 6000) < 0.1
    assert aud.containment(1.0, 100, 100) == 1.0
    assert aud.containment(0.0, 100, 100) == 0.0


# ------------------------------------------------------------------------------ extractor + synapse


def _write_wav(path: Path, x: np.ndarray) -> Path:
    wavfile.write(path, SR, (x * 32767).astype(np.int16))
    return path


def _art(path: Path, role: str = "ad_video", mime: str = "audio/wav") -> ResolvedArtifact:
    data = path.read_bytes()
    return ResolvedArtifact(IncidentArtifact(artifact_id=hashlib.sha256(data).hexdigest(), role=role,
                                             path_in_incident=path.name, mime=mime, size=len(data)), path)


def _inc(iid: str, arts: list[ResolvedArtifact]) -> ResolvedIncident:
    return ResolvedIncident(Incident(id=iid, source="test", source_ref=iid, artifacts=[a.meta for a in arts]), arts)


needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg missing")


@needs_ffmpeg
def test_extractor_genes_and_offline_asr_is_unknown(tmp_path, settings):
    w = _write_wav(tmp_path / "ad.wav", track(1))
    inc = _inc("i1", [_art(w)])
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "work")
    genes = aud.aud_fp(inc, ctx).genes
    sig = [g for g in genes if g.kind == "audio_fp_sig"]
    assert len(sig) == 1 and sig[0].status == GeneStatus.OBSERVED and sig[0].family == Family.VIS
    assert sig[0].dependency_group == "VIS:boilerplate:audio_fp"
    assert "speech=unknown" in (sig[0].evidence.note or "")  # no Whisper weights in the test models dir
    assert len([g for g in genes if g.kind == "audio_fp_band"]) == aud.LSH_BANDS
    # cached per sha256: second run reads the npz
    assert list((settings.cache_dir / "audio_fp").glob(f"{inc.artifacts[0].meta.artifact_id}*.npz"))
    asr = aud.aud_asr(inc, ctx).genes
    assert [g.status for g in asr] == [GeneStatus.UNAVAILABLE]
    assert aud.asr_blocks(inc, ctx) == []


@needs_ffmpeg
def test_no_audio_stream_is_not_applicable(tmp_path, settings):
    v = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-an",
                    str(v)], check=True)
    inc = _inc("i1", [_art(v, mime="video/mp4")])
    genes = aud.aud_fp(inc, ExtractContext(settings=settings, work_dir=tmp_path / "w")).genes
    assert [g.status for g in genes] == [GeneStatus.NOT_APPLICABLE]


def _sig_genes(iid: str, x: np.ndarray, speech: str, aid: str) -> list[Gene]:
    u = aud.fingerprint(x).unique
    sig = aud.minhash(u)
    ev = EvidencePointer(artifact_id=aid, note=f"hashes={u.size}; speech={speech}")
    gs = [Gene(incident_id=iid, family=Family.VIS, kind="audio_fp_sig",
               value=f"{u.size}:" + sig.astype(">u4").tobytes().hex(), evidence=ev, extractor="aud_fp@1",
               dependency_group="VIS:boilerplate:audio_fp")]
    gs += [Gene(incident_id=iid, family=Family.VIS, kind="audio_fp_band", value=b, evidence=ev, extractor="aud_fp@1",
                dependency_group="VIS:boilerplate:audio_fp") for b in aud.lsh_bands(sig)]
    return gs


def _score(genes: dict[str, list[Gene]], a: str, b: str):
    rarity = build_rarity(genes, {})
    gen = {k: build_genome(k, v, {}) for k, v in genes.items()}
    return score_pair(gen[a], gen[b], rarity)


def _audio_reasons(pe):
    return [r for r in pe.score.reasons if r.kind == "audio_track_reuse"]


def test_reused_voice_track_is_capped_campaign_evidence_never_a_link_alone():
    x = track(11)
    genes = {"a": _sig_genes("a", x, "1", "fa"), "b": _sig_genes("b", degrade(x, 3), "1", "fb"),
             "c": _sig_genes("c", track(12), "1", "fc")}
    pe = _score(genes, "a", "b")
    rs = _audio_reasons(pe)
    assert rs and rs[0].bits > 4.0 and "ponownego użycia" in rs[0].explanation_pl
    assert "deepfake" in rs[0].explanation_pl  # the wording states what it is NOT
    assert pe.campaign_by_family.get("VIS", 0) <= 8.0
    assert pe.score.hypothesis != "same_campaign"  # one family alone never links
    assert not _audio_reasons(_score(genes, "a", "c"))


def test_music_or_unknown_speech_is_kit_and_stock_track_is_boilerplate():
    x = track(21)
    genes = {"a": _sig_genes("a", x, "unknown", "fa"), "b": _sig_genes("b", degrade(x, 4), "0", "fb")}
    pe = _score(genes, "a", "b")
    rs = _audio_reasons(pe)
    assert rs and rs[0].bits <= 4.0 and pe.kit_bits > 0 and pe.campaign_bits == 0
    # the same track in 4 incidents: stock music / platform sound, 0 bits
    stock = {k: _sig_genes(k, degrade(x, i), "1", f"f{k}") for i, k in enumerate("abcd")}
    assert not _audio_reasons(_score(stock, "a", "b"))


class _Seg:
    def __init__(self, start, end, text, no_speech=0.05, logprob=-0.3, cr=1.4):
        self.start, self.end, self.text = start, end, text
        self.no_speech_prob, self.avg_logprob, self.compression_ratio = no_speech, logprob, cr


class _FakeWhisper:
    """Stands in for faster_whisper.WhisperModel in unit tests (no weights in CI); returns fixed segments."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **kw):
        assert kw["language"] == "pl" and kw["vad_filter"] is True
        self.calls += 1
        return iter([_Seg(0.0, 3.2, "Zadzwoń pod numer 511 222 333 i zarejestruj się na platformie."),
                     _Seg(3.2, 6.0, "Dziękuję. Dziękuję. Dziękuję.", no_speech=0.9),  # music hallucination
                     _Seg(6.0, 9.0, "Wejdź na zysk-teraz.pl, minimalna wpłata 950 zł.")]), None


@needs_ffmpeg
def test_asr_transcript_feeds_txt_sources_and_is_cached_per_sha(tmp_path, settings):
    from hydra.genome import txt_sources

    w = _write_wav(tmp_path / "ad.wav", track(5, 10.0))
    inc = _inc("i-asr", [_art(w)])
    fake = _FakeWhisper()
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "work", models={"asr_whisper_small": fake},
                         options={"ocr_assets": False})
    blocks = aud.asr_blocks(inc, ctx)
    assert len(blocks) == 1 and blocks[0].source == "asr" and blocks[0].channel == "ad"
    segs = blocks[0].segments
    assert len(segs) == 2  # the no-speech hallucination is dropped
    assert segs[1].evidence.timecode_s == 6.0 and segs[1].evidence.artifact_id == inc.artifacts[0].meta.artifact_id
    aud.asr_blocks(inc, ctx)  # cached per artifact sha256: the model does not run again
    assert fake.calls == 1
    texts = txt_sources._collect(inc, ctx)
    asr = texts.by_source("asr")
    assert asr and "511 222 333" in asr[0].text
    sig = [g for g in aud.aud_fp(inc, ctx).genes if g.kind == "audio_fp_sig"][0]
    assert "speech=1" in (sig.evidence.note or "")
    st = aud.aud_asr(inc, ctx).genes
    assert st[0].status == GeneStatus.OBSERVED and "2 segments" in (st[0].evidence.note or "")


def test_asr_disabled_option_is_unknown(tmp_path, settings):
    w = _write_wav(tmp_path / "ad.wav", track(6, 2.0))
    inc = _inc("i", [_art(w)])
    ctx = ExtractContext(settings=settings, work_dir=tmp_path, options={"asr": False})
    assert aud.transcribe(inc.artifacts[0], ctx) is None
    assert aud.has_speech(None) is None
