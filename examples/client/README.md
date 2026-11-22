# HYDRA NEXUS reference client

`hydra_client.py` implements the integration contract in `docs/INTEGRATION_CONTRACT.md` with the Python standard
library only (urllib, http.server, hmac, sqlite3, zipfile). Python 3.11+. It is both an importable module
(`HydraClient`, `Receiver`, `roundtrip_check`) and a CLI.

## Walk-through

The transcript below was recorded on 2026-11-22 against a throwaway API (`python -m tests.e2e.server --root
/tmp/g5probe/e2e-root --ready /tmp/g5probe/e2e-root/server.json`: own temp state, own free port, the synthetic
e2e corpus analysed first, HYDRA_AUTH=1). The submitted material is the synthetic incident `new-a5` of that corpus.

1. Get a token. Log in to the panel, Integracje & API -> Tokeny API, or `POST /v1/auth/tokens` with a session. The
   token is shown once. Submitting needs the analyst role; reading and verifying needs viewer.

   ```sh
   export HYDRA_URL=http://127.0.0.1:<port>
   export HYDRA_TOKEN=hnx_...
   ```

2. Pin the signing key once, out of band (compare with the operator):

   ```sh
   curl -s -H "Authorization: Bearer $HYDRA_TOKEN" $HYDRA_URL/v1/exports/signing-key
   ```

3. Run the whole contract:

   ```sh
   python examples/client/hydra_client.py run page.har Strona.html "Zrzut ekranu.png" --out /tmp/hydra-out \
       --pin SHA256:<pinned fingerprint>
   ```

   ```
   1. submitted: job 5b9bec88627642d2 (mode files, Idempotency-Key cli-f36697a40d2a4e1e9de8b53f809265f9)
   2. resubmitted with the same key: job 5b9bec88627642d2 replayed=True
   3. result (hydra.job_result.v1 v1): done, decision assign, campaign C-530fd1df, genes 173
   4. report: /tmp/g5probe/out/report.md
   5. package: 9798 bytes, signed by SHA256:1e50934d...70da99; pinned SHA256:1e50934d...70da99
   6. verified: ok=True files=3 errors=[]
      offline hash check: ok
   ```

   (Progress lines of this first recording are omitted: they printed a wrong progress key, fixed before the
   second run.) A second run on the synthetic incident `new-d1`, with `--pin`:

   ```
   1. submitted: job 07bf5c6354a0429b (mode files, Idempotency-Key cli-80827699363047cd958be05a69016357)
   2. resubmitted with the same key: job 07bf5c6354a0429b replayed=True
      progress 0.1% import
      progress 99.0% scoring
   3. result (hydra.job_result.v1 v1): done, decision new_campaign, campaign None, genes 200
   4. report: /tmp/g5probe/out2/report.md
   5. package: 10610 bytes, signed by SHA256:1e50934d...70da99; pinned SHA256:1e50934d...70da99
   6. verified: ok=True files=3 errors=[]
      offline hash check: ok
   ```

   Step 2 shows the idempotent resubmit: the same key returns the same job and starts nothing
   new. The report begins with the decision and its evidence:

   ```
   - decyzja / decision: **assign**, kampania / campaign: `C-530fd1df`
   Przypisano do kampanii C-530fd1df: 53,7 bit dowodów na poziomie kampanii, wsparcie 4 incydentów; ...
   ```

   Output files: `result.json` (v1 envelope), `report.md`, `package.zip` (signed). Exit code 0 only when the
   package verifies against the pinned key. Without `--pin` the client pins the key the server reports
   (trust on first use) and prints it.

4. Receive signed notifications on a loopback receiver (needs an admin token to register the endpoint):

   ```sh
   python examples/client/hydra_client.py receiver --port 8765 --secret "<16+ char shared secret>"
   # in Python, with an admin token:
   HydraClient(url, admin_token).add_webhook("http://127.0.0.1:8765/hook", "<same secret>", ["job"])
   ```

   Every finished job is POSTed with `X-Hydra-Signature`; the receiver prints each receipt with
   `signature_ok`. A wrong signature gets 401 (the sender marks the delivery dead), a stale timestamp (> 300 s)
   is rejected.

5. Check the graph export round trip:

   ```sh
   python examples/client/hydra_client.py roundtrip
   ```

   Recorded on the same server: 428 nodes (Incident 20, Campaign 11, CampaignGroup 1, Gene 396) and 1812 edges
   (HAS_GENE 1600, LINKED_TO 190, MEMBER_OF 20, IN_GROUP 2), identical in the ZIP CSV re-imported into SQLite, the
   `neo4j-admin` CSV and `graph.cypher`; 0 dangling edges; `"equal": true`.

## Retry rules

GET requests, and POST requests carrying an `Idempotency-Key`, are retried on timeouts, connection errors, 429 and
5xx (default 4 retries, backoff 0.5 s doubling). Other 4xx raise `HydraError` with `.status`, `.code` (the
machine-readable `detail.code`) and `.body`. A POST without a key is never retried. `submit_files` and
`submit_json` always send a key (random unless given), so a retried submit cannot create a second job.

## Tests

`tests/contract/` drives this client against the real app on 127.0.0.1 (`uv run pytest tests/contract`, about
12 s): auth required, role enforcement, idempotent resubmit and conflicts, client timeout + retry, result schema
versioning, report/package before completion, tampered package, foreign key, webhook retry/dead letter/manual retry,
receiver signature rules, CSV/Cypher round trip.
