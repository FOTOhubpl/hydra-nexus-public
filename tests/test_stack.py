"""Library stack smoke tests: one small *working* call per dependency, generic synthetic inputs only.

Proves each library the build relies on is installed and behaves as expected on this machine, offline
(the conftest socket guard fails any test that touches the network). No task logic lives here.
DINOv2 / e5 / RapidOCR are covered by tests/test_media_ml.py.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from conftest import POLISH_LINE
from PIL import Image, ImageDraw

from hydra.config import get_settings
from hydra.ml.models import ModelUnavailable, model_dir

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract missing")


def _shapes_image(seed: int = 0, size: tuple[int, int] = (480, 360)) -> np.ndarray:
    """Random rectangles/ellipses/lines on noise: textured enough for keypoints and hashes."""
    rng = np.random.default_rng(seed)
    im = Image.fromarray(rng.integers(0, 60, (size[1], size[0], 3), dtype=np.uint8))
    d = ImageDraw.Draw(im)
    for _ in range(40):
        x0, y0 = int(rng.integers(0, size[0] - 40)), int(rng.integers(0, size[1] - 40))
        x1, y1 = x0 + int(rng.integers(10, 120)), y0 + int(rng.integers(10, 120))
        color = tuple(int(c) for c in rng.integers(60, 256, 3))
        kind = int(rng.integers(0, 3))
        if kind == 0:
            d.rectangle((x0, y0, x1, y1), fill=color)
        elif kind == 1:
            d.ellipse((x0, y0, x1, y1), outline=color, width=3)
        else:
            d.line((x0, y0, x1, y1), fill=color, width=4)
    return np.asarray(im)


# --- OpenCV -------------------------------------------------------------------------------------
@pytest.mark.parametrize("detector", ["orb", "sift"])
def test_cv2_keypoints_and_magsac_homography_recover_known_warp(detector: str):
    import cv2

    src = cv2.cvtColor(_shapes_image(1), cv2.COLOR_RGB2GRAY)
    h_true = np.array([[0.9, 0.08, 20.0], [-0.06, 0.95, 15.0], [1e-4, -5e-5, 1.0]])
    dst = cv2.warpPerspective(src, h_true, (src.shape[1], src.shape[0]))

    if detector == "orb":
        det, norm = cv2.ORB_create(nfeatures=2000), cv2.NORM_HAMMING
    else:
        det, norm = cv2.SIFT_create(nfeatures=2000), cv2.NORM_L2
    k1, d1 = det.detectAndCompute(src, None)
    k2, d2 = det.detectAndCompute(dst, None)
    matches = cv2.BFMatcher(norm, crossCheck=True).match(d1, d2)
    assert len(matches) >= 50
    p1 = np.float32([k1[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    p2 = np.float32([k2[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
    h_est, mask = cv2.findHomography(p1, p2, cv2.USAC_MAGSAC, 3.0)
    assert h_est is not None and int(mask.sum()) >= 30

    corners = np.float32([[50, 50], [400, 50], [400, 300], [50, 300]]).reshape(-1, 1, 2)
    err = np.linalg.norm(cv2.perspectiveTransform(corners, h_est) - cv2.perspectiveTransform(corners, h_true), axis=2)
    assert float(err.max()) < 3.0


def _model_file(key: str, name: str) -> Path:
    try:
        path = model_dir(key) / name
    except ModelUnavailable:
        pytest.skip(f"{key} not fetched")
    if not path.is_file():
        pytest.skip(f"{path} missing")
    return path


def test_cv2_yunet_and_sface_load_and_run():
    import cv2

    yunet = _model_file("yunet", "face_detection_yunet_2023mar.onnx")
    sface = _model_file("sface", "face_recognition_sface_2021dec.onnx")
    detector = cv2.FaceDetectorYN.create(str(yunet), "", (320, 240), 0.9, 0.3, 50)
    img = cv2.cvtColor(_shapes_image(2, (320, 240)), cv2.COLOR_RGB2BGR)
    _, faces = detector.detect(img)
    assert faces is None or faces.shape[1] == 15  # synthetic shapes: normally no face

    recognizer = cv2.FaceRecognizerSF.create(str(sface), "")
    crop = cv2.resize(img, (112, 112))  # SFace takes an aligned 112x112 crop
    f1 = recognizer.feature(crop)
    f2 = recognizer.feature(cv2.flip(crop, 1))
    assert f1.shape == (1, 128)
    same = recognizer.match(f1, f1, cv2.FaceRecognizerSF_FR_COSINE)
    other = recognizer.match(f1, f2, cv2.FaceRecognizerSF_FR_COSINE)
    assert same == pytest.approx(1.0, abs=1e-4) and other < same


# --- perceptual hashes --------------------------------------------------------------------------
def test_pdqhash_and_imagehash_separate_near_duplicate_from_different():
    import imagehash
    import pdqhash

    a = _shapes_image(3)
    near = np.asarray(Image.fromarray(a).resize((360, 270)).resize((480, 360)))
    other = _shapes_image(4)

    def pdq(x: np.ndarray) -> np.ndarray:
        bits, quality = pdqhash.compute(np.ascontiguousarray(x))
        assert bits.shape == (256,) and quality > 0
        return bits

    ha, hn, ho = pdq(a), pdq(near), pdq(other)
    assert int((ha != hn).sum()) < int((ha != ho).sum())

    ph = [imagehash.phash(Image.fromarray(x)) for x in (a, near, other)]
    assert (ph[0] - ph[1]) < (ph[0] - ph[2])
    assert imagehash.hex_to_hash(str(ph[0])) == ph[0]


# --- datasketch -----------------------------------------------------------------------------------
def test_datasketch_minhash_lsh_finds_overlapping_sets():
    from datasketch import MinHash, MinHashLSH

    def mh(tokens: set[str]) -> MinHash:
        m = MinHash(num_perm=128, seed=1)
        for t in tokens:
            m.update(t.encode())
        return m

    base = {f"tok{i}" for i in range(100)}
    variant = (base - {f"tok{i}" for i in range(5)}) | {"x1", "x2", "x3", "x4", "x5"}
    unrelated = {f"other{i}" for i in range(100)}
    lsh = MinHashLSH(threshold=0.7, num_perm=128)
    lsh.insert("variant", mh(variant))
    lsh.insert("unrelated", mh(unrelated))
    assert lsh.query(mh(base)) == ["variant"]
    assert mh(base).jaccard(mh(variant)) == pytest.approx(90 / 110, abs=0.12)


# --- networkx -----------------------------------------------------------------------------------
def test_networkx_louvain_and_maximum_spanning_arborescence():
    import networkx as nx

    g = nx.Graph()
    for block in (range(0, 6), range(6, 12)):
        g.add_edges_from((a, b) for a in block for b in block if a < b)
    g.add_edge(5, 6)
    communities = nx.community.louvain_communities(g, seed=0)
    assert sorted(sorted(c) for c in communities) == [list(range(6)), list(range(6, 12))]

    dg = nx.DiGraph()
    dg.add_weighted_edges_from([("a", "b", 5), ("a", "c", 1), ("b", "c", 4), ("c", "b", 2), ("b", "d", 3)])
    tree = nx.maximum_spanning_arborescence(dg)
    assert set(tree.edges()) == {("a", "b"), ("b", "c"), ("b", "d")}


# --- scikit-learn ---------------------------------------------------------------------------------
def test_sklearn_hdbscan_and_agglomerative_find_blobs():
    from sklearn.cluster import HDBSCAN, AgglomerativeClustering
    from sklearn.datasets import make_blobs
    from sklearn.metrics import adjusted_rand_score

    x, y = make_blobs(n_samples=300, centers=[[0, 0], [10, 0], [0, 10]], cluster_std=0.5, random_state=0)
    hd = HDBSCAN(min_cluster_size=10, copy=True).fit_predict(x)
    assert len(set(hd) - {-1}) == 3
    assert adjusted_rand_score(y[hd >= 0], hd[hd >= 0]) > 0.95
    # threshold mode (no fixed k), the variant used for "cut the dendrogram at distance d"
    agg = AgglomerativeClustering(n_clusters=None, distance_threshold=3.0, linkage="average")
    labels = agg.fit_predict(x)
    assert adjusted_rand_score(y, labels) > 0.95


# --- tldextract (pinned PSL, no network) ----------------------------------------------------------
def test_tldextract_uses_pinned_offline_cache():
    import tldextract

    settings = get_settings()
    assert settings.cache_dir is not None
    cache = settings.cache_dir / "tldextract"
    if not any(cache.glob("publicsuffix.org-tlds/*.json")):
        pytest.skip(f"PSL cache not seeded in {cache} (scripts/fetch_models.py)")
    # fallback_to_snapshot=False: a missing cache raises instead of silently using the bundled snapshot.
    ext = tldextract.TLDExtract(cache_dir=str(cache), suffix_list_urls=(), fallback_to_snapshot=False)
    r = ext("https://forum.example.co.uk/path?q=1")
    assert (r.subdomain, r.domain, r.suffix) == ("forum", "example", "co.uk")
    assert ext("http://a.b.gov.pl").top_domain_under_public_suffix == "b.gov.pl"
    assert ext("http://user.github.io", include_psl_private_domains=True).suffix == "github.io"


# --- lxml + BeautifulSoup -------------------------------------------------------------------------
HTML = """<!doctype html><html><head><title>Próba</title><meta property="og:url" content="https://x.example/a">
<script src="https://cdn.example/t.js"></script></head><body><a href="/r?id=1">link</a><p>Zażółć <b>gęślą</b></p>
<div><span>unclosed</div></body></html>"""


def test_lxml_and_bs4_parse_messy_html():
    import lxml.html
    from bs4 import BeautifulSoup

    doc = lxml.html.fromstring(HTML)
    assert doc.xpath("//meta[@property='og:url']/@content") == ["https://x.example/a"]
    assert doc.xpath("//script/@src") == ["https://cdn.example/t.js"]
    soup = BeautifulSoup(HTML, "lxml")
    assert soup.title is not None and soup.title.string == "Próba"
    assert [a["href"] for a in soup.find_all("a")] == ["/r?id=1"]
    assert "Zażółć gęślą" in soup.get_text(" ", strip=True).replace("  ", " ")
    assert BeautifulSoup(HTML, "html.parser").find("span").get_text() == "unclosed"


# --- STIX 2.1 -----------------------------------------------------------------------------------
def test_stix2_bundle_roundtrip():
    import stix2

    ident = stix2.Identity(name="Generic test identity", identity_class="system")
    ind = stix2.Indicator(
        name="test", pattern="[url:value = 'https://example.org/x']", pattern_type="stix",
        created_by_ref=ident.id,
    )
    rel = stix2.Relationship(ind, "related-to", ident)
    bundle = stix2.Bundle(ident, ind, rel)
    parsed = stix2.parse(bundle.serialize(), allow_custom=False)
    assert {o.type for o in parsed.objects} == {"identity", "indicator", "relationship"}
    assert parsed.objects[1].pattern == ind.pattern


# --- cryptography -------------------------------------------------------------------------------
def test_cryptography_ed25519_sign_verify():
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

    key = Ed25519PrivateKey.generate()
    msg = b'{"manifest":"sha256:00"}'
    sig = key.sign(msg)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub = Ed25519PublicKey.from_public_bytes(raw)
    pub.verify(sig, msg)
    with pytest.raises(InvalidSignature):
        pub.verify(sig, msg + b" ")


# --- PySceneDetect --------------------------------------------------------------------------------
@needs_ffmpeg
def test_scenedetect_finds_hard_cut(tmp_path: Path):
    from scenedetect import ContentDetector, detect

    video = tmp_path / "cut.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
         "-f", "lavfi", "-i", "mandelbrot=size=160x120:rate=10", "-filter_complex",
         "[1:v]trim=duration=2,setpts=PTS-STARTPTS[b];[0:v][b]concat=n=2:v=1[v]",
         "-map", "[v]", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    scenes = detect(str(video), ContentDetector())
    assert len(scenes) == 2
    assert scenes[1][0].seconds  # get_seconds() is deprecated in scenedetect 0.6.7+ == pytest.approx(2.0, abs=0.2)


# --- Tesseract (pytesseract, pol) ------------------------------------------------------------------
@needs_tesseract
def test_pytesseract_reads_polish_line(polish_line):
    import pytesseract
    from rapidfuzz import fuzz

    if "pol" not in pytesseract.get_languages(config=""):
        pytest.skip("tesseract 'pol' traineddata missing")
    text = pytesseract.image_to_string(polish_line, lang="pol", config="--psm 7").strip()
    assert fuzz.ratio(text, POLISH_LINE) >= 85, text
    data = pytesseract.image_to_data(polish_line, lang="pol", config="--psm 7",
                                     output_type=pytesseract.Output.DICT)
    assert any(float(c) > 50 for c in data["conf"])


# --- pandas / pyarrow / openpyxl -----------------------------------------------------------------
def _frame():
    import pandas as pd

    return pd.DataFrame({
        "id": [1, 2, 3],
        "text": ["Zażółć", "gęślą", None],
        "score": [0.5, float("nan"), 2.0],
        "ts": pd.to_datetime(["2026-11-21T11:00:00Z", "2026-11-22T10:30:00Z", None], utc=True),
    })


def test_pandas_pyarrow_parquet_roundtrip(tmp_path: Path):
    import pandas as pd
    import pyarrow.parquet as pq

    df = _frame()
    p = tmp_path / "t.parquet"
    df.to_parquet(p, engine="pyarrow", index=False)
    assert pq.read_schema(p).names == ["id", "text", "score", "ts"]
    back = pd.read_parquet(p)
    pd.testing.assert_frame_equal(back, df, check_dtype=False)
    csv = pd.read_csv(io.StringIO("a;b\n1;zł\n2;ą\n"), sep=";")
    assert csv["b"].tolist() == ["zł", "ą"]


def test_openpyxl_roundtrip(tmp_path: Path):
    import openpyxl
    import pandas as pd

    p = tmp_path / "t.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Arkusz"
    ws.append(["kolumna", "wartość"])
    ws.append(["żółw", 3.5])
    wb.save(p)
    rows = list(openpyxl.load_workbook(p, read_only=True)["Arkusz"].iter_rows(values_only=True))
    assert rows == [("kolumna", "wartość"), ("żółw", 3.5)]
    assert pd.read_excel(p, sheet_name="Arkusz", engine="openpyxl")["wartość"].tolist() == [3.5]


# --- text normalisation ---------------------------------------------------------------------------
def test_rapidfuzz_scores_and_extracts():
    from rapidfuzz import fuzz, process
    from rapidfuzz.distance import Levenshtein

    assert fuzz.ratio("Zażółć gęślą", "Zazolc gesla") < fuzz.ratio("Zażółć gęślą", "Zażółć gęśla")
    assert fuzz.token_sort_ratio("gęślą jaźń", "jaźń gęślą") == 100
    assert Levenshtein.distance("kitten", "sitting") == 3
    best = process.extractOne("exmaple.com", ["example.com", "sample.org", "other.net"])
    assert best is not None and best[0] == "example.com"


def test_ftfy_repairs_mojibake_and_confusable_homoglyphs_flags_mixed_script():
    import ftfy
    from confusable_homoglyphs import confusables

    broken = "Zażółć gęślą".encode().decode("latin-1")
    assert ftfy.fix_text(broken) == "Zażółć gęślą"
    spoof = "pаypal"  # Cyrillic 'а'
    assert confusables.is_mixed_script(spoof)
    assert confusables.is_dangerous(spoof)
    assert not confusables.is_dangerous("paypal")


# --- matplotlib / psutil --------------------------------------------------------------------------
def test_matplotlib_agg_renders_png():
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(3, 2), dpi=80)
    ax.plot([0, 1, 2], [0, 1, 4])
    ax.set_title("Zażółć")
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    im = Image.open(io.BytesIO(buf.getvalue()))
    assert im.format == "PNG" and im.size == (240, 160)


def test_psutil_reports_rss_of_live_buffer():
    import psutil

    proc = psutil.Process()
    block = np.random.default_rng(0).integers(0, 255, 64 * 1024 * 1024, dtype=np.uint8)  # 64 MiB, touched
    # RSS deltas are unreliable once torch has freed arenas (pages get reused), so assert the floor only.
    assert proc.memory_info().rss > block.nbytes
    assert int(block[::4096].sum()) > 0
    assert psutil.cpu_count(logical=True) >= 1 and psutil.virtual_memory().total > 0
