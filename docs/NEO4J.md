# Neo4j graph read model (optional)

The NASK brief suggests a graph database ("Może Neo4j?"). HYDRA computes campaigns, groups and links itself
(`hydra/nexus.py`); Neo4j is an optional read model for analysts who want to explore the graph with Cypher. It is
rebuilt from an analyse run, never written back.

Licence: Neo4j 5 Community Edition is GPL-3.0. It runs as a separate server in its own container; HYDRA talks to it
only through `cypher-shell` in a one-shot loader and does not link to it or ship its code (`docs/LICENSES.md`).

## Start

```bash
bash scripts/neo4j_password.sh               # random password into ./.env (gitignored, mode 600) + host folders
docker compose --profile neo4j up -d         # api + neo4j + one-shot export and load of the latest run
```

To add only the graph next to an already running `api` service:
`docker compose --profile neo4j up -d neo4j neo4j-export neo4j-load`. After a new analyse run, reload with
`docker compose --profile neo4j run --rm neo4j-export && docker compose --profile neo4j run --rm --no-deps neo4j-load`.

| Service | What it does |
|---|---|
| `neo4j` | `neo4j:5.26-community`, host network, listens on 127.0.0.1 only: Browser http://127.0.0.1:7474, Bolt 127.0.0.1:7687 (`NEO4J_HTTP_PORT`, `NEO4J_BOLT_PORT`); heap 1 GB, page cache 512 MB; data under `~/HydraNexus/neo4j/` |
| `neo4j-export` | HYDRA image, runs `hydra export-neo4j /hydra/neo4j-import` (latest analyse run, `hydra/export_neo4j.py`): CSV files + `import.cypher` in the shared import folder; values pass through `hydra.redact` |
| `neo4j-load` | waits for `neo4j` healthy and the export finished, deletes the old graph, runs `import.cypher` with `cypher-shell`, prints node counts. The password reaches `cypher-shell` through `NEO4J_PASSWORD` in the environment, not a command line |

On the server, open the Browser through an SSH tunnel: `ssh -L 7474:127.0.0.1:7474 -L 7687:127.0.0.1:7687 hacknation`,
then http://127.0.0.1:7474 with user `neo4j` and the password from `.env`.

## Export paths: API = CLI

Since 2026-11-21  both paths render one graph built by `hydra.export_neo4j.build_graph`, so the
labels, relationship types, properties and row counts are the same; values pass through `hydra.redact`.

| Path | Output | Load it |
|---|---|---|
| `hydra export-neo4j <out> [--run R] [--min-df 2]` | folder: `incidents.csv`, `campaigns.csv`, `groups.csv`, `genes.csv`, `member_of.csv`, `in_group.csv`, `has_gene.csv`, `linked_to.csv`, `import.cypher` (LOAD CSV of those files), `graph.cypher` (self-contained) | copy the folder into Neo4j's `import/` and `cypher-shell -f import.cypher` (what the `neo4j-load` service does), or `cypher-shell -f graph.cypher` from anywhere |
| `GET /v1/export/neo4j?format=cypher` (default) | `hydra-<run>.cypher`: byte-identical to the CLI `graph.cypher`; `CREATE CONSTRAINT` + `UNWIND [ {...}, ... ] AS r MERGE ...` statements with the rows inline (500 rows per statement) | `cypher-shell -u neo4j -f hydra-<run>.cypher`, or paste into the Browser |
| `GET /v1/export/neo4j?format=zip` (alias `csv`) | `neo4j-<run>.zip`: the CLI folder in `neo4j-<run>/` | unzip into `import/`, `cypher-shell -f import.cypher` |
| `GET /v1/export/neo4j?format=nodes_csv` / `edges_csv` | one node file (all labels, internal `:ID` = `<Label>:<key>`, the key kept as property `id` / `key`) and one relationship file (all types) | `neo4j-admin database import full --nodes=nodes.csv --relationships=edges.csv` |

Query parameters of the API: `run_id` (default: the newest analyse run, as in the UI), `min_p` (drop links below this
probability; default 0 = all scored pairs, as the CLI), `min_df` (default 2, as the CLI). In the UI the whole-run
exports sit on the graph page, panel "Eksport przebiegu" ("Neo4j Cypher", "Neo4j CSV (ZIP)", "Zbiór danych (ZIP)").
Every API download is recorded as an `EXPORT` event (`neo4j_cypher`, `neo4j_zip`, `neo4j_nodes_csv`,
`neo4j_edges_csv`).

Before 2026-11-21 the API wrote only Incident/Campaign nodes and a `LINKED` relationship, so queries 1 and 3 below did
not run on it. The subgraph download of a graph selection (`POST /v1/graph/export` with `format=cypher`, button
"Eksport Neo4j Cypher" in the selection panel) uses the same model since 2026-11-21 (`export_neo4j.subgraph`): the
selected incidents, their campaigns and groups, links with both ends selected and the gene values they hold.

## Graph model

- `(:Incident {id, title, observed_at, source})`: campaign members of the run and the endpoints of exported links
- `(:Campaign {id, label, size, confidence, scheme, run_id})`, `(:CampaignGroup {id, label, confidence})`
- `(:Gene {key, kind, value, family, doc_freq, tier, level})`: one node per exact (kind, value) shared by at least 2 incidents;
  stop-genes and embeddings are not exported. `key` is `<kind>|sha256(kind|value)[:32]` (a salted fingerprint for a
  value changed by redaction), never the value itself; match on `kind` + `value` in queries, not on `key`
- Serialisation (CR-ASTRA-SEC-10/11): CSV cells are redacted and a text cell starting with `=`, `+`, `-`, `@` gets a
  leading apostrophe; Cypher string literals are double-quoted with every quote, backslash and control character
  escaped (`hydra.export_safe`)
- `(Incident)-[:MEMBER_OF]->(Campaign)`, `(Campaign)-[:IN_GROUP]->(CampaignGroup)`
- `(Incident)-[:HAS_GENE {family, provenance}]->(Gene)`
- `(Incident)-[:LINKED_TO {bits, probability, hypothesis, calibrated, model, top_reason}]->(Incident)`: every scored
  pair, including `independent` ones

Since 2026-11-22 gene nodes also carry HYDRA's own evidence rules: `tier` (`hydra.synapse.gene_tier`: operator,
platform, kit, asset, persona, phrase, boilerplate, label; empty when the gene has none) and `level`
(`hydra.synapse.level`: campaign, kit, asset, none: how a match counts in `score_pair`). Queries can filter on
`g.tier = 'operator'` instead of a kind list; the kind list in query 1 mirrors the tier table in
`hydra/genome/webparse.py` and `hydra/genome/txt.py`. Near-duplicate visual matches (for example a pHash distance
of 6) are not exact values, so they appear on `LINKED_TO` (bits, `top_reason`), not as shared `Gene` nodes.

## Three analyst queries

1. **Incidents sharing an operator gene** (who shares a tracker id, lander, operator id, backend host, phone...):

```cypher
MATCH (a:Incident)-[:HAS_GENE]->(g:Gene)<-[:HAS_GENE]-(b:Incident)
WHERE a.id < b.id AND g.kind IN ['tracker_id','operator_id','tds_value','lead_endpoint','landing_host',
  'registrable_domain','linked_host','host_contacted','capi_token_hash','entity_phone','entity_email',
  'entity_iban','platform_page_id','platform_ad_id']
RETURN g.kind AS kind, g.value AS value, g.doc_freq AS incidents, collect(DISTINCT a.id + ' - ' + b.id) AS pairs
ORDER BY incidents DESC, kind
```

2. **Campaigns of a group** (similar campaigns on one kit or platform):

```cypher
MATCH (c:Campaign)-[:IN_GROUP]->(grp:CampaignGroup)
OPTIONAL MATCH (i:Incident)-[:MEMBER_OF]->(c)
RETURN grp.id AS group_id, grp.label AS group_label, c.id AS campaign, c.label AS label, collect(i.id) AS incidents
ORDER BY group_id, campaign
```

3. **Shortest evidence path between two incidents**, either through scored links that are not `independent`, or
   only through rare shared values (doc_freq at most 3, narrative genes excluded):

```cypher
MATCH (a:Incident {id: 'nask-1'}), (b:Incident {id: 'nask-61'})
MATCH p = shortestPath((a)-[:LINKED_TO|HAS_GENE*..6]-(b))
WHERE all(r IN relationships(p) WHERE type(r) = 'HAS_GENE' OR r.hypothesis <> 'independent')
RETURN p;

MATCH (a:Incident {id: 'nask-3'}), (b:Incident {id: 'nask-61'})
MATCH p = shortestPath((a)-[:HAS_GENE*..6]-(b))
WHERE all(n IN nodes(p) WHERE n:Incident OR (n.doc_freq <= 3 AND n.family <> 'NAR'))
RETURN p;
```

The three queries are checked without a Neo4j server by `tests/core/test_neo4j_dataset_api.py`: it extracts the
`cypher` blocks of this file and asserts that every label, relationship type and property they use is written by
both the CLI `import.cypher` and the API script on a synthetic run, that the API script equals the CLI `graph.cypher`
with the same rows as the CSV files, and that the script is syntactically sane (balanced brackets and quotes, every
statement ends with `;`, only known clauses). This is a static check, not an execution in Neo4j; the server run
below predates the alignment and used `import.cypher`.

A gene-only path is not weighted by bits: without the doc_freq and family filter, the shortest path between 3 and
61 on the sample went through the impersonated media brand (a narrative gene, not evidence). The HYDRA pair view
("Dowód pary") remains the authoritative explanation of a link.

## Measured on the server (2026-11-21)

Setup: test stack `-p hydra-neo4jtest` (own image tag, state copy of `state-int` with the sample run
`analyse-20261121T135359-9d99aa`, own Neo4j folder) after the profile commit; afterwards the
stack, image, state copy and Neo4j folder were removed.

| Step | Result |
|---|---|
| `up -d neo4j neo4j-export neo4j-load` (image already pulled) | 17 s until the loader finished; Neo4j healthy, export and load exit 0 |
| Export (`hydra export-neo4j`, container start included) | 5.8 s: 10 incidents, 7 campaigns, 1 group, 1,678 gene nodes, 3,798 HAS_GENE, 45 LINKED_TO |
| Load (delete old graph + `import.cypher` + counts) | 8.2 s |
| Queries 1-3 through `cypher-shell` (container exec included) | 4.9 s for all three |
| Query 1 | shared operator values in 7 kinds; for example `operator_id` 9 values among pairs of 1, 2, 3, 48, 61; one phone entity shared by 48 and 61 |
| Query 2 | one group with 2 campaigns, 5 incidents |
| Query 3 | 1 → 61: one `LINKED_TO` hop (`shared_template`); 3 → 61 over rare values: 2 hops through a shared price entity |
| Ports | 7474 and 7687 bound to 127.0.0.1 only (`ss -ltn`) |
| Browser through SSH tunnel | login page served (HTTP 200, screenshot taken locally, not committed); a logged-in graph view was not captured |
| Image | `neo4j:5.26-community`, 934 MB on disk |

## End-to-end proof on synthetic data (2026-11-22, branch docs/method-map)

Purpose: show the whole path working (analyse, export, compose profile, load, Cypher) on data that may leave the
machine. Everything was synthetic; no organiser data was used.

Setup on the server (`the Linux server`): own clone at `e2cd22f` plus this branch's changes, own state and Neo4j folders
(`HYDRA_NEO4J_SUBDIR=methodmap-neo4j`), own compose project `-p hydra-methodmap`, ports 17474/17687 (127.0.0.1 only).

```bash
uv run python -m tests.fuzz.jury_corpus graphdemo "$R/demo"        # 17 synthetic incident folders
HYDRA_STATE_DIR="$R/state" uv run hydra analyse "$R/demo"          # pinned to 8 cores with taskset
HYDRA_STATE_DIR="$R/state" uv run hydra export-neo4j ~/HydraNexus/methodmap-neo4j/import
bash scripts/neo4j_password.sh
export HYDRA_NEO4J_SUBDIR=methodmap-neo4j NEO4J_HTTP_PORT=17474 NEO4J_BOLT_PORT=17687
docker compose -p hydra-methodmap --profile neo4j up -d neo4j
docker compose -p hydra-methodmap --profile neo4j run --rm --no-deps neo4j-load
```

Corpus (`tests/fuzz/jury_corpus.py:build_graph_demo`): three planted campaigns (5, 4, 3 incidents) with their own
host, phone, IBAN and pixel; campaign 1 reuses the phone of campaign 0; one extra incident uses the landing host of
campaign 2 and the IBAN of campaign 0 (a planted bridge); four unrelated singletons.

| Step | Measured |
|---|---|
| `hydra analyse` | run `analyse-20261122T050049-888e89`: 17 incidents, 3,417 genes, 136 pairs, 6 campaigns (2 multi-member). Campaigns 0 and 1 were merged into one campaign of 9: the shared phone is an operator gene, which may link on its own (rule in `synapse.score_pair`). The bridge incident stayed in campaign 2 |
| Export | 0.72 s: 17 Incident, 6 Campaign, 331 Gene, 1,449 HAS_GENE, 136 LINKED_TO |
| Neo4j healthy after `up -d neo4j` | about 18 s (image `neo4j:5.26-community` already pulled) |
| `neo4j-load` (delete old graph, `import.cypher`, counts) | 8.7 s; Campaign 6, Gene 331, Incident 17 |
| Queries P1-P5 below through `cypher-shell` | 3.45 s for all five |
| Ports | `127.0.0.1:17474` and `127.0.0.1:17687` only (`ss -ltn`) |
| Clean-up | container `hydra-methodmap-neo4j-1` stopped and removed |

Two bugs found and fixed on the way (export/import path only, no scoring change):

1. The `neo4j` healthcheck probed the bolt port from `NEO4J_BOLT_PORT`, which is not passed into the container, so it
   always probed 7687. With a non-default bolt port the service stayed `unhealthy` (observed: not healthy after
   120 s) and `neo4j-load`, which waits for `service_healthy`, would never run. The check now reads
   `NEO4J_server_bolt_listen__address`; after the fix the service was healthy in about 18 s on port 17687.
2. `import.cypher` loaded the spreadsheet-safety apostrophe of `hydra.export_safe.csv_cell` into Neo4j: a phone
   `+48...` became `'+48...` on the LOAD CSV path but stayed `+48...` in `graph.cypher`, so the two paths did not hold
   the same values. `LOAD CSV` now strips exactly that prefix (`export_neo4j._cast`). Checked after the fix:
   `MATCH (g:Gene {kind: "entity_phone"}) RETURN g.value` returns `+48607153581` and `+48786977899`.

### Proof queries P1-P5 (tier-aware)

P1. **Members of each campaign**

```cypher
MATCH (i:Incident)-[:MEMBER_OF]->(c:Campaign)
WHERE c.size > 1
RETURN c.id AS campaign, c.label AS label, c.size AS size, collect(i.id) AS members
ORDER BY size DESC, campaign;
```

```text
"C-73bfa7b7", "Inwestycja z bankiem 0", 9, [nask-incydent_00001 ... nask-incydent_00009]
"C-eb2d3d31", "Kryptowaluty dla seniorów 4", 4, [nask-incydent_00010 ... nask-incydent_00013]
```

P2. **Rare evidence genes shared by two campaigns** (no phrase, narrative or boilerplate genes)

```cypher
MATCH (c1:Campaign)<-[:MEMBER_OF]-(a:Incident)-[:HAS_GENE]->(g:Gene)<-[:HAS_GENE]-(b:Incident)-[:MEMBER_OF]->(c2:Campaign)
WHERE c1.id < c2.id AND g.doc_freq <= 10 AND g.level <> 'none' AND NOT g.tier IN ['phrase', ''] AND g.family <> 'NAR'
RETURN c1.label AS campaign_1, c2.label AS campaign_2, g.tier AS tier, g.kind AS kind, g.value AS value,
  g.doc_freq AS incidents, collect(DISTINCT a.id) AS in_1, collect(DISTINCT b.id) AS in_2
ORDER BY campaign_1, campaign_2, kind;
```

```text
"Inwestycja z bankiem 0", "Kryptowaluty dla seniorów 4", "operator", "entity_iban", "h:2f5de6360a9be72b", 6,
  [nask-incydent_00001 ... nask-incydent_00005], ["nask-incydent_00013"]
(+ 24 rows among the four singletons: URL skeletons of one generator template, tiers operator / kit / platform)
```

The IBAN is shown as a salted fingerprint (`h:...`) because `hydra.redact` masks IBANs in every export. The singleton
rows are an artefact of the synthetic generator (one URL pattern `ogrod-#-przepisy.pl` for all four); HYDRA did not
link those incidents.

P3. **Bridge incidents**: the only member of its campaign that holds an operator value of another campaign

```cypher
MATCH (x:Incident)-[:MEMBER_OF]->(home:Campaign), (x)-[:HAS_GENE]->(g:Gene {tier: 'operator'})<-[:HAS_GENE]-(y:Incident)-[:MEMBER_OF]->(other:Campaign)
WHERE other <> home
  AND NOT EXISTS { MATCH (home)<-[:MEMBER_OF]-(z:Incident)-[:HAS_GENE]->(g) WHERE z <> x }
RETURN x.id AS bridge, home.label AS own_campaign, collect(DISTINCT other.label) AS reaches,
  collect(DISTINCT g.kind + '=' + g.value) AS via
ORDER BY bridge;
```

```text
"nask-incydent_00013", "Kryptowaluty dla seniorów 4", ["Inwestycja z bankiem 0"], ["entity_iban=h:2f5de6360a9be72b"]
(+ the four singletons through the template skeleton, as in P2)
```

This finds the planted bridge. Inside one campaign HYDRA reports bridges and articulation members itself
(`hydra.campaign_support.member_support`, campaign view); this query covers bridges between campaigns.

P4. **Operator identifiers reused across campaigns**

```cypher
MATCH (i:Incident)-[:HAS_GENE]->(g:Gene {tier: 'operator'}), (i)-[:MEMBER_OF]->(c:Campaign)
WITH g, collect(DISTINCT c.label) AS campaigns, collect(DISTINCT i.id) AS incidents
WHERE size(campaigns) >= 2
RETURN g.family AS family, g.kind AS kind, g.value AS value, campaigns, incidents
ORDER BY kind, value;
```

```text
"INF", "entity_iban", "h:2f5de6360a9be72b", ["Inwestycja z bankiem 0", "Kryptowaluty dla seniorów 4"], [00001 ... 00005, 00013]
"INF", "url_digit_skeleton", "ogrod-#-przepisy.pl", ["Poradnik 14", "Poradnik 16", "Poradnik 15", "Poradnik 17"], [00014 ... 00017]
```

The phone reused by campaigns 0 and 1 is not listed because HYDRA merged those two campaigns (P1).

P5. **Shortest path between two incidents of different campaigns through operator values only**

```cypher
MATCH (a:Incident {id: 'nask-incydent_00001'}), (b:Incident {id: 'nask-incydent_00010'})
MATCH p = shortestPath((a)-[:HAS_GENE*..8]-(b))
WHERE all(n IN nodes(p) WHERE n:Incident OR n.tier = 'operator')
RETURN length(p) AS hops, [n IN nodes(p) | coalesce(n.id, n.kind + '=' + n.value)] AS path;
```

```text
4, ["nask-incydent_00001", "entity_iban=h:2f5de6360a9be72b", "nask-incydent_00013",
    "linked_host=bonus-0004-223872.shop", "nask-incydent_00010"]
```

The path runs through the bridge: the IBAN of campaign 0, then the landing host of campaign 2.
