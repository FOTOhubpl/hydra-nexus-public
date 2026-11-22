# Clean-room spec: kit file-name-set fingerprint gene

## Source studied
- URL: https://github.com/t4d/PhishingKit-Yara-Rules. Last commit 2026-10-27 (SHA [U]). Licence AGPL-3.0.
- Files read: repository listing (931 `.yar` files) and several individual rules. Studied 2026-11-21.
- Related, not copied: StalkPhish (AGPL) and miteru (MIT) archive discovery logic.

## Purpose
Phishing and scam kits are distributed as archives with a characteristic set of directory and file names. Saved pages (`_files/` folders, capture `resources/`) and HAR request paths preserve those names even when the bytes change (re-minified JS, recompressed images, injected tracker IDs). HYDRA hashes asset bytes but has no gene for the name set. This spec adds a kit-tier gene for it.

## Inputs
For one incident:
- the list of landing-asset relative paths (saved-page folder or capture resources);
- the first-party request URL paths from the HAR (host in `first_party_hosts`);
- the file list of a kit archive, if one is attached as an artifact (read the listing only, never extract or execute).

## Behaviour
1. Collect candidate names: path basenames (lower-cased), plus the last 1-2 directory names for first-party request paths.
2. Drop names that carry no kit information:
   - names on the library allowlist (jquery, bootstrap, fontawesome, ...);
   - names that are content hashes or random (entropy above 3.5 bits/char and length >= 16, or hex/uuid shaped);
   - numeric-only names;
   - generic names (index.html, style.css, main.js, favicon.ico, logo.png).
   - Use HYDRA's existing library regexes and the warninglists.
3. Strip the "saved from" suffixes that browsers add (`(1)`, `.download`) and query strings.
4. If at least 4 distinctive names remain, emit:
   - `asset_nameset_hash`: hash of the sorted unique distinctive names (exact family match);
   - `asset_name_token`: one gene per distinctive name. These are rarity-weighted individually and live in the same dependency group, so the per-family cap applies.
   - optionally a MinHash signature over the distinctive names, for Jaccard-based variant matching. Report Jaccard in the explanation.
5. Tier kit, dependency group `inf:kit:asset_names`, quality 0.6 for the set hash and 0.4 for tokens. Calibrate with leave-one-family-out.

## Parameters and thresholds
- Minimum distinctive names: 4.
- Entropy threshold: 3.5 bits/char for names of length >= 16.
- MinHash: 64 permutations. A Jaccard of 0.6 or more counts as a candidate kit-variant link (to be tuned).

## Outputs
Genes as above, with evidence = list of the shared distinctive names, shown in the bit-waterfall.

## Edge cases
- WordPress and other CMS sites share many names (wp-content, wp-includes). Put CMS path families on the boilerplate list.
- Single-file pages (everything inline): no names. The gene is unavailable, not negative.
- Capture tools that rename resources to hashes: most names are dropped by entropy and the gene becomes unavailable.
- Duplicate submissions of one capture: identical gene. The rarity df accounts for it.

## What NOT to replicate
- No YARA rules or rule names from the studied repo.
- No brand/kit labels taken from it.
- No archive probing on live hosts.
- No extraction or execution of archives.

## Suggested acceptance tests
1. Two saves of the same synthetic kit, one with recompressed images and re-minified JS: they share `asset_nameset_hash`, while `asset_sha256` differs.
2. Two unrelated CMS sites: no shared gene after the boilerplate filters.
3. A capture with hashed resource names: the gene is unavailable.
4. A variant with 1 of 8 names changed: Jaccard about 0.78, reported in the explanation.

## Similarity-check reminder
Confirm that no rule text, name list or comment from the AGPL repository appears in our code or lexicons. The allowlists and boilerplate lists must be our own.

## Implementation and similarity-check result (2026-11-21, builder OPERATOR-GENES)
- Implementation: `hydra/genome/kit_names.py` (extractor `inf_kit_names@1`, kinds `asset_nameset_hash` q 0.6 and
  `asset_name_token` q 0.4, group `inf:kit:asset_names`), tests `tests/genes_inf/test_kit_names.py`. Written from
  this spec only; the builder did not open the YARA repository. Generic-name, CMS-path and library lists are our own.
- Deviations: the entropy filter (3.5 bits/char, length >= 16) applies only to names without `-`, `_` or space
  separators, because descriptive Polish names such as `baner-prezes-wywiad` exceed 3.5 bits/char (measured on the
  synthetic test name: dropped before the change). The optional MinHash signature is not emitted; per-name tokens
  give the rarity-weighted overlap, and `kit_names.jaccard` is available for explanations. Kit archive listings are
  not read (no archive artifacts are produced by ingest today).
- Reference: t4d/PhishingKit-Yara-Rules @ 7ea0bfbe1a86bcd4c7c86f568f72d3177bb0a131 (shallow clone on the server,
  never read; 934 files, 83 953 tokens). Method: `python3 -I scripts/simcheck_cleanroom.py`.

| Our file | Tokens | Longest raw run | Longest normalised run | Shared raw 12-grams | Shared 12+ char strings |
|---|---|---|---|---|---|
| hydra/genome/kit_names.py | 1345 | 5 | 20 | 0 | 0 |
| tests/genes_inf/test_kit_names.py | 678 | 3 | 21 | 0 | 0 |

Conclusion: no long identical token sequences, no shared rule text, names or literals.
