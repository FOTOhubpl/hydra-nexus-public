# MODEL_REGISTRY

Verified 2026-11-20 (Fri, environment prep before the task start); faster-whisper-small and mediapipe-face-landmarker added to `scripts/fetch_models.py` 2026-11-21 (docs/PORTABILITY.md). All weights and reference data live outside the repo
(`$HYDRA_MODELS_DIR`, `$HYDRA_DATA_DIR/external`, `$HYDRA_CACHE_DIR`). Every byte is pinned in
[`models.lock.json`](../models.lock.json): source, immutable revision, license, and size + sha256 for each file.

| | |
|---|---|
| Fetch (network, idempotent) | `HYDRA_OFFLINE=0 . scripts/env.sh && uv run python scripts/fetch_models.py --with-base` |
| Verify (offline) | `. scripts/env.sh && uv run python scripts/fetch_models.py --verify` (re-hash + one isolated smoke test per key) |
| Lock sha256 | `1be9e0112d40f76c772f68e0041f94e7617ef658b7853680441c8d6748386a9b`: byte-identical on the laptop and on `the Linux server` |
| How the server copy was made | independent download from the same pinned sources (not rsync), so it reproduced the same bytes. Only the CERT snapshot was rsynced, because that list changes every few minutes. |
| Total on disk | 1.72 GB per machine (models 1.68 GB + external data 43 MB) |
| Verify reports | `$HYDRA_MODELS_DIR/verify-darwin-arm64.json`, `$HYDRA_MODELS_DIR/verify-linux-x86_64.json` |

Rules the script enforces:
- Downloads only from pinned revisions.
- Fetches only safetensors, ONNX, TFLite bundles, config and tokenizer files. It never fetches `.bin`, `.pt` or pickle files, and it refuses a model if one is present; the one exception is the CTranslate2 `model.bin` of `faster-whisper-small`, allowed by name (2026-11-21).
- No `trust_remote_code`. It also refuses a model whose `config.json` has an `auto_map`.
- Checks each HF file against the Hub tree metadata: the LFS sha256, or the git blob sha1 for small files.
- Runs the smoke tests with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` and a socket guard. If a library tries to download something, the test fails instead of the download succeeding.

## Models

RAM is the peak RSS of the isolated smoke-test process, including Python, torch and onnxruntime. `load` includes the library import and the first call. `inf` is a single warm call. Torch tests use 8 threads.

| key | ID / source | revision | license | sha256 (main file) | size | backend | RAM mac / srv | smoke laptop (load / inf) | smoke server (load / inf) | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| `dinov2-small` | `facebook/dinov2-small` (HF) | `ed25f3a31f01` | Apache-2.0 | `ae1e99fcefd5` model.safetensors | 84.2 MB | transformers `AutoModel` + `AutoImageProcessor` | 453 / 570 MB | ok 1.5 s / 19 ms | ok 6.0 s / 26 ms | `pooler_output` (1, 384). Works without torchvision. |
| `e5-small` | `intfloat/multilingual-e5-small` (HF) | `614241f622f5` | MIT | `1a55775f5344` model.safetensors | 470 MB | sentence-transformers | 944 / 985 MB | ok 2.6 s / 8 ms | ok 7.2 s / 10 ms | (2, 384). Needs the `query: ` / `passage: ` prefixes. cos(q,p)=0.896 |
| `e5-base` | `intfloat/multilingual-e5-base` (HF) | `d12875059715` | MIT | `a18a44fad1d0` model.safetensors | 1.08 GB | sentence-transformers | 1186 / 1222 MB | ok 2.7 s / 19 ms | ok 6.0 s / 21 ms | (2, 768). Fetched with `--with-base` on both machines (laptop had 28 GB free). |
| `yunet` | `opencv/face_detection_yunet` (HF, official OpenCV org) | `3cc26e7f1014` | MIT | `8f2383e4dd3c` face_detection_yunet_2023mar.onnx | 228 KB | `cv2.FaceDetectorYN` | 66 / 84 MB | ok 18 ms / 2 ms | ok 64 ms / 3 ms | sha256 = git-lfs oid in `opencv/opencv_zoo@48b10fa5` (real ONNX, not an LFS pointer). Synthetic image, so 0 faces is expected. |
| `sface` | `opencv/face_recognition_sface` (HF, official OpenCV org) | `3d7082438a6e` | Apache-2.0 | `0ba9fbfa01b5` face_recognition_sface_2021dec.onnx | 36.9 MB | `cv2.FaceRecognizerSF` | 212 / 195 MB | ok 50 ms / 4 ms | ok 178 ms / 2 ms | feature (1, 128). sha256 = lfs oid in `opencv_zoo@48b10fa5` |
| `faster-whisper-small` | `Systran/faster-whisper-small` (HF) | `536b0662742c` | MIT | `3e305921506d` model.bin (CTranslate2) | 464 MB | faster-whisper 1.2.1 / CTranslate2 int8 CPU | - / 728 MB | - | ok 2.2 s / 1.65 s (2 s synthetic tone, native); container 2.0 s / 1.75 s | added 2026-11-21 ; `model.bin` is a CTranslate2 flat tensor file, not pickle, allowed by name for this key only (`allow_bin`); read by hydra/genome/aud.py from `$HYDRA_MODELS_DIR/faster-whisper-small` |
| `mediapipe-face-landmarker` | Google MediaPipe Face Landmarker (`face_landmarker.task`, float16 v1), https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task | float16/1 | Apache-2.0 per the MediaPipe model card [verify the card text before citing] | `64184e229b26` (full: 64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff) | 3.76 MB | mediapipe 1.2.3 Tasks API `vision.FaceLandmarker` (478 points incl. iris 468/473) | n/m | ok 1.40 s / 3 ms (256x256 grey image, 0 faces, expected) | not fetched | Fetched manually 2026-11-21 17:37 to `$HYDRA_MODELS_DIR/mediapipe/` with curl; NOT yet in `scripts/fetch_models.py` / `models.lock.json`. Used only by `hydra/genome/prov.py` (StyleGAN eye-geometry suspicion gene, 0 linking bits); without the file the extractor falls back to YuNet eye landmarks. mediapipe 1.2.3 has no `mp.solutions` module, so the legacy FaceMesh API is unavailable. |
| `rapidocr` | ModelScope `RapidAI/RapidOCR` | tag `v3.10.0` | Apache-2.0 | det `4d97c44a20d3`, cls `54379ae5174d`, rec `b20bd37c168a` | 13.1 MB | RapidOCR 3.10.0 + onnxruntime | 1061 / 642 MB | ok 0.55 s / 281 ms (match 0.84) | ok 1.5 s / 629 ms (match 0.81) | Requested set: PP-OCRv5 det mobile + PP-LCNet textline cls + **latin** rec. Read `Zazótć gęśla jań 2026`: it drops ż/ą/ź. |
| `rapidocr-v6` | ModelScope `RapidAI/RapidOCR` | tag `v3.10.0` | Apache-2.0 | det `090f04abcd9d`, cls `e47acedf6632`, rec `6f327246b503` | 30.3 MB | RapidOCR 3.10.0 + onnxruntime | 1066 / 672 MB | ok 0.54 s / 289 ms (match 0.96) | ok 1.4 s / 639 ms (match 0.96) | **Recommended for Polish.** rapidocr 3.10.0's own default (PP-OCRv6 small, multilingual incl. `pl`), the same bytes the wheel bundles. Read `Zażółć gęślą jaźń 2 2026`. |
| `mediapipe-face-landmarker` | `face_landmarker.task` float16 v1, https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task (MediaPipe Tasks) | URL version `float16/1` | Apache-2.0 (model card, as recorded in `mvp-lab/research/deepfake/02_video.md`; MediaPipe code Apache-2.0) | `64184e229b26` face_landmarker.task (full: `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff`) | 3.76 MB | `mediapipe` 1.2.3 Tasks `FaceLandmarker` (VIDEO mode) | not measured | ok, 20 blank 480x360 frames in 33 ms, 0 faces (expected) (2026-11-21, `.scratch/mp_smoke.py`, not in git) | not run | Added 2026-11-21 by for `vid_avsync`. Not in `scripts/fetch_models.py`: fetched by `hydra.genome.vid_avsync.fetch_landmarker` only when `HYDRA_MODEL_DOWNLOAD=1` (sha256-checked); missing file = gene `unavailable`. |

The URLs and sha256 for both RapidOCR sets come from `rapidocr==3.10.0` `default_models.yaml` (`onnxruntime.PP-OCRv5` / `PP-OCRv6`). The OCR test renders `Zażółć gęślą jaźń 2026` in a system font: Arial on macOS, DejaVuSans on Linux. Pillow's bundled font has no Polish glyphs.

On the 64-vCPU server, onnxruntime's default (`intra_op_num_threads=-1`) is not the fastest setting. Median times per line:

| `intra_op_num_threads` | v5 | v6 |
|---|---|---|
| -1 (default) | 582 ms | 626 ms |
| 8 | 478 ms | 603 ms |
| 16 | 401 ms | 465 ms |

For throughput, run several worker processes with about 8–16 threads each. Set it with `"EngineConfig.onnxruntime.intra_op_num_threads": 16`.

### ASR (AUD stream, 2026-11-21)

| key | ID / source | revision | license | sha256 (main file) | size | backend | smoke laptop | notes |
|---|---|---|---|---|---|---|---|---|
| `faster-whisper-small` | `Systran/faster-whisper-small` (HF) | `536b0662742c` | MIT | `3e305921506d` model.bin | 483.5 MB (+ tokenizer.json, vocabulary.txt, config.json) | faster-whisper 1.2.1 / CTranslate2, CPU int8 | measured on the NASK sample (internal notes, not published) | Not in `models.lock.json` yet: fetched by `faster_whisper.download_model` only when `HYDRA_MODEL_DOWNLOAD=1` (`hydra/genome/aud.py`). `model.bin` is CTranslate2's own serialisation, not a pickle. Without it ASR is `unavailable` (unknown, no penalty). |

Speaker embeddings (SpeechBrain ECAPA, VoxCeleb-trained) are deliberately not registered: internal notes, not published

### DFK suspicion genes (2026-11-21)

| key | ID / source | revision | license | sha256 (main file) | size | backend | smoke server | notes |
|---|---|---|---|---|---|---|---|---|
| `audioseal-detector` | `facebook/audioseal` (HF), file `detector_base.pth` converted to `detector_base.safetensors` | `3c19eba53390` | MIT (code and weights; training data VoxPopuli, CC0, per the AudioSeal paper) | source `.pth` `8a78e8a83584` (full `8a78e8a83584113523e161fc599fcab10fd0e94c04d2eb9d2fa1e9ec91ab69d9`, 34,667,641 B); converted `449efba12e01` (full `449efba12e01c194ac526c0c7dfd062bfe9fe5a6799273d13d78e174926cf1f9`, 34,593,512 B) | 33.0 MB | `hydra.vendor.audioseal` detector (PyTorch, CPU) | ok: load 1.63 s, 1 s of silence 60 ms, marked fraction 0.000, peak RSS 303 MB (`fetch_models.py --verify --only audioseal-detector`, the Linux server, 2026-11-21) | Upstream ships only a pickle (with an omegaconf config). `fetch_models.py` (kind `url_convert`) downloads it at the pinned commit, checks its sha256, reads it with the allow-list unpickler (no callable outside torch tensor rebuilding is resolved), folds weight norm in float64 and writes safetensors with constant metadata; the `.pth` is deleted. Conversion was byte-identical in two runs. The runtime loads only the safetensors file. Used by `hydra/genome/dfk.py::dfk_audio_wm`; missing file = gene `unavailable`; `HYDRA_MODEL_DOWNLOAD=1` lets the extractor fetch it itself. |
| `dinov2-small` (reused) | see above | - | Apache-2.0 | - | - | transformers `AutoModel` | - | Also the backbone of the training-free `dfk_image` and `dfk_video` statistics (no extra weights). |

Not registered (evaluation only, on the server under `~/HydraNexus/research2/deepfake/models/`): `facebook/dinov2-base`, `facebook/dinov2-large` (Apache-2.0; backbone sweep), AudioSeal `generator_base.pth` (MIT; used to embed marks in the test audio), AASIST-L (clovaai/aasist, in-repo weights, ASVspoof 2019 LA).

## External reference data

| key | source | revision | license / terms | sha256 | size | location | smoke laptop / server |
|---|---|---|---|---|---|---|---|
| `cert-domains` | https://hole.cert.pl/domains/v2/domains.json | snapshot 2026-11-20T18:58:20Z (HTTP Last-Modified 18:55:10 GMT) | free for any use, no attribution required; must not imply endorsement by CERT Polska | `bd904e8db931` | 21.5 MB (126,730 rows, 126,684 active) | `$HYDRA_DATA_DIR/external/cert/`: `domains.json`, `domains_20261120T185820Z.json`, `SNAPSHOT.json`, `SHA256SUMS` | ok / ok |
| `psl` | `publicsuffix/list` `public_suffix_list.dat` | `d7d7f1fd5516` | MPL-2.0 | `3084c6ca4ac1` | 335 KB | `$HYDRA_DATA_DIR/external/psl/` and the seeded tldextract cache | ok / ok: `sub.przyklad.com.pl` → `('sub', 'przyklad', 'com.pl')` |

The CERT list is a living list. The script keeps one snapshot and re-uses it on later runs, so it is idempotent. `--refresh-external` takes a new timestamped snapshot and appends it to `SHA256SUMS`. To keep both machines identical, rsync `data/external/cert/` to the other machine.

## RapidOCR offline load (exact params)

With `model_path` set, RapidOCR does not resolve or download anything. Enum-typed keys must be enums, not strings. Both rec ONNX files embed their character list (ONNX metadata `character`), so `rec_keys_path` is not needed. This is the same code as `rapidocr_params` in `scripts/fetch_models.py`.

```python
import os
from pathlib import Path
from rapidocr import RapidOCR, EngineType, LangRec, ModelType, OCRVersion

M = Path(os.environ["HYDRA_MODELS_DIR"])
ORT = EngineType.ONNXRUNTIME

# requested set: PP-OCRv5 det + textline cls + latin rec   ($HYDRA_MODELS_DIR/rapidocr/)
d = M / "rapidocr"
ocr_v5 = RapidOCR(params={
    "Global.model_root_dir": str(d),          # keeps RapidOCR from writing into site-packages
    "Det.engine_type": ORT, "Det.ocr_version": OCRVersion.PPOCRV5, "Det.model_type": ModelType.MOBILE,
    "Det.model_path": str(d / "ch_PP-OCRv5_det_mobile.onnx"),
    "Cls.engine_type": ORT, "Cls.ocr_version": OCRVersion.PPOCRV5, "Cls.model_type": ModelType.MOBILE,
    "Cls.model_path": str(d / "ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx"),   # PPOCRV5 => 80x160 cls input
    "Rec.engine_type": ORT, "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE,
    "Rec.lang_type": LangRec.LATIN,
    "Rec.model_path": str(d / "latin_PP-OCRv5_rec_mobile.onnx"),
})

# recommended for Polish: PP-OCRv6 small + v2.0 cls   ($HYDRA_MODELS_DIR/rapidocr-v6/)
d6 = M / "rapidocr-v6"
ocr_v6 = RapidOCR(params={
    "Global.model_root_dir": str(d6),
    "Det.engine_type": ORT, "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": ModelType.SMALL,
    "Det.model_path": str(d6 / "PP-OCRv6_det_small.onnx"),
    "Cls.engine_type": ORT, "Cls.ocr_version": OCRVersion.PPOCRV4, "Cls.model_type": ModelType.MOBILE,
    "Cls.model_path": str(d6 / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),           # PPOCRV4 => 48x192 cls input
    "Rec.engine_type": ORT, "Rec.ocr_version": OCRVersion.PPOCRV6, "Rec.model_type": ModelType.SMALL,
    "Rec.model_path": str(d6 / "PP-OCRv6_rec_small.onnx"),
})
result = ocr_v6(image_bgr_ndarray_or_path); print(result.txts, result.scores)
```

## tldextract offline recipe

`fetch_models.py` downloads the PSL at a pinned commit. It parses the PSL with tldextract's own parser and writes the result into tldextract's disk cache. The cache entry uses the key that `TLDExtract(suffix_list_urls=)` looks up: namespace `publicsuffix.org-tlds`, key `{"urls": , "fallback_to_snapshot": True/False}`. Both values of `fallback_to_snapshot` are written. The cache files are in `$HYDRA_CACHE_DIR/tldextract/publicsuffix.org-tlds/*.tldextract.json`, with the same bytes on both platforms. They are recorded under `psl.derived` in the lock.

```python
import os, tldextract
extract = tldextract.TLDExtract(
    cache_dir=os.path.join(os.environ["HYDRA_CACHE_DIR"], "tldextract"),
    suffix_list_urls=,            # never touches the network
    fallback_to_snapshot=False,     # fail loudly instead of silently using the older bundled snapshot
)
extract("sub.przyklad.com.pl")      # ExtractResult(subdomain='sub', domain='przyklad', suffix='com.pl', ...)
```

The cache key depends on tldextract internals (5.4.0). If tldextract is upgraded, re-run the fetch to re-seed the cache. Another option that does not depend on the cache layout is `suffix_list_urls=("file://" + os.environ["HYDRA_DATA_DIR"] + "/external/psl/public_suffix_list.dat",)`.

## Deliberately not downloaded

| item | why |
|---|---|
| Bielik GGUF (LLM) | 6.7 GB, P2. Not authorized for this pass. |
| `pytorch_model.bin` for dinov2 / e5 | pickle format, so excluded by policy. The safetensors copies are used instead. |
| e5 `onnx/` and `openvino/` exports, YuNet/SFace `int8`/`int8bq` variants | not needed for the CPU baseline. Same pinned revisions, so they can be added to `allow` later. |
| `.eval_results/`, README model cards | not runtime files. The licenses come from the Hub metadata. YuNet/SFace `LICENSE` files are stored next to the weights. |
