# Third-party code copied into this repository

Each entry: project, URL, commit, licence, files, modifications. Dependencies installed via uv/pnpm are listed in docs/LICENSES.md instead.

| Project | URL @ commit | Licence | Files in this repo | Modified |
|---|---|---|---|---|
| invisible-watermark (ShieldMnt) | https://github.com/ShieldMnt/invisible-watermark @ 68d0376d94a4701ed240af0841ec12e00676e325 | MIT (Copyright (c) 2021 ShieldMnt; full text in the file header and `hydra/vendor/imwatermark/LICENSE`) | hydra/vendor/imwatermark/__init__.py (from imwatermark/maxDct.py, DWT-DCT path only) | yes (2026-11-21): SVD/RivaGAN paths removed, snake_case, decode loop vectorised with numpy (same block order and bit rule, equality test in tests/genes_prov), per-bit scores exposed; encoder kept for synthetic test images only |
| AudioSeal (Meta FAIR) | https://github.com/facebookresearch/audioseal @ 1496f7662d6aa625602bb0dcd1b4997b63b47609 | MIT (Copyright (c) Meta Platforms, Inc. and affiliates; full text in `hydra/vendor/audioseal/LICENSE` and the header of `hydra/vendor/audioseal/__init__.py`) | hydra/vendor/audioseal/conv.py, lstm.py, seanet.py (from src/audioseal/libs/audiocraft/modules/, themselves vendored by AudioSeal from facebookresearch/audiocraft, MIT); hydra/vendor/audioseal/__init__.py (`build_detector` follows class AudioSealDetector of src/audioseal/models.py and cards/audioseal_detector_16bits.yaml) | yes (2026-11-21): import paths and a file-level ruff exemption in the three module files; detector-only subset with weight norm folded (`fold_weight_norm`); the allow-list checkpoint reader and the safetensors conversion are our own code |

## Runtime dependencies added with a notice (not copied; installed via uv, listed in uv.lock and docs/LICENSES.md)

| Project | URL, version | Licence | Used by | Notice |
|---|---|---|---|---|
| schwifty | https://github.com/mdomke/schwifty, 2026.11.3 (PyPI) | MIT, "Copyright (c) 2021 Martin Domke" | `hydra/genome/operator_ids.py`: IBAN validation and the bank registry (bank code, bank name; PL registry from NBP data shipped in the package) | MIT text in the installed `schwifty-*.dist-info/licenses/LICENSE` |
| phonenumbers (Python port of Google libphonenumber) | https://github.com/daviddrysdale/python-phonenumbers, 9.0.44 (PyPI) | Apache-2.0 | `hydra/genome/operator_ids.py`: E.164 normalisation, number type, short and emergency numbers | Apache-2.0 text in `phonenumbers-*.dist-info/licenses/LICENSE`; the package ships no separate NOTICE file |
| PyYAML | https://github.com/yaml/pyyaml, 6.0.3 (PyPI) | MIT, "Copyright (c) 2017-2021 Ingy döt Net, Copyright (c) 2006-2016 Kirill Simonov" | `hydra/genome/kit_rules.py`: loads HYDRA-owned kit-rule YAML | MIT text in `pyyaml-*.dist-info/licenses/LICENSE` |
| pycountry (transitive, required by schwifty) | https://github.com/pycountry/pycountry, 26.2.16 | LGPL-2.1-only | imported by schwifty only; unmodified, installed as a separate library, never copied into this repository | LGPL-2.1: users may replace the installed library |
| rstr (transitive, required by schwifty) | https://github.com/leapfrogonline/rstr, 4.0.0 | BSD-3-Clause | imported by schwifty only | licence in its dist-info |

Algorithms implemented from public specifications (own code, no third-party code copied): Keccak-256 (FIPS 202
permutation with the original Keccak padding), Base58Check, Bech32/Bech32m (BIP-173, BIP-350), EIP-55, Monero Base58,
IBAN mod-97 (ISO 13616), Polish bank sort-code check digit.

## Model weights and reference data redistributed in the full image (`ghcr.io/fotohubpl/hydra-nexus:full`)

The slim image and this repository contain no weights. The full image (Dockerfile target `full`) contains these
public files, unmodified (AudioSeal: the detector converted to safetensors, source and result sha256 in
`models.lock.json`), fetched at build time at the pinned revisions and checked against the sha256 in
`models.lock.json` (`scripts/pin_check.py`). Inside the image: `/opt/hydra/models` and `/opt/hydra/data/external`;
licence and attribution files in `/opt/hydra/models/LICENSES` (this file, `docs/MODELS.md`, `docs/LICENSES.md`,
`MODEL_LICENCES.txt`, and the `SOURCE.md`/licence files of each reference-data set, including the MPL-2.0 text of the
Public Suffix List). The CERT Polska list is the snapshot in the v1.0.0 reference-data bundle; it may be used freely,
and its use does not imply endorsement by CERT Polska.

| Entry | Source | Revision | Licence |
|---|---|---|---|
| `dinov2-small` | https://huggingface.co/facebook/dinov2-small/tree/ed25f3a31f01632728cabb09d1542f84ab7b0056 | ed25f3a31f01632728cabb09d1542f84ab7b0056 | Apache-2.0 |
| `e5-small` | https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3 | 614241f622f53c4eeff9890bdc4f31cfecc418b3 | MIT |
| `e5-base` | https://huggingface.co/intfloat/multilingual-e5-base/tree/d128750597153bb5987e10b1c3493a34e5a4502a | d128750597153bb5987e10b1c3493a34e5a4502a | MIT |
| `yunet` | https://huggingface.co/opencv/face_detection_yunet/tree/3cc26e7f1014a5ee5d74a42acee58bafc9d0a310 | 3cc26e7f1014a5ee5d74a42acee58bafc9d0a310 | MIT |
| `sface` | https://huggingface.co/opencv/face_recognition_sface/tree/3d7082438a6e4551e840c9b2bb60b71e8da4b524 | 3d7082438a6e4551e840c9b2bb60b71e8da4b524 | Apache-2.0 |
| `faster-whisper-small` | https://huggingface.co/Systran/faster-whisper-small/tree/536b0662742c02347bc0e980a01041f333bce120 | 536b0662742c02347bc0e980a01041f333bce120 | MIT |
| `mediapipe-face-landmarker` | https://storage.googleapis.com/mediapipe-models/face_landmarker | float16/1 | Apache-2.0 (MediaPipe model card: Face Landmarker) |
| `audioseal-detector` | https://huggingface.co/facebook/audioseal/tree/3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba | 3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba | MIT (code and weights, facebook/audioseal model card; training data VoxPopuli, CC0) |
| `rapidocr` | https://www.modelscope.cn/models/RapidAI/RapidOCR | v3.10.0 | Apache-2.0 (PaddleOCR PP-OCRv5 weights, ONNX conversion by RapidAI) |
| `rapidocr-v6` | https://www.modelscope.cn/models/RapidAI/RapidOCR | v3.10.0 | Apache-2.0 (PaddleOCR PP-OCRv6/v2.0 weights, ONNX conversion by RapidAI) |
| `cert-domains` | https://hole.cert.pl/domains/v2/domains.json | snapshot of the v1.0.0 reference-data bundle (2026-11-22; `models.lock.json` pins the 2026-11-20 snapshot used during development) | CERT Polska warning list: free for any use, attribution not required; must not imply endorsement by CERT Polska |
| `psl` | https://raw.githubusercontent.com/publicsuffix/list/d7d7f1fd55166aaf73ad2f526e72634490acd9c8/public_suffix_list.dat | d7d7f1fd55166aaf73ad2f526e72634490acd9c8 | MPL-2.0 |
| `misp-warninglists` | https://github.com/MISP/misp-warninglists | 40e2b570fda2e5443a2d6fa1d1db16c37ab0a956 | CC0-1.0 (X4BNet lists inside: MIT) |
