"""CR-ASTRA-RES-1/2/3 regressions on synthetic inputs only (no organiser data, no real weights, isolated tmp state).

RES-1: heterogeneous provenance observations keep separate meanings on the likeness card; nothing from prov / DFK
       adds linking bits. RES-2: the AV-sync correlation uses only frames passing the face-and-speech gate.
RES-3: AudioSeal reports local marked intervals next to the unchanged clip-majority headline.
"""

from __future__ import annotations

import importlib.util
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest
import torch

from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Incident
from hydra.genome import dfk, prov, vid_avsync
from hydra.genome.base import ExtractContext, ResolvedIncident
from hydra.genome.prov import parse_manifest_store
from hydra.genome.vid_avsync import av_sync_score, suspicion_level
from hydra.likeness import build_card
from hydra.synapse import build_genome, level, score_pair
from tests.genes_prov.c2pa_synth import signed_jpeg
from tests.genes_vis.helpers import artifact

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
ROOT = Path(__file__).resolve().parents[2]


def gene(kind, value, quality=1.0, family="META", art=None):
    ev = {"note": "synthetic input"} | ({"artifact_id": art} if art else {})
    return dict(kind=kind, value=value, status="observed", family=family, quality=quality, evidence=ev,
                provenance="OBS")


def ai_signal(genes):
    card = build_card("synthetic-only", genes, [])
    return card, next(s for s in card["signals"] if s["key"] == "ai_generator")


# ------------------------------------------------------------------------------------------------ RES-1

ASTRA_CASES = {  # docs/research/astra_upgrade_2026-11-21/deepfake_probe.py
    "audioseal_only": [gene("wm_audio", "audioseal"), gene("wm_audio_check", "detected")],
    "c2pa_invalid": [gene("c2pa_ai_generated", "trainedAlgorithmicMedia"), gene("c2pa_validation", "errors"),
                     gene("c2pa_manifest", "present")],
    "low_quality_avatar": [gene("synth_avatar_eyes", "ffhq_aligned", 0.2, "ID"),
                           gene("avatar_geometry_check", "flagged", family="ID")],
    "unsigned_png_prompt_key": [gene("ai_metadata_marker", "png:prompt")],
}


@pytest.mark.parametrize("name", sorted(ASTRA_CASES))
def test_weak_or_unverified_observations_are_not_generator_premises(name):
    card, sig = ai_signal(ASTRA_CASES[name])
    assert sig["state"] == "unknown" and sig["state_label_pl"] == "nieznane"
    assert card["present"] == 0 and card["strength"] == "brak"
    present = [c for c in sig["checks"] if c["state"] == "present"]
    assert present and all(not c["counted"] for c in present)  # still shown: raw diagnostics are kept
    assert sig["items"], "evidence pointers of the observation must stay visible"
    assert "autentyczne" not in (sig["summary_pl"] + str(sig["state_label_pl"]))


def test_audioseal_wording_and_scope():
    _, sig = ai_signal(ASTRA_CASES["audioseal_only"])
    aw = {c["key"]: c for c in sig["checks"]}["audio_watermark"]
    assert aw["summary_pl"].startswith("wykryto znak AudioSeal") and "prawdziwego głosu" in aw["summary_pl"]
    assert aw["role"] == "tool_mark" and not aw["counted"]
    _, sig = ai_signal([gene("wm_audio_check", "not_detected")])
    aw = {c["key"]: c for c in sig["checks"]}["audio_watermark"]
    assert sig["state"] == "unknown" and "nieznane" in aw["summary_pl"] and "nieznane" in sig["summary_pl"]


def test_legacy_c2pa_genes_count_only_with_valid_binding():
    a = "a" * 64
    ok = [gene("c2pa_manifest", "present"), gene("c2pa_ai_generated", "trainedAlgorithmicMedia", art=a),
          gene("c2pa_validation", "Valid", art=a)]
    card, sig = ai_signal(ok)
    assert sig["state"] == "present" and card["present"] == 1
    bad = ok[:2] + [gene("c2pa_validation", "Invalid", art=a)]
    card, sig = ai_signal(bad)
    assert sig["state"] == "unknown" and card["present"] == 0
    assert {c["key"]: c["state"] for c in sig["checks"]}["c2pa_unverified"] == "present"
    # binding gene of the current extractor wins over the raw validation string
    card, _ = ai_signal(ok + [gene("c2pa_binding", "errors", art=a)])
    assert card["present"] == 0


def test_generator_watermark_still_counts():
    card, sig = ai_signal([gene("wm_generator", "sdxl_dwtdct"), gene("wm_check", "detected")])
    assert sig["state"] == "present" and card["present"] == 1


def test_parse_manifest_separates_integrity_and_trust():
    store = {"active_manifest": "s", "manifests": {"s": {"assertions": [{"label": "c2pa.actions", "data": {
        "actions": [{"digitalSourceType":
                     "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"}]}}]}},
        "validation_status": [{"code": "signingCredential.untrusted"}, {"code": "assertion.dataHash.mismatch"}]}
    f = parse_manifest_store(store)
    assert f["ai_source"] == "trainedAlgorithmicMedia"  # raw assertion kept
    assert f["binding"] == "errors" and f["trust"] == "untrusted"
    assert f["integrity_codes"] == ["assertion.dataHash.mismatch"]
    only_trust = {**store, "validation_state": "Valid", "validation_status": [{"code": "signingCredential.untrusted"}]}
    g = parse_manifest_store(only_trust)
    assert g["binding"] == "valid" and g["trust"] == "untrusted"
    assert parse_manifest_store({"manifests": {}})["binding"] == "unknown"


def _prov_genes(settings, tmp_path, path):
    a = artifact(path, "ad_creative")
    inc = ResolvedIncident(Incident(id="syn-sem", source="synthetic", source_ref="t", artifacts=[a.meta]), [a])
    genes = prov.prov(inc, ExtractContext(settings=settings, work_dir=tmp_path / "work")).genes
    return genes, [g.model_dump(mode="json") for g in genes]


def test_real_signed_and_tampered_manifest(settings, tmp_path):
    p = signed_jpeg(tmp_path / "gen.jpg", ai=True)
    genes, dicts = _prov_genes(settings, tmp_path, p)
    kinds = {g.kind: g.value for g in genes}
    assert kinds["c2pa_binding"] == "valid" and kinds["c2pa_trust"] == "untrusted"
    assert kinds["c2pa_ai_generated"] == "trainedAlgorithmicMedia"
    card, sig = ai_signal(dicts)
    c2pa = {c["key"]: c for c in sig["checks"]}["c2pa"]
    assert sig["state"] == "present" and "spoza listy zaufanych" in c2pa["summary_pl"]
    b = bytearray(p.read_bytes())
    b[-200] ^= 0xFF  # one flipped byte in the image data: c2pa reports assertion.dataHash.mismatch
    t = tmp_path / "tampered.jpg"
    t.write_bytes(bytes(b))
    genes, dicts = _prov_genes(settings, tmp_path, t)
    kinds = {g.kind: g.value for g in genes}
    assert kinds["c2pa_binding"] == "errors" and "c2pa_ai_generated" not in kinds
    assert kinds["c2pa_ai_declared_unverified"] == "trainedAlgorithmicMedia"  # raw diagnostic kept
    card, sig = ai_signal(dicts)
    assert sig["state"] == "unknown" and card["present"] == 0


def _all_label_genes(iid: str) -> list[Gene]:
    ev = EvidencePointer(artifact_id="b" * 64, note="synthetic")
    inc = ResolvedIncident(Incident(id=iid, source="synthetic", source_ref="t", artifacts=[]), [])
    out = [prov._gene(inc, k, v, ev) for k, v in (
        ("c2pa_manifest", "present"), ("c2pa_claim_generator", "SyntheticGen 1.0"), ("c2pa_signer", "Syn"),
        ("c2pa_validation", "Valid"), ("c2pa_binding", "valid"), ("c2pa_trust", "untrusted"),
        ("c2pa_ai_generated", "trainedAlgorithmicMedia"), ("c2pa_ai_declared_unverified", "trainedAlgorithmicMedia"),
        ("ai_metadata_marker", "png:prompt"), ("wm_generator", "sdxl_dwtdct"), ("wm_check", "detected"))]
    out += [prov._gene(inc, k, v, ev, family=Family.ID) for k, v in (("synth_avatar_eyes", "ffhq_offset=0.010"),
                                                                     ("avatar_geometry_check", "flagged"))]
    for kind, grp, fam, val in (("wm_audio", dfk.GROUP_WM, Family.META, "audioseal"),
                                ("wm_audio_local", dfk.GROUP_WM, Family.META, "audioseal_partial"),
                                ("wm_audio_check", dfk.GROUP_WM, Family.META, "detected"),
                                ("synthetic_image_suspicion", dfk.GROUP_IMAGE, Family.VIS, "high"),
                                ("synthetic_video_suspicion", dfk.GROUP_VIDEO, Family.VIS, "high")):
        out.append(dfk._gene(iid, fam, kind, grp, GeneStatus.OBSERVED, "synthetic", art="b" * 64, value=val))
    out.append(vid_avsync._gene(iid, GeneStatus.OBSERVED, "b" * 64, "synthetic", value="high"))
    return out


def test_no_linking_bits_from_any_prov_or_dfk_gene():
    from hydra.pipeline import rarity_for

    genomes = {i: build_genome(i, _all_label_genes(i), {}) for i in ("a", "b")}
    for i in range(6):
        genomes[f"x{i}"] = build_genome(f"x{i}", [], {})
    assert all(level(g) == "none" for g in genomes["a"].genes)
    pe = score_pair(genomes["a"], genomes["b"], rarity_for(genomes))
    assert pe.operator_bits == 0.0 and pe.campaign_bits == 0.0 and pe.kit_bits == 0.0 and pe.asset_bits == 0.0
    assert pe.score.hypothesis != "same_campaign"


# ------------------------------------------------------------------------------------------------ RES-2


def _astra_arrays():
    envelope = np.zeros(200)
    envelope[:40] = 1 + 0.2 * np.sin(np.arange(40))
    return envelope.copy(), envelope


def test_samples_outside_gate_do_not_change_the_score():
    mouth, env = _astra_arrays()
    base = av_sync_score(mouth, env)
    gate = env > 0.2 * np.percentile(env, 95)
    rng = np.random.default_rng(0)
    for k in range(5):
        m = mouth.copy()
        m[~gate] = 100 * rng.normal(size=int((~gate).sum()))
        e = env.copy()
        e[~gate] = rng.uniform(0, 0.19 * np.percentile(env, 95), int((~gate).sum()))  # stays below the gate
        for mm, ee in ((m, env), (mouth, e), (m, e)):
            s = av_sync_score(mm, ee)
            assert (s["r"], s["lag_s"], s["speech_s"]) == (base["r"], base["lag_s"], base["speech_s"]), k
    # frames without a face are outside the gate too
    m = mouth.copy()
    m[100:] = np.nan
    assert av_sync_score(m, env)["r"] == base["r"]


def test_lag_shift_masks_both_series():
    rng = np.random.default_rng(3)
    env = np.abs(rng.normal(size=300)) * (np.arange(300) % 40 < 25)
    mouth = np.roll(env, -2) + 0.05 * rng.normal(size=300)  # mouth leads audio by 0.2 s
    s = av_sync_score(mouth, env)
    assert s["lag_s"] == pytest.approx(0.2) and suspicion_level(s) == "low"
    # garbage in mouth frames whose partner audio frame (i + lag) is outside the gate cannot raise or lower r
    gate = env > 0.2 * np.percentile(env, 95)
    m = mouth.copy()
    m[~gate] = 1e3
    assert av_sync_score(m, env)["r"] == s["r"]


def _scenarios():
    spec = importlib.util.spec_from_file_location("measure_dfk_semantics", ROOT / "scripts/measure_dfk_semantics.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.scenarios


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_av_scenarios_dubbing_narration_cuts_no_face(seed):
    sc = {k: suspicion_level(av_sync_score(m, e)) for k, (m, e) in _scenarios()(seed).items()}
    assert sc["matched"] == "low" and sc["matched_lag2"] == "low"
    assert sc["dubbing"] in ("medium", "high")  # the same signal as a manipulation: uncalibrated cue only
    assert sc["narration_face_closed"] in ("medium", "high")
    assert sc["narration_broll"] is None and sc["no_face"] is None  # too little face: unknown, not a penalty
    assert sc["cuts_speaker_listener"] in ("low", "medium", "high")


# ------------------------------------------------------------------------------------------------ RES-3


class MarkedPrefix:
    """Idealised probability stub (NOT a real AudioSeal detector): the first `marked_s` seconds of the clip are marked;
    keeps absolute time across 30 s chunks via a running offset."""

    def __init__(self, marked_s: float, sr: int = 16000):
        self.left = int(marked_s * sr)

    def __call__(self, x):
        n = x.shape[-1]
        p = torch.zeros((1, 2, n))
        k = max(0, min(n, self.left))
        p[0, 1, :k] = 1.0
        self.left -= n
        return p, None


def test_majority_headline_unchanged_and_local_interval_reported():
    x = np.zeros(160000, np.float32)
    assert dfk.audioseal_score(MarkedPrefix(1.0), x) == pytest.approx(0.1)  # Astra probe value, below 0.5
    prof = dfk.audioseal_profile(MarkedPrefix(3.0), x)
    assert prof["fraction"] == pytest.approx(0.3) and prof["checked_s"] == 10.0
    assert prof["intervals"] == [(0.0, 3.0)] and prof["marked_share"] == pytest.approx(0.3)
    assert dfk.audioseal_profile(MarkedPrefix(0.0), x)["intervals"] == []
    assert dfk.AS_THRESHOLD == 0.5  # global threshold not lowered


def test_interval_rules():
    m = np.zeros(10 * 16000, bool)
    m[16000:16000 + 8001] = True  # 1 s window with 50.006% marked
    assert dfk.marked_intervals(m, min_windows=1) == [(1.0, 2.0)]
    assert dfk.marked_intervals(m) == ([] if dfk.AS_MIN_WINDOWS > 1 else [(1.0, 2.0)])
    m[2 * 16000:5 * 16000] = True
    assert dfk.marked_intervals(m) == [(1.0, 5.0)]


def _wav(path: Path, seconds: float) -> Path:
    t = np.arange(int(16000 * seconds)) / 16000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes())
    return path


@needs_ffmpeg
@pytest.mark.parametrize(("marked_s", "headline", "local"), [(3.0, False, True), (6.0, True, False),
                                                             (0.0, False, False)])
def test_extractor_reports_local_marks_without_changing_headline(settings, tmp_path, marked_s, headline, local):
    a = artifact(_wav(tmp_path / "a.wav", 10.0), "ad_video", mime="audio/wav")
    inc = ResolvedIncident(Incident(id="syn-as", source="synthetic", source_ref="t", artifacts=[a.meta]), [a])
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w", models={"dfk_audioseal": MarkedPrefix(marked_s)})
    genes = dfk.dfk_audio_wm(inc, ctx).genes
    kinds = {g.kind: g for g in genes}
    assert ("wm_audio" in kinds) is headline and ("wm_audio_local" in kinds) is local
    assert kinds["wm_audio_check"].value == ("detected" if headline else "not_detected")
    if local:
        g = kinds["wm_audio_local"]
        assert g.evidence.timecode_s == 0.0 and "0–3 s" in g.evidence.note and level(g) == "none"
        _, sig = ai_signal([g.model_dump(mode="json") for g in genes])
        aw = {c["key"]: c for c in sig["checks"]}["audio_watermark"]
        assert aw["state"] == "present" and "fragmentach" in aw["summary_pl"] and not aw["counted"]
