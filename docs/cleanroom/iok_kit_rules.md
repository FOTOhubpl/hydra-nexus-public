# Clean-room spec: named kit-indicator rule engine (IOK-like)

## Source studied
- URL: https://github.com/phish-report/IOK. Last commit 2025-04-24 (SHA [U]). Licence ODbL-1.0 (database share-alike; LICENSE covers the whole repo).
- Files read: `iok.go`, `urlscanio.go`, `nethttp.go` (skim), `logsource.yml`, about 10 files in `indicators/`, plus aggregate statistics over all 266 rules. Studied 2026-11-21.

## Purpose
Attach an explicit, human-readable "this page was built with kit K" verdict to an incident. A rule is a set of exact textual artefacts that an analyst wrote and that one kit family reliably leaves on a page. The verdict is kit-tier evidence for linking, and it must be explainable. Negative rules mark known legitimate or generic templates, so they are never used as link evidence.

## Inputs
For one incident, a page record with these facets:
- title: list of strings (all titles seen, including ones set by script)
- hostname: string (landing host)
- html: string (first document response body)
- dom: string (rendered DOM, if available)
- js: list of strings (every script body, external and inline)
- css: list of strings (every stylesheet body, external and inline)
- requests: list of URLs requested during load
- cookies: list of "name=value"
- headers: list of "Canonical-Key: value" for the first document response, sorted
Build the record from HAR, MHTML or saved HTML. A missing facet is an empty value, not an error.

Rule files: YAML, one rule per file, in a HYDRA-owned directory.

## Behaviour
1. At start-up, load every rule file. A rule has: id (default: file stem), title, description, references (list), tags (list), a `detection` map of named selections plus one `condition` string, and an optional `polarity` (positive by default, or negative).
2. A selection maps one or more facet expressions to values. A facet expression is a facet name followed by zero or more modifiers: contains, startswith, endswith, re, all. With no modifier, matching is equality.
3. Matching a value against a facet:
   - For a string facet, apply the operator to the string.
   - For a list facet, the expression is true if any element satisfies the operator.
4. A value list means OR, unless `all` is present, which means every listed value must be satisfied. With `all` on a list facet, each value may be satisfied by a different element.
5. Inside one selection, multiple facet expressions are ANDed.
6. Matching is case-sensitive by default. A rule-level flag may make it case-insensitive. Before matching, NFC-normalise text and collapse whitespace runs to one space in html/dom only, so line-wrapping changes do not break rules.
7. The condition supports selection names, `and`, `or`, `not`, parentheses, and `N of prefix*` / `all of prefix*` / `any of prefix*`.
8. Evaluate every rule against the record. Collect matches with the names of the selections that were true.
9. Emit one gene per positive match: kind `kit_rule`, value = rule id, tier kit, dependency group `inf:kit:kit_rule`, quality 1.0. Explanation text = rule title + the true selections + the facet and snippet offset of the first hit, truncated to 120 chars.
10. Emit a negative match as a `boilerplate` marker that suppresses the DOM/title/asset genes listed in the rule's `suppresses` field.
11. A rule that fails to parse or to compile a regex is reported once and skipped. It never raises inside the pipeline.

## Parameters and thresholds
- Regex timeout or size guard: scan at most the first 3 MB of any facet value (aligned with HYDRA MAX_TEXT_SCAN).
- Regex must be compiled with a linear-time engine or a timeout.
- Rule quality defaults to 1.0. A rule may set 0.3-1.0 if it is known to be broad.
- Recommended rule hygiene (from the observed corpus): 2-3 selections ANDed. At least one anchor that is not a brand name, e.g. hidden-input names, template meta description, kit-specific CSS/JS file name, or exfil endpoint path.

## Outputs
A list of matches: (rule id, title, polarity, tags, true selection names, evidence snippets with facet and offset). These become genes as above and appear in the bit-waterfall explanation and in STIX as an `indicator` with pattern type "hydra-kit-rule".

## Edge cases
- Empty or binary bodies: skip that element.
- Script-set titles: include all of them.
- Very large inline JSON in html: still subject to the 3 MB cap.
- Duplicate incidents: identical matches. The rarity weighting in scoring handles frequency, so the engine does not deduplicate.
- Rules that match almost everything (df > 30% of the corpus) are flagged at calibration time and down-weighted automatically.

## What NOT to replicate
- Do not copy, translate or bundle IOK `indicators/*.yml`. They are an ODbL database, and a derived DB would inherit share-alike.
- Write our own rules from the NASK sample and public reports.
- Do not reuse IOK field-name strings verbatim in documentation. The facet names above are generic HTTP/HTML terms.
- Do not fetch from urlscan at scoring time.

## Suggested acceptance tests
1. A synthetic rule with `html|contains|all` of two hidden-input names fires only when both are present.
2. A list facet: a `requests|endswith` rule fires when any request URL ends with the token.
3. A condition `a and not b` behaves correctly.
4. A negative rule suppresses `page_title`, so two parked-template incidents gain 0 bits from the title.
5. A bad regex in one rule file leaves the other rules working and logs exactly one warning.
6. The explanation contains the rule title and the selection names.

## Similarity-check reminder
Before merging, run a similarity check (e.g. token overlap or `diff`-style comparison) between our engine and rule files and the IOK repository. No identical rule texts, no copied comments, and no identical structure beyond the generic Sigma-style concept.

## Similarity-check result (2026-11-21, builder sub-agent of OPERATOR-GENES)
- Implementation: `hydra/genome/kit_rules.py` (engine, called from `inf_kit`), our own rules
  `hydra/genome/lexicons/kit_rules/syn_lead_kit.yml` (synthetic test kit) and `neg_parked_template.yml` (generic
  parked template), tests `tests/genes_inf/test_kit_rules.py`. No IOK rule was read, copied or translated.
- Reference: phish-report/IOK @ 142f762aa12994cbd26ddf7d40d40c07ac01bd66 incl. all `indicators/*.yml` (shallow
  clone on the server, never read; 280 text files, 44 528 tokens). Method: `python3 -I scripts/simcheck_cleanroom.py`.

| Our file | Tokens | Longest raw run | Longest normalised run | Shared raw 12-grams | Shared 12+ char strings |
|---|---|---|---|---|---|
| hydra/genome/kit_rules.py | 3188 | 5 | 28 | 0 | 0 |
| hydra/genome/kit_exfil.py | 3715 | 3 | 26 | 0 | 0 |
| hydra/genome/lexicons/kit_rules/neg_parked_template.yml | 73 | 5 | 15 | 0 | 0 |
| hydra/genome/lexicons/kit_rules/syn_lead_kit.yml | 81 | 5 | 18 | 0 | 0 |
| tests/genes_inf/test_kit_rules.py | 1016 | 8 | 18 | 0 | 0 |

Raw runs of 3-8 tokens are YAML keys shared by the Sigma-style concept (`detection`, `condition`, selection
syntax) and generic Python. Conclusion: no identical rule texts, no copied comments, no long identical sequences.
