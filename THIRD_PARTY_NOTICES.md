# Third-party code copied into this repository

Each entry: project, URL, commit, licence, files, modifications. Dependencies installed via uv/pnpm are listed in docs/LICENSES.md instead.

| Project | URL @ commit | Licence | Files in this repo | Modified |
|---|---|---|---|---|
| hallmark (skill) | https://github.com/nutlope/hallmark | MIT | .claude/skills/hallmark/** | tests/fixtures excluded |
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
