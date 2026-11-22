# HYDRA NEXUS image: API/worker (CPU torch, tesseract pol, ffmpeg) + the built UI.
# Same uv.lock and pnpm-lock.yaml as the laptop. Models, data, state and caches are mounted from
# ~/HydraNexus at run time (docker-compose.yml); nothing organiser-related is baked into the image.

# ---- stage 1: UI build (pnpm, Vite) -------------------------------------------------------------
FROM node:24-bookworm-slim AS ui
RUN npm install -g pnpm@12.15.0 && npm cache clean --force
WORKDIR /ui
COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/ ./
RUN pnpm build
# `vite preview` serves dist/ and proxies /api -> HYDRA_UI_BACKEND (used until the API mounts the UI).
ENV HYDRA_UI_BACKEND=http://127.0.0.1:8000
EXPOSE 4173
CMD ["pnpm", "exec", "vite", "preview", "--host", "127.0.0.1", "--port", "4173", "--strictPort"]

# ---- stage 2: Python environment (build toolchain stays in this stage) -----------------------------
FROM python:3.11-slim-bookworm AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never
# build-essential: pdqhash and py-tlsh have no cp311 Linux wheel (sdist builds need g++).
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/
WORKDIR /app
# Dependencies first (cached layer); torch resolves to the CPU wheel on Linux via [tool.uv.sources]. The uv cache
# is a BuildKit cache mount, so the ~2.9 GB of downloaded wheels never land in an image layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,id=hydra-uv,sharing=locked,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY hydra ./hydra
RUN --mount=type=cache,id=hydra-uv,sharing=locked,target=/root/.cache/uv uv sync --locked --no-dev
# Strip what the runtime never imports: test suites, C++ headers/static libs and CMake files of torch.
# scenedetect pulls opencv-python and mediapipe pulls opencv-contrib-python next to our opencv-python: all
# write the same cv2/ package, so only the bundled-libs directory that cv2's extension links to is kept.
RUN set -eu; sp=/opt/venv/lib/python3.11/site-packages; \
    find "$sp" -depth -type d \( -name tests -o -name test \) -path "$sp/*/*" -exec rm -rf {} +; \
    rm -rf "$sp/torch/include" "$sp/torch/share/cmake" "$sp/torch/test" "$sp/torch/bin/test_"*; \
    find "$sp" -name "*.a" -delete; \
    cvso=$(find "$sp/cv2" -name "cv2*.so" | head -n1); \
    keep=$(ldd "$cvso" | sed -n 's|.*/\(opencv[^/]*\.libs\)/.*|\1|p' | sort -u | tr '\n' ' '); \
    echo "cv2: $cvso uses ${keep:-system libs only}"; \
    for d in "$sp"/opencv_*python*.libs; do \
        case " $keep " in *" $(basename "$d") "*) ;; *) echo "drop $(basename "$d")"; rm -rf "$d";; esac; \
    done

# ---- stage 3: runtime (no compiler, no uv, no caches) ----------------------------------------------
FROM python:3.11-slim-bookworm AS api

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HYDRA_ROOT=/hydra HYDRA_API_HOST=127.0.0.1 HYDRA_OFFLINE=1 HYDRA_ENRICH=0 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 \
    HYDRA_CACHE_DIR=/hydra/cache/linux-x86_64 HF_HOME=/hydra/cache/linux-x86_64/huggingface \
    HYDRA_UI_DIST=/app/ui/dist

# libgl1/libglib2.0-0: opencv-python (non-headless). dnsutils: `dig` for live DNS and Team Cymru lookups in
# hydra/enrich/dns.py (CR-INF-1). libegl1/libgles2: MediaPipe Tasks (face landmarker, vid_avsync + prov) dlopens
# libEGL.so.1 even on CPU. curl: healthcheck. zstd: unpacking the offline reference-data bundle (scripts/reference_data.py).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg tesseract-ocr tesseract-ocr-pol tesseract-ocr-eng \
        libgl1 libglib2.0-0 libegl1 libgles2 curl ca-certificates dnsutils zstd \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /usr/share/doc/* /usr/share/man/* /usr/share/info/*

WORKDIR /app
# /app/hydra is installed editable into /opt/venv (same path as in the build stage).
COPY --from=build /opt/venv /opt/venv
COPY pyproject.toml uv.lock README.md ./
COPY hydra ./hydra
COPY scripts ./scripts
COPY models.lock.json ./
COPY --from=ui /ui/dist /app/ui/dist

ENV PATH=/opt/venv/bin:$PATH
RUN useradd --create-home --uid 1000 hydra && mkdir -p /hydra && chown hydra /hydra \
    && python -c "import cv2, torch, mediapipe, onnxruntime, ctranslate2, pdqhash, tlsh, scipy, sklearn, pandas, \
hydra.api, hydra.pipeline; print('imports ok: cv2', cv2.__version__, 'torch', torch.__version__)" \
    && hydra --help >/dev/null
USER hydra

EXPOSE 8000
# Shell form: /bin/sh expands HYDRA_API_PORT when the check runs, so a non-default port stays healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS "http://127.0.0.1:${HYDRA_API_PORT:-8000}/health" || exit 1
CMD ["hydra", "serve"]

# ---- stage 4: full image = runtime + pinned model weights + offline reference data ---------------------------------
# `docker build --target full` / ghcr.io/fotohubpl/hydra-nexus:full. No models step needed: the 10 pinned models
# (models.lock.json, including the optional e5-base) are fetched at build time and every file is checked against the
# sha256 pinned in the repository; the reference-data bundle (MISP warninglists CC0, PSL MPL-2.0 unmodified, CERT
# Polska snapshot) is the v1.0.0 release asset, checked against its sha256 and its per-set SHA256SUMS.
# Baked under /opt/hydra (not under /hydra, which docker-compose.yml mounts from the host). Licence and attribution
# files: /opt/hydra/models/LICENSES. All weights are permissive (Apache-2.0 / MIT, docs/MODELS.md).
FROM api AS full
ARG HYDRA_REFDATA_URL=https://github.com/FOTOhubpl/hydra-nexus-public/releases/download/v1.0.0/hydra-reference-data-20261122.tar.zst
ARG HYDRA_REFDATA_SHA256=5b09ee603286743a010fa1dc7dc355910fd85c1faf6ec1afd9ca0c090ae3e190
USER root
RUN mkdir -p /opt/hydra/models /opt/hydra/data /opt/hydra/cache && chown -R hydra /opt/hydra
USER hydra
ENV HYDRA_MODELS_DIR=/opt/hydra/models HYDRA_DATA_DIR=/opt/hydra/data HYDRA_IMAGE_FLAVOUR=full
# 1. reference data (download + sha256 of the archive, then SHA256SUMS of every set inside)
RUN set -eu; curl -fsSL "$HYDRA_REFDATA_URL" -o /tmp/refdata.tar.zst; \
    echo "$HYDRA_REFDATA_SHA256  /tmp/refdata.tar.zst" | sha256sum -c -; \
    python scripts/reference_data.py install /tmp/refdata.tar.zst; rm -f /tmp/refdata.tar.zst
# 2. models at the pinned revisions (network only during this build step), PSL tldextract cache for the verify step
RUN set -eu; HYDRA_MODEL_DOWNLOAD=1 HYDRA_OFFLINE=0 HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 \
    HYDRA_CACHE_DIR=/opt/hydra/cache HF_HOME=/tmp/hf \
    python scripts/fetch_models.py --with-base --lock /opt/hydra/models/models.lock.json \
      --only dinov2-small e5-small e5-base yunet sface faster-whisper-small mediapipe-face-landmarker \
             audioseal-detector rapidocr rapidocr-v6 psl; \
    rm -rf /tmp/hf
# 3. verification: every model file must equal the sha256 pinned in the repository's models.lock.json; then the
#    offline re-hash + one smoke test per entry under a socket guard (HF offline flags of the runtime image apply)
RUN set -eu; python scripts/pin_check.py /app/models.lock.json /opt/hydra/models/models.lock.json; \
    HYDRA_CACHE_DIR=/opt/hydra/cache python scripts/fetch_models.py --verify --lock /opt/hydra/models/models.lock.json
COPY --chown=hydra THIRD_PARTY_NOTICES.md docs/MODELS.md docs/LICENSES.md /opt/hydra/models/LICENSES/
RUN set -eu; d=/opt/hydra/models/LICENSES; \
    python -c "import json; e=json.load(open('/app/models.lock.json'))['entries']; \
open('$d/MODEL_LICENCES.txt','w').write(''.join(f\"{k}\t{v.get('repo') or v.get('source')}\t{v.get('revision')}\t{v['license']}\t{v.get('url','')}\n\" for k,v in e.items()))"; \
    for s in misp-warninglists psl cert; do mkdir -p "$d/$s"; \
      find /opt/hydra/data/external/$s -maxdepth 3 \( -iname 'SOURCE.md' -o -iname 'LICENSE*' -o -iname 'README.md' \) \
        -not -path '*/lists/*' -exec cp {} "$d/$s/" \; ; done; ls -R "$d" | head -40
