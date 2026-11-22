"""Video-PDQ: temporal matching of shared footage between two videos (ad-to-ad, ad-to-source).

Our own implementation of the vPDQ idea (per-frame PDQ over sampled keyframes; no vpdq code): every video
artifact is already a sequence of visual units in the VIS stream (hydra/genome/vis_media.py: 1 fps frames +
scene keyframes, near-identical consecutive frames dropped, phone chrome cropped, ad regions of screen
recordings as their own units), each with a `pdq_canon` hash, a DINOv2 vector and `EvidencePointer.timecode_s`.

`match_sequences` aligns two such sequences:
1. near-identical frame pairs: PDQ Hamming <= 31 (SPEC_ERRATA: survives re-encode and 1% crops), or DINOv2
   cosine >= 0.97 with PDQ <= 96 (2-4% crops / letterboxing break PDQ but keep it far below the random 128;
   cosine alone matched unrelated synthetic shots at 0.96), or cosine >= 0.98 when a frame has no PDQ;
2. each pair votes for a time offset t_b - t_a; pairs within OFFSET_TOL_S of the best offset form a candidate run;
3. the run is the longest monotone chain (time increasing in both videos, gaps <= MAX_GAP_S);
4. result: matched segment [start, end] in both videos, the offset, the number of matched frames.

Frequency check (`annotate_footage`): for every frame unit, the number of corpus incidents that hold a
near-identical frame. Footage shared by more than FOOTAGE_MAX_DF incidents (stock clips, a news broadcast cut into
many unrelated ads) is boilerplate tier: reported, 0 bits, never an operator link.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from hydra.synapse import IncidentGenome

log = logging.getLogger(__name__)

PDQ_MAX = 31            # SPEC_ERRATA: PDQ near-duplicate threshold
COS_MIN = 0.97          # DINOv2 near-identical frame ...
PDQ_CROP_MAX = 96       # ... only while PDQ stays clearly below random (128): crops 2-4% give 34-80
COS_ONLY_MIN = 0.98     # frames without a usable PDQ hash (flat / low quality)
OFFSET_TOL_S = 1.5      # 1 fps sampling + scene keyframes at arbitrary times
MAX_GAP_S = 12.0        # VIS keeps >= 1 frame per 10 s even on static footage
MIN_FRAMES = 2          # a "segment" needs at least two aligned frames
FOOTAGE_MAX_DF = 2      # same rule as synapse.STOCK_IMAGE_MAX_DF: held by more incidents = stock/news footage
FRAME_CLASSES = {"frame", "ad_region"}
MAX_INDEX_FRAMES = 60_000  # corpus frequency index cap (torch matmul over 256-bit vectors)
_UNIT_RE = re.compile(r"unit=([^;\s]+)")


@dataclass
class Frame:
    uid: str
    artifact_id: str
    t: float
    pdq: np.ndarray | None       # packed uint8[32]
    vec: np.ndarray | None       # DINOv2, L2-normalised
    gene: Any = None             # a representative Gene (evidence pointer)
    footage_df: int = 1


@dataclass
class Segment:
    artifact_a: str
    artifact_b: str
    start_a: float
    end_a: float
    start_b: float
    end_b: float
    offset_s: float              # t_b - t_a
    frames: int
    min_hamming: int | None
    mean_cos: float | None
    footage_df: int              # max over the matched frames of incidents holding near-identical footage
    pairs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return round(max(self.end_a - self.start_a, self.end_b - self.start_b), 3)

    @property
    def common(self) -> bool:
        return self.footage_df > FOOTAGE_MAX_DF

    def note(self, side: str) -> str:
        s, e = (self.start_a, self.end_a) if side == "a" else (self.start_b, self.end_b)
        return (f"video_segment start={s:.2f} end={e:.2f} offset={self.offset_s:+.2f} frames={self.frames}"
                f" footage_df={self.footage_df}")


NOTE_RE = re.compile(r"video_segment start=(-?[\d.]+) end=(-?[\d.]+) offset=([+-]?[\d.]+) frames=(\d+)"
                     r"(?: footage_df=(\d+))?")


def parse_note(note: str | None) -> dict[str, float] | None:
    m = NOTE_RE.search(note or "")
    if not m:
        return None
    return {"start": float(m.group(1)), "end": float(m.group(2)), "offset": float(m.group(3)),
            "frames": int(m.group(4)), "footage_df": int(m.group(5) or 1)}


POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint16)


def _hamming(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return POPCOUNT[np.bitwise_xor(a[:, None, :], b[None, :, :])].sum(axis=2)


def frames_of(g: IncidentGenome) -> dict[str, list[Frame]]:
    """Video frame units of an incident, per artifact, ordered by time."""
    out: dict[str, list[Frame]] = defaultdict(list)
    for uid, d in g.units.items():
        gene = d.get("gene")
        if gene is None or d.get("class") not in FRAME_CLASSES:
            continue
        ev = gene.evidence
        if ev.timecode_s is None or ev.artifact_id is None:
            continue
        if "pdq" not in d and "dinov2" not in d:
            continue
        out[ev.artifact_id].append(Frame(uid, ev.artifact_id, float(ev.timecode_s), d.get("pdq"),
                                         d.get("dinov2"), gene, int(d.get("footage_df", 1))))
    for fr in out.values():
        fr.sort(key=lambda f: (f.t, f.uid))
    return dict(out)


def near_matrix(fa: list[Frame], fb: list[Frame]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(near bool [na, nb], hamming int (999 = unknown), cosine float (nan = unknown))."""
    na, nb = len(fa), len(fb)
    ham = np.full((na, nb), 999, dtype=np.int32)
    cos = np.full((na, nb), np.nan, dtype=np.float32)
    ia = [i for i, f in enumerate(fa) if f.pdq is not None]
    ib = [j for j, f in enumerate(fb) if f.pdq is not None]
    if ia and ib:
        ham[np.ix_(ia, ib)] = _hamming(np.stack([fa[i].pdq for i in ia]), np.stack([fb[j].pdq for j in ib]))
    va = [i for i, f in enumerate(fa) if f.vec is not None]
    vb = [j for j, f in enumerate(fb) if f.vec is not None]
    if va and vb:
        cos[np.ix_(va, vb)] = np.stack([fa[i].vec for i in va]) @ np.stack([fb[j].vec for j in vb]).T
    c = np.nan_to_num(cos, nan=-1.0)
    near = (ham <= PDQ_MAX) | ((c >= COS_MIN) & (ham <= PDQ_CROP_MAX)) | ((c >= COS_ONLY_MIN) & (ham >= 999))
    return near, ham, cos


def _chain(cands: list[tuple[int, int, float]], fa: list[Frame], fb: list[Frame]) -> list[tuple[int, int]]:
    """Longest chain with strictly increasing time in A, non-decreasing in B, gaps <= MAX_GAP_S (O(n^2) DP)."""
    cands = sorted(cands, key=lambda c: (fa[c[0]].t, fb[c[1]].t, -c[2]))
    n = len(cands)
    best = [1] * n
    prev = [-1] * n
    for k in range(n):
        ik, jk, _ = cands[k]
        for m in range(k):
            im, jm, _ = cands[m]
            dta, dtb = fa[ik].t - fa[im].t, fb[jk].t - fb[jm].t
            if 0 < dta <= MAX_GAP_S and 0 <= dtb <= MAX_GAP_S and best[m] + 1 > best[k]:
                best[k], prev[k] = best[m] + 1, m
    if not n:
        return []
    k = max(range(n), key=lambda x: (best[x], -x))
    out = []
    while k >= 0:
        out.append(cands[k][:2])
        k = prev[k]
    return out[::-1]


def match_sequences(fa: list[Frame], fb: list[Frame]) -> Segment | None:
    """Best aligned run of near-identical frames between two videos, or None (< MIN_FRAMES aligned frames)."""
    if len(fa) < MIN_FRAMES or len(fb) < MIN_FRAMES:
        return None
    near, ham, cos = near_matrix(fa, fb)
    ii, jj = np.nonzero(near)
    if len(ii) < MIN_FRAMES:
        return None
    offs = np.array([fb[j].t - fa[i].t for i, j in zip(ii, jj, strict=True)])
    best: tuple[tuple[int, float], list[tuple[int, int]], float] | None = None
    for o in sorted(set(np.round(offs, 1).tolist())):
        sel = np.abs(offs - o) <= OFFSET_TOL_S
        cands = [(int(i), int(j), float(np.nan_to_num(cos[i, j], nan=0.0)))
                 for i, j in zip(ii[sel], jj[sel], strict=True)]
        chain = _chain(cands, fa, fb)
        if len(chain) < MIN_FRAMES:
            continue
        span = fa[chain[-1][0]].t - fa[chain[0][0]].t
        key = (len(chain), span)
        if best is None or key > best[0]:
            best = (key, chain, o)
    if best is None:
        return None
    _, chain, _ = best
    ia, ib = [c[0] for c in chain], [c[1] for c in chain]
    hams = [int(ham[i, j]) for i, j in chain if ham[i, j] < 999]
    coss = [float(cos[i, j]) for i, j in chain if not math.isnan(float(cos[i, j]))]
    offset = float(np.median([fb[j].t - fa[i].t for i, j in chain]))
    df = max(max(fa[i].footage_df, fb[j].footage_df) for i, j in chain)
    return Segment(fa[0].artifact_id, fb[0].artifact_id, fa[ia[0]].t, fa[ia[-1]].t, fb[ib[0]].t, fb[ib[-1]].t,
                   round(offset, 3), len(chain), min(hams) if hams else None,
                   round(float(np.mean(coss)), 4) if coss else None, df,
                   [(fa[i].uid, fb[j].uid) for i, j in chain])


def match_genomes(a: IncidentGenome, b: IncidentGenome) -> tuple[Segment, Frame, Frame] | None:
    """Best segment over all video-artifact pairs of two incidents (most frames, then longest)."""
    sa, sb = frames_of(a), frames_of(b)
    best: tuple[Segment, Frame, Frame] | None = None
    for xa in sorted(sa):
        for xb in sorted(sb):
            if xa == xb:
                continue  # the same file in both incidents is the artifact_sha256 rule's job
            seg = match_sequences(sa[xa], sb[xb])
            if seg is None:
                continue
            first_a = next(f for f in sa[xa] if f.uid == seg.pairs[0][0])
            first_b = next(f for f in sb[xb] if f.uid == seg.pairs[0][1])
            if best is None or (seg.frames, seg.duration_s) > (best[0].frames, best[0].duration_s):
                best = (seg, first_a, first_b)
    return best


def annotate_footage(genomes: dict[str, IncidentGenome]) -> dict[str, int]:
    """Set units[u]["footage_df"] = incidents holding a near-identical frame (PDQ <= 31, the incident itself
    included) for every video frame unit of the corpus. Returns summary counts."""
    rows: list[tuple[str, str, np.ndarray]] = []
    for iid, g in genomes.items():
        for uid, d in g.units.items():
            gene = d.get("gene")
            if (d.get("class") in FRAME_CLASSES and "pdq" in d and gene is not None
                    and gene.evidence.timecode_s is not None):
                rows.append((iid, uid, d["pdq"]))
    info = {"frames": len(rows), "common_frames": 0}
    if not rows:
        return info
    if len(rows) > MAX_INDEX_FRAMES:
        log.warning("footage index skipped: %d frames > %d (frequency falls back to exact hash df)", len(rows),
                    MAX_INDEX_FRAMES)
        info["skipped"] = 1
        return info
    import torch

    bits = np.unpackbits(np.stack([r[2] for r in rows]), axis=1).astype(np.float32)
    x = torch.from_numpy(bits)
    pop = x.sum(dim=1)
    ids = {iid: k for k, iid in enumerate(sorted(genomes))}
    inc_idx = np.array([ids[r[0]] for r in rows])
    for s in range(0, len(rows), 2048):
        xs = x[s : s + 2048]
        d = pop[s : s + 2048, None] + pop[None, :] - 2.0 * (xs @ x.T)
        hit = (d <= PDQ_MAX + 0.5).numpy()
        for k in range(hit.shape[0]):
            n_inc = len(set(inc_idx[hit[k]].tolist()))
            iid, uid, _ = rows[s + k]
            genomes[iid].units[uid]["footage_df"] = max(1, n_inc)
            if n_inc > FOOTAGE_MAX_DF:
                info["common_frames"] += 1
    return info


def segment_bits(seg: Segment, neardup_bits: float) -> float:
    """Bits of a shared-footage segment: boilerplate (0) when the footage is common in the corpus, else
    near-duplicate strength for >= 3 frames, scaled down for a two-frame run."""
    if seg.common:
        return 0.0
    return neardup_bits if seg.frames >= 3 else round(neardup_bits * 0.5, 3)
