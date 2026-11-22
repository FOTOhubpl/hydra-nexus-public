# From the hackathon build to production at NASK

## Podsumowanie (PL)

Wersja konkursowa HYDRA NEXUS to jeden kontener: SQLite, magazyn plików na dysku, pula procesów i interfejs
na jednym porcie. To świadomy wybór: demonstrator ma działać offline, z jednego polecenia, na laptopie
i na serwerze, bez zewnętrznych usług. Kontrakty danych (`hydra/contracts.py`) i migracje są niezależne od
bazy, więc droga do produkcji nie wymaga przepisywania logiki:

- SQLite → PostgreSQL (te same tabele i migracje 1:1);
- pliki CAS na dysku → MinIO/S3 (adres = sha256, bez zmian w logice);
- tabela zadań + pula procesów → kolejka (Redis lub RabbitMQ) i bezstanowe workery skalowane osobno dla każdej
  rodziny genów (VIS, TXT/OCR, INF, ID);
- osobna usługa pamięci wzbogaceń (RDAP, DNS, crt.sh, ASN) współdzielona przez workery;
- graf w Neo4j albo w PostgreSQL jako model odczytu dla analityków;
- logowanie, role i dziennik decyzji wielu analityków (łańcuch hashy już istnieje).

Zmierzone wąskie gardła: OCR i wideo (VIS), a przy dużych zbiorach ocena par kandydatów. Liczby poniżej
pochodzą z pomiarów; tam, gdzie są szacunkiem, jest to zaznaczone.

## 1. What the hackathon build is (and why)

| Concern | Hackathon build | Why |
|---|---|---|
| Database | SQLite in WAL mode, one `Writer` thread (`hydra/db.py`, ADR-002), migrations in `MIGRATIONS` | one file, no server, portable single-container demo; deterministic replays |
| Originals | content-addressed store on disk, sha256 names, read-only (`hydra/storage.py`) | evidence integrity; dedup by construction |
| Jobs | `jobs` table + `JobRunner`; gene extraction in a spawn process pool (`hydra/parallel.py`) | no broker to install; one machine |
| Enrichment | file cache per source with `fetched_at`, replay by default (ADR-007) | offline evaluation, reproducible results |
| Graph | networkx in process; optional Neo4j read model (`docs/NEO4J.md`) | the GUI and the method need no graph server |
| API/UI | FastAPI + built React UI on one loopback port | one container, one command |
| Audit | hash-chained event log (`hydra/events.py`), analyst reviews with audit events | tamper evidence from day one |

SQLite is a deliberate choice for this phase, not a limitation of the design: all reads and writes go through
`hydra/db.py` and the contracts, so a server database replaces it behind the same interfaces.

## 2. Measured performance today

| Measurement | Value | Source |
|---|---|---|
| Sample (10 incidents), cold run, server 64 vCPU, process-parallel extraction | 98.2 s total, genes 94.7 s (sequential: 185.3 s) | internal notes, not published (PERF) |
| Same, laptop M4 | 100.9 s (sequential: 171.2 s) | internal notes, not published |
| Parallel vs sequential results | genes, vectors, campaigns and pair scores byte-identical (torch threads fixed at min(16, cpus)) | internal notes, not published |
| Long pole on the sample | one video incident (VIS job: frames, hashes, embeddings) | internal notes, not published |
| Rarity + candidates + scoring + clustering, 45 pairs | 0.03-0.16 s | internal notes, not published; clean-clone run internal notes, not published |
| Assignment of one new item (cached genes) | 0.03-0.06 s | internal notes, not published |
| Public corpus, 6,663 incidents (INF genes only, enrichment from cache), server 64 vCPU | 3 min 36 s (genes 97.4 s, pair scoring 46.9 s for 303,973 candidate pairs, clustering 1.6 s, persist 58.7 s), peak RSS 4.9 GB | internal notes, not published |
| Docker image | 7.43 GB on disk (1.72 GB content); clean-clone `up --build` 236 s | internal notes, not published |

Pre-start lab on synthetic and public data (`mvp-lab/scale/RESULTS.md`, server shared, so throughput figures are
lower bounds):

| Measurement | Value |
|---|---|
| Image genes (decode, pHash, PDQ, DINOv2-small bf16), 50k images | 48.9 img/s end to end under load; model alone about 241 img/s on a quiet server |
| Exact cosine top-k | about 10 s over 100k vectors; 1.2 s at 25k: no approximate index needed |
| URL genes + CERT Polska lookup, 144,613 phishing URLs | 7 s wall, 0.29 GB RSS |
| Text MinHash (128 permutations) | about 2,000 texts/s |
| Candidate pairs vs corpus size | about N^1.4: 117k at 2k ads, 1.12M at 10k, 3.67M at 25k, 10.4M at 50k |
| 50k synthetic ads, linking | pair scoring 3,006 s (3.5k pairs/s, Python), peak RSS 24.1 GB, wall 59.4 min; B-cubed F1 0.914 at the best threshold (synthetic) |
| OCR (prototype, 16 threads) | 0.148 s per image, about 7 img/s (about 25k images/h) |

### 2a. SCALE profile: one 825-incident run before and after (2026-11-21)

Corpus `data/external-big/corpus-demo` (825 external incidents, 1,939 artifacts; the imported state copied fresh for
every run), `hydra analyse --corpus extended` under cProfile with a 5 s process-tree sampler, with the team per-job envelope (12 extraction workers x 2 torch threads, 1 OCR worker), gene CAS off (cold), OCR/ASR caches warm in
both. Server the Linux server 128 vCPU, shared (load 100-250). "Before" = 54026db (`base`, 18:22 UTC; `base2` ran side
by side with "after" from 19:29), "after" = on 877bdd9 (`fix2`, 19:29). Tools in `.scale-tools/` (not in git).

| Stage | before (base) | before, same time as after (base2) | after (fix2) | what changed (internal notes, not published) |
|---|---|---|---|---|
| genes (extract + persist) | 1,287 s | 880 s | 180 s | models reloaded per job (#23), NAR in the parent and its memo key (#24), cache files written by workers |
| worker CPU in extractors | 4,702 s (`txt_embed` 3,409) | | 1,037 s (`txt_embed` 236, fix1 run) | #23 |
| load_genomes | 12 s | 17 s | 14 s | key-range vector reads (O(N) instead of O(N^2)) |
| rarity + candidates + scoring + clustering | 328 s | 395 s | 49 s | pair scores and robustness in 12 forked workers, `Background.tail` (#27) |
| conformal calibration (120 + 120 items) | 594 s | 722 s | 53 s | rarity built once for pass 1, query-mode retrieval, forked items |
| self-assignment (825 leave-one-out items, new upstream stage) | not in 54026db | not in 54026db | 119 s | forked items |
| total wall | 2,314 s (38.6 min) | 2,054 s (34.2 min) | 496 s (8.3 min) | |
| process tree CPU (user + sys) | 5,837 s, 2.5 cores busy on average | 7,349 s | 3,507 s | |

Results: genes (223,834 rows), vectors (4,054) and all 106,328 pair scores byte-identical between base, fix1 and fix2;
campaign members identical between base and fix1 (pre-merge code). Group ids of one oversized component differed,
also between the two runs of the unchanged code (base vs base2): the Leiden split was not reproducible between
processes before (#28). The NASK sample regression
(`tests/core/test_sample_regression.py`, HYDRA_SAMPLE_TEST=1, same envelope) passes: 6 of 6.

Downstream cost against corpus size, nested random subsets (`.scale-tools/scaling.py`, one process, 2 BLAS threads;
measured):

| corpus | N | candidate pairs | candidates s | rarity s | one open-set item s | load_genomes s |
|---|---|---|---|---|---|---|
| corpus-demo | 100 / 200 / 400 / 825 | 1,285 / 5,779 / 22,744 / 106,328 | 0.1 / 0.5 / 0.3 / 1.3 | 0.18 / 0.08 / 0.16 / 0.57 | 0.03 / 0.07 / 0.06 / 0.10 | 2.6 / 2.9 / 5.2 / 9.6 |
| state-train (web pages) | 400 / 900 / 1,857 | 41,228 / 221,626 / 761,728 | 0.3 / 1.2 / 11.9 | 0.28 / 0.89 / 1.94 | 0.15 / 0.11 / 0.49 | 6.8 / 19.2 / 36.6 |

Candidate pairs grow about N^2.1 up to 1,000 incidents (`max_df` = 20 % of N admits values held by thousands of
incidents) and about N^1.7 from 900 to 1,857, where `MAX_DF_ABS` = 200 binds. Scoring runs at 900-1,470 pairs/s per
process (sample of 1,500 pairs).
These counts predate the operator-tier retrieval backstop (CR-ASTRA-PIPE-201), which adds at most
2,016 or df x 4 pairs per operator-tier value above `max_df`. Measured 2026-11-21 on the server copy of the
837-incident run `analyse-20261121T164214-b8633e` (aggregate script, read-only DB): 9 postings above `max_df` = 167,
none operator-tier INF/META, so 89,357 candidate pairs with and without the backstop (retrieval 2.7 s vs 3.6 s at
load average ~130). The other corpora in the table were not re-measured.

Projection to 10k and 100k incidents (**estimated**, same corpus mix assumed; per-job envelope 12 workers unless
stated; not measured at that size):

| Stage | 10k | 100k | basis |
|---|---|---|---|
| genes | ~36 min (12 workers); 48 workers: 17-35 min, bound by the single DB writer | ~6 h (12); 3-6 h (48, writer-bound) | linear in N from the measured 180 s per 825 incidents (pool start and persist tail included); writer 0.1-0.2 s per incident on this host, 0.8 s under I/O pressure (#26) |
| candidate pairs | 3-8 M | 50-160 M | N^1.4 (lab, 50k synthetic ads) to the postings projection of corpus-demo with the 200 cap (`.scale-tools/project_pairs.py`); 17 M at 10k without the cap |
| pair scoring | 4-11 min | 1.2-3.7 h | 1,000 pairs/s per process x 12 |
| pair scores in memory | 5-15 GB | 80-250 GB | ~1.5 KB per `PairEval`; 100k needs scores for shown pairs only (section 3, item 3) |
| conformal (capped at 120 + 120 items) | 2-5 min | 25-45 min | one item 0.1-0.5 s at 1-2k incidents plus an O(N) rarity rebuild per pass-2 item (1.9 s at 1,857) |
| self-assignment (one item per incident) | 1-2.5 h | 16-46 h | measured 1.7 s of CPU per item at 825 (258 candidates per incident); candidates per incident grow with pairs / N, so items x cost is ~N^1.7. The dominant stage beyond ~5k incidents: it needs a cap like the conformal one |
| load_genomes | ~2-3 min | ~25-35 min | linear, ~13-20 ms per incident (single process) |

## 3. Bottlenecks, in order

1. **OCR**: about 7-11 images/s per process; the ceiling for screenshot-heavy material. Already cached by content
   hash and skipped on exact duplicates; production runs it on its own worker pool.
2. **Video VIS**: frame extraction and per-frame hashing/embedding make one video incident the long pole of the
   sample run. Production samples frames by scene cut first and runs video jobs on separate workers.
3. **Pair scoring at scale**: candidate pairs grow about N^1.4 and Python scoring runs at 3.5-12k pairs/s (lab).
   Vectorised scoring (numpy over pair arrays) with full evidence records built only for pairs that are shown or
   exported is the next step; it is designed but not measured.
4. **Enrichment rate limits**: on the public corpus crt.sh answered about 5 domains/min (7% of 2,500 in the budget)
   and several RDAP registries returned HTTP 429 at 1 request/s; production needs a shared enrichment service with
   per-server backoff and a longer budget.
5. **Candidate recall at scale**: the union of indexes recalled 0.61 of true pairs at 50k synthetic ads; per-index
   top-k has to grow with N, and thresholds have to be re-tuned at the real corpus size.

## 4. Target architecture

```
                 +-------------------+        +------------------------+
 analysts ---->  | API (FastAPI) x N |  --->  | PostgreSQL             |  contracts + migrations 1:1
 (SSO, roles)    | stateless, UI     |        | incidents, genes,      |  (genes table partitioned by family)
                 +---------+---------+        | pairs, campaigns, runs |
                           |                  | reviews, audit chain   |
                           v                  +-----------+------------+
                 +-------------------+                    |
                 | queue (Redis /    |                    v
                 | RabbitMQ)         |        +------------------------+
                 +----+----+----+----+        | graph read model       |
                      |    |    |             | Neo4j or Postgres      |
        +-------------+    |    +-----------+ | (export_neo4j today)   |
        v                  v                v +------------------------+
 +-------------+  +---------------+  +-------------+     +----------------------+
 | VIS workers |  | TXT/OCR       |  | INF workers | --> | enrichment service   |
 | (CPU/GPU)   |  | workers       |  | HAR, DOM    |     | cache + rate limits  |
 +------+------+  +-------+-------+  +------+------+     | RDAP DNS crt.sh ASN  |
        |                 |                 |            +----------------------+
        +--------+--------+--------+--------+
                 v                 v
        +-----------------+  +----------------------+
        | MinIO / S3      |  | linking job          |
        | originals (CAS) |  | rarity, candidates,  |
        | work files      |  | scoring, clustering  |
        +-----------------+  +----------------------+
```

| Component | Change | What stays the same |
|---|---|---|
| Database | PostgreSQL; the `MIGRATIONS` list maps 1:1 to Alembic or plain SQL migrations; JSON columns become `jsonb`; the single-writer rule becomes ordinary transactions | `hydra/contracts.py` models and table semantics |
| Originals | MinIO/S3 bucket with object key = sha256, object lock for evidence | content addressing, read-only originals, dedup |
| Jobs | queue with one topic per gene family (VIS, TXT/OCR, INF, ID) and a linking topic; stateless workers scaled per family; idempotent jobs keyed by (artifact sha256, extractor version) | extractor interface `hydra/genome/base.py`, gene fingerprint cache |
| Enrichment | one shared service in front of RDAP, DNS, crt.sh, Team Cymru: cache with TTL per source, global rate limits, replay for audits | `EnrichmentRecord` with source, query, `fetched_at`, sha256 |
| Linking | incremental: new incidents update document frequencies and are scored against candidates only; full re-clustering on a schedule; vectorised scoring | Fellegi–Sunter bits, tiers, thresholds, open-set assignment |
| Graph | Neo4j (Community or Enterprise) or Postgres as a read model rebuilt from the pairs and campaigns tables | campaigns and groups computed in HYDRA, not in the graph store |
| Access | SSO (OIDC), roles (analyst, reviewer, admin), TLS behind the institution's proxy; the API keeps binding to loopback or a private network | review actions and audit events per analyst |
| Review | multi-analyst queue: accept/reject link, merge/split campaign, with the analyst id in the hash-chained log; labels feed the LR + isotonic fusion model | `Review` contract, audit chain |
| Ad library and watchlist | scheduler as a periodic job on the queue; Meta Ad Library token held in a secret store | `hydra/scheduler.py`, `hydra/enrich/adlibrary.py` |

## 5. Migration steps (estimated effort, not measured)

1. Postgres behind `hydra/db.py` with the same migrations; run the evaluation harness on both backends and compare
   results byte for byte (the PERF parallel-vs-sequential check is the template).
2. CAS on S3 (an interface swap in `hydra/storage.py`).
3. Queue workers per gene family, reusing `hydra.parallel` job units (incident x extractor group).
4. Enrichment service with shared cache and rate limits.
5. Vectorised pair scoring, then incremental linking.
6. SSO, roles and the multi-analyst review queue.

## 6. What is not claimed

- Production throughput has not been measured; the target figures in §2 are from the sample run and the
  pre-start lab.
- The lab numbers are on synthetic and public data, measured on a shared server; they show scaling behaviour, not
  NASK performance.
