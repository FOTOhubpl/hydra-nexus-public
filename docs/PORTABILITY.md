# Portability and jury runbook

Status 2026-11-21 (Europe/Warsaw); image slimming, `gui-ports`, CPU budget and the
re-measured jury flow from are in section 10. Measured on `the Linux server` (Ubuntu x86_64, 64 vCPU,
no GPU, Docker 29.10.0, Compose v5.8.0) from a fresh clone with an empty models directory, empty Docker build cache
(`docker compose build --no-cache --pull`) and an empty uv cache. Numbers marked *measured* come from those runs;
the commands are in the runbook below. Polish jury instructions: README, section "Uruchomienie przez jury (offline)".

## 1. What the jury needs

| Item | Requirement | Note |
|---|---|---|
| Docker path | Docker Engine 24+ with Compose v2 (measured on 29.10.0 / v5.8.0), Linux x86_64 | macOS/Windows: Docker Desktop; the offline `cli` profile needs no host networking; GUI: service `api` (`network_mode: host`, Linux) or service `gui` (profile `gui-ports`, bridge network, port published on 127.0.0.1 only; for Docker Desktop) |
| Native path | Python 3.11 exactly (`requires-python >=3.11,<3.12`), uv, Node 24 + pnpm 12 for the UI build | lock covers linux x86_64 and macOS arm64 only (`[tool.uv] environments`) |
| CPU / RAM | 8 cores, 16 GB RAM recommended | CPU only; peak RSS per model in section 3 |
| Disk | image 4.3 GB unpacked, 1.01 GB compressed (*measured*, multi-stage since; was 9.66 / 2.21 GB); models 1.2 GB + reference data 61 MB (*measured*); state ~1.5x the input size | |
| Network | once, for `docker compose build` and the model download; none afterwards | runtime `cli` profile runs with `network_mode: none` |

## 2. Python dependencies (direct)

Licence from installed package metadata; wheel availability read from `uv.lock` (cp311 wheels per platform). Windows is supported only through Docker (no Windows
environment in the lock). Full transitive list with licences: `docs/LICENSES.md`.

| package | locked version | licence | linux x86_64 cp311 | macOS arm64 cp311 |
|---|---|---|---|---|
| fastapi | 0.165.0 | MIT | pure | pure |
| uvicorn | 0.54.0 | BSD-3-Clause | pure | pure |
| pydantic / pydantic-settings | 2.14.4 / 2.16.3 | MIT | pure | pure |
| python-multipart | 0.0.34 | Apache-2.0 | pure | pure |
| typer | 0.29.2 | MIT | pure | pure |
| orjson | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | wheel | wheel |
| pillow | 13.0.0 | MIT-CMU | wheel | wheel |
| opencv-python | 5.1.0.95 | Apache-2.0 | wheel (needs libGL, libglib) | wheel |
| filetype | 1.2.0 | MIT | pure | pure |
| scenedetect | 0.7.3 | BSD-3-Clause | pure | pure |
| imagehash | 4.3.2 | BSD-2-Clause | pure | pure |
| **pdqhash** | 0.2.8 | MIT | **no wheel: sdist build, needs g++** | wheel |
| numpy | 2.4.6 | BSD-3-Clause and others | wheel | wheel |
| scikit-learn | 1.9.1 | BSD-3-Clause | wheel | wheel |
| datasketch | 2.0.0 | MIT | pure | pure |
| networkx | 3.6.1 | BSD-3-Clause | pure | pure |
| pytesseract | 0.3.13 | Apache-2.0 | pure (needs the tesseract binary) | pure |
| rapidocr | 3.10.0 | Apache-2.0 | pure | pure |
| onnxruntime | 1.32.0 | MIT | wheel | wheel |
| tldextract | 5.4.0 | BSD-3-Clause | pure | pure |
| lxml | 6.1.3 | BSD-3-Clause | wheel | wheel |
| stix2 | 3.0.2 | BSD-3-Clause | pure | pure |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | wheel | wheel |
| torch | 2.15.0 (+cpu on Linux) | BSD-3-Clause and bundled permissive | wheel from the PyTorch CPU index | wheel from PyPI |
| transformers | 5.23.0 | Apache-2.0 | pure | pure |
| sentence-transformers | 6.3.0 | Apache-2.0 | pure | pure |
| huggingface-hub | 2.4.0 | Apache-2.0 | pure | pure |
| safetensors | 0.8.0 | Apache-2.0 | wheel | wheel |
| pandas | 3.1.1 | BSD-3-Clause | wheel | wheel |
| pyarrow | 26.0.1 | Apache-2.0 | wheel | wheel |
| openpyxl | 3.1.5 | MIT | pure | pure |
| rapidfuzz | 3.14.6 | MIT | wheel | wheel |
| ftfy | 6.3.1 | Apache-2.0 | pure | pure |
| confusable-homoglyphs | 3.3.1 | MIT | pure | pure |
| beautifulsoup4 | 4.15.0 | MIT | pure | pure |
| matplotlib | 3.11.2 | PSF-based (matplotlib licence) | wheel | wheel |
| psutil | 7.2.2 | BSD-3-Clause | wheel | wheel |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | pure | pure |
| graspologic-native | 1.3.1 | MIT | wheel | wheel |
| anthropic[bedrock] | 2.12.0 | MIT | pure | pure (only for the optional, off-by-default LLM layer) |
| **py-tlsh** | 5.0.0 | Apache-2.0 or BSD | **no wheel: sdist build, needs g++** | **sdist build, needs Xcode CLT** |
| faster-whisper | 1.2.1 | MIT | pure (ctranslate2 wheel) | pure |
| mediapipe | 1.2.3 | Apache-2.0 | wheel (**needs libEGL.so.1, libGLESv2**) | wheel |
| c2pa-python | 0.39.0 | MIT OR Apache-2.0 | wheel | wheel |

`vpdq` is not a dependency (not used). UI: React 19, sigma 4, graphology, TanStack Query, react-router (all MIT);
build tools Vite 8, TypeScript, Tailwind 4 (MIT/Apache-2.0); locked in `ui/pnpm-lock.yaml`, built in the
Docker `ui` stage, served by the API from `ui/dist`.

## 3. Models and reference data

All are fetched by one explicit step (`scripts/fetch_models.py`; Docker: `docker compose --profile models run --rm
models`; native: `just models-fetch`) into `$HYDRA_MODELS_DIR` (default `~/HydraNexus/models`) and pinned in
`models.lock.json` (revision + sha256 of every file). No extractor downloads anything at run time unless
`HYDRA_MODEL_DOWNLOAD=1` is set explicitly. Load/inference/peak RSS: `fetch_models.py --verify` on
the Linux server, offline with a socket guard (*measured* 2026-11-21).

| key | source, pinned revision | licence | size | used by | if missing | verify (load ms / infer ms / peak RSS) |
|---|---|---|---|---|---|---|
| dinov2-small | HF facebook/dinov2-small @ ed25f3a3 | Apache-2.0 | 84 MB | VIS embeddings, candidates | VIS embedding genes unavailable | 14938 / 56 / 508 MB |
| e5-small | HF intfloat/multilingual-e5-small @ 614241f6 | MIT | 470 MB | TXT semantic | TXT semantic genes unavailable | 15049 / 17 / 999 MB |
| yunet | HF opencv/face_detection_yunet @ 3cc26e7f (= opencv_zoo 48b10fa5) | MIT | 0.2 MB | ID face detection | ID genes unavailable | 1038 / 4 / 93 MB |
| sface | HF opencv/face_recognition_sface @ 3d708243 (= opencv_zoo 48b10fa5) | Apache-2.0 | 37 MB | ID face embedding | ID genes unavailable | 331 / 4 / 200 MB |
| faster-whisper-small | HF Systran/faster-whisper-small @ 536b0662 (CTranslate2 `model.bin`, not pickle) | MIT | 464 MB | AUD ASR transcript | ASR genes unavailable | 2237 / 1647 / 728 MB |
| mediapipe-face-landmarker | storage.googleapis.com mediapipe-models face_landmarker float16/1, sha256 64184e22 | Apache-2.0 | 3.6 MB | vid_avsync, prov avatar geometry | av-sync / avatar genes unavailable | needs libEGL (see §4) |
| rapidocr | ModelScope RapidAI/RapidOCR v3.10.0 (PP-OCRv5 mobile, latin rec) | Apache-2.0 | 13 MB | OCR | OCR falls back to tesseract | 4121 / 2070 / 633 MB |
| rapidocr-v6 | ModelScope RapidAI/RapidOCR v3.10.0 (PP-OCRv6 small) | Apache-2.0 | 30 MB | OCR (default) | OCR falls back | 3333 / 2113 / 667 MB |
| cert-domains | CERT Polska warning list (living list, timestamped snapshot) | free use, no endorsement | 22 MB | INF CERT match | CERT gene unavailable | ok |
| psl | publicsuffix/list @ d7d7f1fd | MPL-2.0 | 0.3 MB | domain parsing (tldextract offline cache) | tldextract bundled snapshot | ok |
| misp-warninglists | MISP/misp-warninglists @ 40e2b570 (43 lists), `scripts/fetch_misp_warninglists.py` | CC0-1.0; X4BNet lists MIT | 11 MB | INF shared-infrastructure tiers (CDN, cloud, parking) | tier overrides off, hand-made lists only | snapshot JSON with per-file sha256 |
| e5-base (optional, `--with-base`) | HF intfloat/multilingual-e5-base @ d1287505 | MIT | 1.1 GB | not on the default path | — | — |
| siglip2-base (optional) | not in the lock; used only if `$HYDRA_MODELS_DIR/siglip2-base` exists | Apache-2.0 | — | second VIS score | off | — |
| Bielik GGUF (optional) | `just llm-fetch` into `~/HydraNexus/models-llm` (CR-LLM-5) | Apache-2.0 | 2.9 GB | optional LLM analyst, off by default | off | not on the jury path |

Total default download *measured*: 1.1 GB, 34 s on the Linux server (fetch), 59 s verify.

## 4. System binaries and libraries

| binary / library | used for | in the Docker image | native Linux (Debian/Ubuntu) | macOS |
|---|---|---|---|---|
| ffmpeg, ffprobe | video frames, audio for ASR/fingerprint | yes (apt) | `apt install ffmpeg` | `brew install ffmpeg` |
| tesseract + pol, eng data | OCR fallback / line re-read | yes | `apt install tesseract-ocr tesseract-ocr-pol tesseract-ocr-eng` | `brew install tesseract tesseract-lang` |
| g++ / build-essential | build pdqhash and py-tlsh from sdist | build stage only (not in the runtime image) | `apt install build-essential` | Xcode Command Line Tools |
| libGL, libglib2.0 | opencv-python | yes | `apt install libgl1 libglib2.0-0` | — |
| libEGL, libGLESv2 | MediaPipe Tasks (dlopen even on CPU) | yes (added on this branch) | `apt install libegl1 libgles2` | — |
| dig (dnsutils) | live DNS lookups only (`HYDRA_ENRICH=1`) | yes | optional | optional |
| DejaVu/Arial font | OCR smoke test in `--verify` only | DejaVu via tesseract deps | optional | system font |

## 5. Offline guarantees

- `HYDRA_OFFLINE=1`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` are the image defaults; enrichment replays
  `cache/enrich` only. Every loader uses `local_files_only=True` / a local path.
- The `cli` compose service runs with `network_mode: none`: the analyse + assign flow was run that way
  (section 6), so any outbound attempt would fail instead of silently succeeding.
- Missing models degrade to `unavailable` genes, never a crash: verified by running `hydra analyse` with an empty
  `HYDRA_MODELS_DIR` (section 6).

## 6. Measured runs

All on the Linux server, 2026-11-21. Simulated hidden set built on the server only
(`~/HydraNexus/portability/hidden-sim`, never in git): known base = the 10 NASK sample incidents under renamed
folder names (533 MB, screenshots, saved pages, HAR, videos); new items = 2 partial copies (HAR, videos and half of
the remaining files removed), 1 exact duplicate under a new name, 1 images-only copy (5 images, no page), 1 ZIP,
6 external incidents from `corpus-demo` (3 purino-scam-ads, 3 phreshphish-2k) as material from other campaigns.

| step | Docker (`network_mode: none` for analyse/assign) | native (uv) |
|---|---|---|
| clean build / env | `docker compose build --no-cache --pull`: 535 s and 614 s (two runs); image 9.5 GB unpacked, 2.18 GB compressed | `uv sync --frozen` with an empty uv cache: 19 s (pdqhash and py-tlsh compiled from sdist) |
| model download + verify, empty models dir | `--profile models`: 96 s, 10 of 10 keys ok | fetch 34 s + verify 59 s; 9 of 10 ok (landmarker: host lacks libEGL) |
| `analyse` known base (10 incidents) | 339 s (genes 332 s), 13,465 genes, 7 campaigns (2 multi-member), 1 group | 651 s (ran concurrently with a Docker build), same campaigns |
| `assign-batch` new items (11) | 58 s (0.5 to 9 s per item) | 79 s |
| decisions vs expectation | 10 of 11: partial 2/2, duplicate 1/1, ZIP 1/1, external rejected as new 6/6; images-only copy flagged `new_campaign` instead of its campaign (0/1) | identical |
| GUI / API | `docker compose up -d api` on port 8200: `/health` ok (offline, 8 models), `GET /api/v1/assignments` total 11, `/` HTTP 200 | not run |
| empty `HYDRA_MODELS_DIR` | — | analyse of 3 incidents exit 0: 4,795 observed, 36 unavailable, 0 failed genes (ident, txt_embed, vis, prov, vid_avsync unavailable); `assign` exit 0 |

The test is a robustness/portability check, not a quality benchmark: copies of known incidents share bytes with
the base, so the partial/duplicate/ZIP results show that degraded inputs still link, not that new variants do.

Throughput after the SCALE changes (the Linux server 128 vCPU, shared, load 230-340; 2026-11-21 19:52-19:58 UTC;
`docker run --network none --cpus 24 -e HYDRA_CPU_BUDGET=24 -e HYDRA_WORKERS=8 -e HYDRA_TORCH_THREADS=8`, fresh
state, cold OCR cache; input `~/HydraNexus/data/scale-synth100`, 100 synthetic incidents made by
`.scale-tools/make_synth.py`: saved page, url.txt, 1-2 screenshots, a 3 s video in every tenth, 20 campaigns of 4
plus 20 singletons; never in git):

| image | `analyse` wall | per incident | genes | scoring + clustering | conformal | self-assignment | result |
|---|---|---|---|---|---|---|---|
| `hydra-nexus:portab2` (17:09 UTC) | 255 s | 2.6 s | 138 s | 6.4 s | 35.7 s | (not in that code) | 4,950 pairs, 37 campaigns (21 multi-member), 12,097 genes |
| `hydra-nexus:scale`  | 156 s | 1.6 s | 60 s | 3.9 s | 3.4 s | 4.5 s | identical counts |

The wall time includes container start, imports and model loading (about 75 s of the 156 s are outside the stage
timings). Open-set assignment of 50 synthetic items one after another against the 825-incident base run
(`.scale-tools/bench_assign.py`, native, same envelope): 646 s with the base corpus reloaded per item, 86 s with
it kept (`HYDRA_ASSIGN_BASE_CACHE`, default on; median 12.3 s -> 1.2 s per item), 50 of 50 assignments identical.
On a container whose CPU quota is lower than the host's core count, set `HYDRA_CPU_BUDGET` to the quota:
`os.cpu_count` inside the container reports the host's cores.

## 7. Jury runbook (Docker, Linux)

```bash
git clone https://github.com/FOTOhubpl/hydra-nexus-public && cd hydra-nexus-public       # or the submitted tag
mkdir -p ~/HydraNexus/{models,data/external,cache/linux-x86_64,cache/enrich,state-docker,exports}
docker compose build api                                    # needs network once
docker compose --profile models run --rm models             # one-time model download + offline verify
# put the material under ~/HydraNexus/data/, e.g. data/jury/known (known base) and data/jury/new (new items)
# face/logo reference base from the known material (GAP B11; written to the state volume, data stays read-only)
docker compose --profile cli run --rm --entrypoint python cli scripts/build_vis_reference.py --sample /hydra/data/jury/known --out /hydra/state/reference
docker compose --profile cli run --rm cli analyse /hydra/data/jury/known
docker compose --profile cli run --rm cli assign-batch /hydra/data/jury/new --out /hydra/exports/jury
docker compose up -d api                                    # GUI on http://127.0.0.1:8000/ (Linux, host networking)
docker compose --profile gui-ports up -d gui                # instead, on Docker Desktop (macOS/Windows)
```

Results: `~/HydraNexus/exports/jury/assignments.csv` and `assignments.json` (one row per new item: decision
`assign` / `new_campaign` / `abstain` / `error`, campaign id, top candidate with evidence bits, reasons in Polish).
The GUI shows the same assignments ("Kampanie", incident card, `GET /api/v1/assignments`).
With `just`: `just docker-models` then `just docker-jury jury/known jury/new jury`.

CPU: compose passes `HYDRA_CPU_BUDGET=8` (default) to `api`, `gui` and `cli`, because `os.cpu_count` in a container
reports the host's cores. `hydra.parallel.available_cpus` also caps the budget by the affinity mask and the
cgroup quota (`docker run --cpus`, compose `cpus:`). Bigger machine: `HYDRA_CPU_BUDGET=$(nproc)`.

GUI without host networking (`gui` service, profile `gui-ports`): the port is published as `127.0.0.1:8000:8000`, so
it is reachable from the host only. Inside its own network namespace the API binds `0.0.0.0`; `hydra.config` allows
that single non-loopback value only when `HYDRA_IN_CONTAINER=1`, which only this service sets. The Host/Origin guard
(`hydra.api._LocalOnlyGuard`) is unchanged: a request with a non-local Host header gets 403.

Host uid other than 1000: prefix commands with `HYDRA_UID=$(id -u) HYDRA_GID=$(id -g)`. Another root than
`~/HydraNexus`: `HYDRA_HOST_ROOT=/path`.

Input layout: one subfolder per incident (any mix of screenshots png/jpg/webp, videos mp4/ts, saved pages
html + `_files/`, HAR logs, `url.txt`/text files, platform metadata JSON), or one ZIP / single file per incident.
Hidden files, `__MACOSX` and `.DS_Store` are skipped.

## 8. Native path (no Docker)

```bash
git clone https://github.com/FOTOhubpl/hydra-nexus-public && cd hydra-nexus-public
uv python install 3.11 && uv sync --frozen
just models-fetch                         # = HYDRA_MODEL_DOWNLOAD=1 HYDRA_OFFLINE=0 scripts/fetch_models.py
. scripts/env.sh
uv run hydra analyse ~/HydraNexus/data/jury/known
uv run hydra assign-batch ~/HydraNexus/data/jury/new --out ~/HydraNexus/exports/jury
(cd ui && pnpm install --frozen-lockfile && pnpm build) && HYDRA_UI_DIST=$PWD/ui/dist uv run hydra serve
```

## 9. Blockers found and their status

| # | blocker | status |
|---|---|---|
| 1 | faster-whisper-small not in `fetch_models.py` / lock (aud ASR silently unavailable on a clean machine) | fixed: pinned `Systran/faster-whisper-small@536b0662`, sha256 in lock, smoke test |
| 2 | `face_landmarker.task` not in fetch/lock; `prov` read `mediapipe/`, `vid_avsync` read `mediapipe-face-landmarker/` | fixed: one key `mediapipe-face-landmarker` (URL float16/1 + sha256); `prov` reads the same path (old path still accepted); test `tests/test_portability.py` |
| 3 | MediaPipe needs `libEGL.so.1` / `libGLESv2`: missing in the image and on the server host (av-sync genes FAILED, avatar genes unavailable) | fixed in the image (`libegl1 libgles2`); native Linux: `apt install libegl1 libgles2` (documented) |
| 4 | MISP warninglists snapshot not fetched by the model step (INF tier overrides off on a clean machine) | fixed: `scripts/fetch_misp_warninglists.py --commit 40e2b570` runs in `--profile models` and `just models-fetch` |
| 5 | no batch path for a jury folder; no assignment export file | fixed: `hydra assign-batch <folder> --out <dir>` writes `assignments.json` + `assignments.csv` against one fixed base run |
| 6 | model fetch was not a documented container step; image had no way to write the lock (read-only `/app`) | fixed: compose `models` profile; lock write tolerates the read-only copy and writes the mirror in `$HYDRA_MODELS_DIR` |
| 7 | runtime network isolation not enforced | fixed for the CLI path: compose `cli` profile with `network_mode: none`; the GUI service keeps host networking (loopback bind) |
| 8 | container cache dir depended on the CPU arch (`linux-aarch64` on Apple silicon would miss the mounted PSL cache) | fixed: `HYDRA_CACHE_DIR=/hydra/cache/linux-x86_64` in the image |
| 9 | host uid other than 1000 cannot write the bind mounts | fixed: `HYDRA_UID`/`HYDRA_GID` in compose |
| 10 | pdqhash and py-tlsh have no Linux cp311 wheel (py-tlsh none on macOS either) | open, handled: build-essential in the image; native needs g++ / Xcode CLT |
| 11 | 3 tests fail on Linux on `integration` a3096ab itself (not caused by this branch): `test_inf_enrich::test_replay_genes_and_records` (as_org/asn), `test_prov::test_avatar_flag_via_landmarker_path` (mp.Image without libEGL on the host), `test_ident_logo::test_logo_orb_template_found_with_bbox` | open: owners CORE/INF, PROV, VIS; `just check` exit 1 on the server for that reason |
| 12 | `FaceLandmarker.__del__` prints a TypeError at interpreter exit after `assign-batch` | open, cosmetic (exit code 0) |
| 13 | image size 9.5 GB (torch CPU, mediapipe, onnxruntime, opencv, build-essential) | fixed: 4.3 GB (section 10) |
| 14 | Docker Desktop (macOS/Windows) GUI needs host networking | fixed: `gui` service (profile `gui-ports`); tested on Linux Docker Engine with a bridge network, not on Docker Desktop itself |
| 15 | `audioseal-detector` failed its sha256 pin in about half of the fetches: safetensors writes `__metadata__` from a Rust HashMap (random key order per run) | fixed: `convert_checkpoint` rewrites the block in a fixed order (same length, same offsets); 10 of 10 conversions = pinned `449efba1` |
| 16 | three opencv wheels (opencv-python, opencv-contrib-python via mediapipe, opencv-python-headless) write the same `cv2/` | handled: the build keeps only the `.libs` directory the installed `cv2` links to (measured: contrib); which wheel owns `cv2/` is decided by the installer, unchanged from before |

## 10. Jury Docker re-check (2026-11-21 22:00-23:00 Europe/Warsaw)

Fresh clone of `integration` a213ae8 on the Linux server (128 vCPU, shared, load 80-140 during the runs), Docker
29.10.0 / Compose 5.8.0, `HYDRA_HOST_ROOT` = an empty jury root. All numbers *measured*; commands in section 7 and
`docker compose ... --profile cli` with the compose defaults (`HYDRA_WORKERS=8`, `HYDRA_TORCH_THREADS=8`,
`HYDRA_CPU_BUDGET=8`).

Image (`docker compose build --no-cache api`, BuildKit cache mount for uv empty at the start):

| | before (a213ae8) | after (multi-stage) |
|---|---|---|
| build time | 475 s (of which 344 s exporting layers) | 368 s (154 s exporting) |
| image, unpacked / compressed (`docker images`) | 9.66 GB / 2.21 GB | 4.3 GB / 1.01 GB |
| largest layers | venv + uv cache 6.29 GB, apt incl. build-essential 802 MB | venv 2.63 GB, apt runtime 499 MB |

What was removed: the uv download cache (2.9 GB, now a BuildKit cache mount), build-essential and uv (build stage
only), test suites (`*/tests`, `*/test`) and torch headers/CMake files/static libraries, and two of the three
opencv bundled-library directories (only the one `cv2` links to is kept, checked with `ldd` during the build).
Kept: libEGL/libGLESv2 (MediaPipe), libGL/libglib (opencv), ffmpeg, tesseract pol+eng, dig, curl. The runtime stage
imports cv2, torch, mediapipe, onnxruntime, ctranslate2, pdqhash, tlsh, scipy, sklearn, pandas and `hydra.api` at
build time, so a missing library fails the build.

Models (`docker compose --profile models run --rm models` into an empty models dir): exit 0 in 155 s; 11 of 11
keys `ok` in the offline verify (dinov2-small, e5-small, yunet, sface, faster-whisper-small,
mediapipe-face-landmarker, audioseal-detector via `url_convert`, rapidocr, rapidocr-v6, cert-domains, psl; 32 files
sha256 re-checked); MISP warninglists @40e2b570: 206 lists fetched, 25 unmapped upstream lists skipped by design;
1.2 GB models + 61 MB reference data. The first attempt failed on `audioseal-detector` (blocker 15).

Jury flow (`network_mode: none`; reference base B11, `analyse` of the known base, `assign-batch` of the new items):

| set | known / new | reference (B11) | `analyse` | `assign-batch` (wall, incl. container start) | per new item (`seconds` column) | decisions vs expectation |
|---|---|---|---|---|---|---|
| synthetic `jury-synth` (generated, no organiser data: 8 campaigns x 4 + 8 singletons; new: 8 variants, 1 ZIP variant, 1 creative-only variant, 2 byte copies of known incidents, unseen campaign of 3 + an in-batch byte copy, unseen campaign of 2, 4 singletons) | 40 / 22 | 6 s | 58 s (1.45 s per incident; 5,776 genes, 9 campaigns) | 53 s | median 1.73 s, max 15.6 s | 16 of 22 |
| `hidden-sim` (section 6, server only) | 10 / 11 | 96 s | 441 s (44 s per incident; genes 395 s; 14,938 genes, 7 campaigns) | 70 s | median 2.44 s, max 15.4 s | 10 of 11 (images-only copy `new_campaign`, as in section 6) |

`duplicate_of` / `new_pattern_id` in `assignments.csv` and `.json`:
- byte copies of known incidents: `duplicate_of` = the known incident (2 of 2 synthetic, 1 of 1 hidden-sim), plus
  the ZIP copy in hidden-sim; the in-batch byte copy has `duplicate_of_item` = its twin (1 of 1).
- unseen synthetic campaign A (3 items): 2 share `new_pattern_id` N-0b71fca0, the third was assigned to a known
  campaign (its in-batch byte copy followed it); unseen campaign B (2 items): 1 abstain, 1 assigned to a known
  campaign; singletons: 2 of 4 `new_campaign` with a size-1 pattern id, 1 abstain, 1 assigned to a known campaign. The synthetic campaigns share one page template and
  a 16-word vocabulary, so the base run already merged two singletons into each of two campaigns (6-member
  campaigns); the misses are template false positives on deliberately similar data, not a Docker effect.
- hidden-sim external items (6) and the images-only copy: each `new_campaign` with its own pattern id.

GUI (`docker compose --profile gui-ports up -d gui`, `HYDRA_API_PORT=8231`, bridge network): `/health` ok after 3 s
(offline, models listed), `GET /api/v1/assignments` 62 rows, `/` HTTP 200, container healthcheck `healthy`;
`Host: evil.example` -> 403, cross-origin POST -> 403; the host's LAN address on that port is not reachable
(published on 127.0.0.1 only). `HYDRA_API_HOST=0.0.0.0` without `HYDRA_IN_CONTAINER=1`: `hydra serve` exits with a
validation error. Not tested on Docker Desktop itself (no macOS/Windows Docker host available to the team here).

CPU budget on the hidden-sim known base (10 incidents), cold caches unless stated: `HYDRA_CPU_BUDGET=8` (compose
default, 4 incident processes = `budget // 2`) `analyse` 441 s (genes 395 s); `HYDRA_CPU_BUDGET=24` 274 s;
`HYDRA_CPU_BUDGET=24` with the gene/OCR/ASR caches of `cache/linux-x86_64` warm from an earlier run 136 s (genes
28 s). Section 6 measured 339 s with the budget at every host core (64).
