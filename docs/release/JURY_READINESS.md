# JURY-READY test report (clean state, jury's own data)

Date 2026-11-22 (Europe/Warsaw), measured on the release candidate before 1.0 (the scoring code `hydra/rarity*`,
`synapse`, `conformal`, `assign` is identical in 1.0). Measured numbers only; every row names its command. Synthetic
data only (`tests/fuzz/jury_corpus.py`), no organiser material.

## 1. Clean-state path

| Check | Command / place | Result (measured) |
|---|---|---|
| Server on an empty state | fresh `HYDRA_ROOT`, `hydra serve` with `HYDRA_UI_DIST=ui/dist` | `/health` ok, `offline: true`, 11 models listed, 0 artifacts |
| API on an empty state | 23 GET endpoints (`/v1/runs`, `/runs/latest`, `/incidents`, `/campaigns`, `/graph`, `/pairs`, `/assignments`, `/dashboard`, `/campaign-groups`, `/analytics/*`, `/corpora`, `/simulation`, `/jobs`, `/reviews`, `/watchlist`, `/scheduler/status`, `/audit/verify`, `/assign-batches`) | all 200 with empty lists; `/v1/search?q=x` 422 (minimum 2 characters, by design) |
| GUI on an empty state | Playwright, 22 routes incl. deep links to missing ids | no page errors, no 5xx; every page shows an empty state; deep links to unknown ids show "Punkt końcowy API jeszcze niedostępny" with the 404 path |
| Onboarding | empty Pulpit | new card "Pusta baza: zaimportuj własny zbiór incydentów" (drag and drop, server folder, CLI, formats, rejected files), links to `/analyse?tab=files` and `?tab=path` |
| Offline | `just jury-smoke` (tests/e2e netlog: every non-loopback connect/DNS that passes the product guard is logged) | network log empty in 3 runs |

## 2. Import fuzz corpus

`uv run pytest tests/fuzz` (29 tests, cheap extractors) and full-extractor `hydra analyse <layout>` on a fresh state per
layout (laptop, macOS arm64, 10 cores, 16 GB, `HYDRA_EXTRACT_WORKERS=2`). All exit 0.

| Layout | Incidents | Pairs | Campaigns (multi) | Wall s | Statuses / notes |
|---|---|---|---|---|---|
| nask_folders (4 + 1 byte copy) | 5 | 10 | 2 (2) | 32 | copy lands with its original |
| wrapper (`root/Dane testowe/p1..p3`) | 3 | 3 | 2 (1) | 17 |  |
| top_zip (one ZIP, wrapper folder inside) | 3 | 3 | 2 (1) | 18 | |
| zip_in_zip (ZIP of 3 incident ZIPs, one holding another ZIP) | 3 | 3 | 1 (1) | 16 |  |
| loose_root (files at the input root) | 1 | 0 | 1 (0) | 11 | |
| nested (`kategoria/podkategoria/incydent`) | 2 | 1 | 2 (0) | 27 | imported per category folder; note in the import report |
| one_incident | 1 | 0 | 1 (0) | 10 | no pairs, no crash |
| singletons (3 unrelated) | 3 | 3 | 3 (0) | 12 | |
| identical (3 byte-identical) | 3 | 3 | 1 (1) | 14 | |
| nothing (only 0-byte and OS junk files) | 2 | 1 | 2 (0) | 1 | `FILE_EMPTY` x2, 0 observed genes |
| weird (one incident with every hostile file) | 1 | 0 | 1 (0) | 65 | corrupt PNG/JPEG/MP4 stored, extractors report failed/unavailable |
| mixed (folders + incident ZIP + README + bomb + password ZIP + corrupt ZIP + weird incident + duplicate) | 7 | 21 | 5 (1) | 81 | 9 files with a status (CLI run) |

Hostile files covered and their status: 0-byte (`FILE_EMPTY`), ZIP bomb in the root and inside an incident
(`ZIP_BOMB_SUSPECTED`), password ZIP (`ZIP_ENTRY_UNREADABLE`), traversal and absolute entries (`ZIP_PATH_TRAVERSAL`),
symlink entry in a ZIP (`ZIP_SYMLINK`), symlinks in a folder incl. a loop and one to `/etc/passwd`
(`SYMLINK_NOT_FOLLOWED`), corrupt ZIP at the root and README (`LOOSE_ROOT_FILE`), unknown binary (stored as `other`
by the adapter; `MIME_NOT_ALLOWED` in the generic `/v1/import`), wrong extensions (PNG named `.htm` sniffed
`image/png`), uppercase extension, 240-byte UTF-8 file name, Polish diacritics and spaces, cp1250 and ISO-8859-2 text,
~3 MB text, MHTML (base64 and quoted-printable), HAR variants (base64 body, no `pages`, truncated JSON), OS junk
(`.DS_Store`, `Thumbs.db`). In every case the other incidents were analysed.

## 3. Scale and time

Linux x86_64 server (Ubuntu, 128 vCPU shared with other jobs, load average 260-1090 during the runs, so absolute
times are pessimistic); hydra/pipeline.py is identical in 1.0. Synthetic `tests/fuzz/jury_corpus.py scale --n N --video-every 10` (~70 % in campaigns of 2-8, a
recording every 10th incident), fresh state per run, no `data/external`. `HYDRA_EXTRACT_WORKERS` unset (plan: 4 extract
workers at budget 8, 2 at budget 4). Command: `/usr/bin/time -v uv run hydra analyse <dataset> --json`; tree memory
sampled every 5 s. All runs exit 0, no traceback; the same incidents/pairs/campaigns at both budgets.

| N | `HYDRA_CPU_BUDGET` | Wall | Single-process max RSS | Process-tree peak | Pairs | Campaigns (multi) | State on disk |
|---|---|---|---|---|---|---|---|
| 50 | 8 | 83 s | 1.59 GB | 6.0 GB (RSS sum) | 1,225 | 23 (8) | 41 MB |
| 50 | 4 | 111 s | 1.58 GB | 3.2 GB | 1,225 | 23 (8) | |
| 200 | 8 | 255 s | 1.59 GB | 6.4 GB | 11,324 | 87 (27) | 226 MB |
| 200 | 4 | 430 s | 1.63 GB | 3.6 GB | 11,324 | 87 (27) | |
| 200 | 8, Docker `--cpus 8 --memory 16g --network none` | 359 s (incl. ~65 s container start under load) | | 4.85 GiB (cgroup) | 11,324 | 87 (27) | |
| 1000 | 8 | 2,241 s (37 min) | 19.2 GB | 30.7 GB PSS | 244,408 | 433 (133) | 2.6 GB |
| 1000 | 4 | incomplete, stopped at 2,555 s in self_assignment (252/1000) | 9.3 GB so far | 18.5 GB PSS | | | |

Per-stage seconds at budget 8 (import / genes / load_genomes / rarity+scoring+clustering / conformal / self_assignment /
persist / aggregates): N=50 0.7 / 57 / 2.3 / 5.4 / 9.1 / 2.5 / 1.7 / 0.7; N=200 0.9 / 162 / 3.5 / 12.1 / 27.5 / 18.6 /
22.0 / 2.7; N=1000 5.2 / 822 / 12.1 / 170 / 157 / 512 / 495 / 16.0.

Scaling exponents (log t2/t1 / log N2/N1), budget 8, 200 -> 1000: genes 1.01, conformal 1.08 (capped at 120 items),
rarity+scoring+clustering **1.64**, self_assignment **2.06**, persist **1.94**, wall **1.35**. The super-linear stages
are pair-count driven (244k candidate pairs at N=1000): leave-one-out self-assignment over the whole corpus (no cap by
default, `HYDRA_SELF_ASSIGN_MAX` unset) and the single-threaded run write (main process grows from 9.6 to 19.2 GB during
persist). They live in hydra/pipeline.py / hydra/assign.py, frozen for the final evaluation, so they are documented, not
changed.

Progress visibility (API `POST /v1/analyse {path}`, N=200, budget 8): 202 at once; `GET /v1/demo/progress/<id>` carries
stage, incidents_done, pairs and self_assigned; `/v1/stream` (SSE) sent 15 job events plus heartbeats. The counter is
frozen for long stretches: incidents_done stayed 0 for ~150 s of the 163 s genes stage and the assign stage
ran ~85 s without a job-count change; at N=1000 the progress file did not change for ~6 min during conformal calibration
and ~9 min during persist. Upload path N=50 (1.24 MB ZIP): 202 in 0.13 s, done after 85.5 s, same result as the CLI.

**Recommended machine (from these measurements).** Up to ~200 incidents: minimum 4 cores / 8 GB RAM (measured tree peak
3.6 GB, N=200 in 7.2 min); comfortable 8 cores / 16 GB (tree 6.4 GB, Docker cgroup 4.85 GiB, N=200 in 4.3 min); 10 GB
free disk with Docker. About 1000 incidents: 8 cores / 32 GB RAM minimum (*estimated* from the measured 19.2 GB
single-process and 30.7 GB tree PSS peaks; a 16 GB machine or the compose `--memory 16g` limit is expected to run out of
memory, not tested), 16 cores / 64 GB comfortable, > 30 min wall, >= 5 GB free disk for the state.

## 4. One-command flows

| Flow | Command | Measured |
|---|---|---|
| Jury smoke (clean state, no developer `data/external`) | `just jury-smoke` (`scripts/jury_smoke.py --n 20`) | PASS 9/9 steps on the laptop: hostile ZIP upload 42-68 s (6 incidents, 10 file statuses, progress stages `import`, `genes`, `done` seen), 20-incident dataset 16-27 s (190 pairs, 17 campaigns, 3 multi), dataset export 11 files with 10 sha256 re-verified, audit chain 159 events ok, `hydra verify` ok, CLI batch 12-15 s, network log empty |
| Same with the reference bundle | `just jury-smoke --reference-bundle hydra-reference-data-20261122.tar.zst` | PASS 9/9; no "Brak danych referencyjnych offline" note; same pair/campaign counts on the synthetic set |
| Reference bundle | `uv run python scripts/reference_data.py build` / `install` | 4.3 MB `.tar.zst`; install verified 209 + 3 + 3 files by SHA256SUMS |

## 5. Gates

Laptop (macOS arm64), release candidate before 1.0.

| Gate | Result |
|---|---|
| `just check` | exit 0: ruff clean, pytest 1473 passed / 52 skipped (175 s), UI typecheck + vitest 237 passed |
| `just e2e` | exit 0, 41 passed (38.5 s) |
| `just e2e-ui` | exit 0, 114 passed (1.2 min) |
| `just jury-smoke` | PASS 9/9; the signed dataset export also passes `hydra verify-export` (Ed25519 + sha256) |
| Sample regression (`HYDRA_SAMPLE_TEST=1 uv run pytest tests/core/test_sample_regression.py`, needs the organiser sample, which is not public) | 7 passed |

The test counts of the public tree differ (tests that need the organiser sample skip); see the release notes.

## 6. Open risks

1. Category-nested datasets are imported per category folder; the report names the folder, the jury must
   re-run per folder.
2. A single incident delivered as a ZIP that holds only >= 2 inner ZIPs is treated as a dataset in `hydra analyse` and
   GUI uploads; single-item paths are unchanged.
3. Without the face gallery/logo templates, gallery `person_named` and ORB logo genes are empty on a clean machine
   (visible note); scores on the organiser sample were measured with a gallery built from that sample.
4. ~1000 incidents need ~32 GB RAM and > 30 min (§3); self_assignment and the run write grow ~N² and live in frozen
   scoring paths. The progress counter freezes for minutes on large sets; the analysis continues.
5. Without the reference bundle, MISP warninglist tier overrides are off. On the 20-incident synthetic set the pair and
   campaign counts were identical with and without the bundle; on other data they may differ.
