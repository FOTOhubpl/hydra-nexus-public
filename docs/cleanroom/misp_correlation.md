# Clean-room behaviour spec: MISP-style correlation guards (over-correlation, exclusions, scoping, CIDR, fuzzy pre-filter)

## Source studied
- URL: https://github.com/MISP/MISP
- Commit: HEAD of the default branch, committed 2026-11-19T15:11:09+01:00 (shallow clone; hash not recorded)
- Licence: AGPL-3.0 (GitHub API, 2026-11-21)
- Files read: `app/Model/Correlation.php` (save-time correlation, exclusion matching, CIDR and fuzzy-hash paths),
  `app/Model/OverCorrelatingValue.php` (limit, block/unblock), `app/Model/CorrelationRule.php` (scoping rules),
  `app/Model/FuzzyCorrelateSsdeep.php` (chunk index), `app/Model/MispAttribute.php` (non-correlating type list),
  `app/Model/Warninglist.php` (list matching, for context)
- Date studied: 2026-11-21, by research agent c2_infra. Builders must not open these files.

## Purpose
Keep exact-value linking between incidents useful as the corpus grows. Hub values (shared IPs, common hashes, platform
hosts) are suppressed automatically and the suppression lifts again on its own; analysts get simple exclusion
patterns; groups of items that must not link to each other can be scoped out; IPs link to small enclosing networks;
fuzzy-hash comparison runs only on a cheap candidate set. HYDRA already uses rarity bits, which are a soft version of
the first point. This spec adds the hard, explainable guard on top.

## Inputs
- Gene observations: (incident id, unit id, kind, value), where unit is HYDRA's de-duplication unit (campaign or
  incident).
- Configuration: over-correlation limit K (default 20; HYDRA should scale it, e.g. max(20, 1% of units)), the
  non-linking kinds, exclusion patterns, scope rules, minimum CIDR prefix lengths, fuzzy threshold.

## Behaviour (numbered steps)
1. Non-linking kinds: some kinds never link (free-text comments, ports, plain integers/floats, HTTP methods, language
   codes). They may still be shown as coverage.
2. Composite values (host plus port, IP plus port) link on the first component only.
3. Exclusion patterns: each pattern is either an exact value, "ends with X" (written with a leading wildcard marker),
   "starts with X" (trailing marker) or "contains X" (markers on both ends). A value matching any pattern does not
   link. The match is reported with the pattern that matched.
4. Scope rules: a rule names a set of items by incident id list, by source/reporter, or by a case-insensitive pattern
   on the incident title/batch name. Two items covered by the same rule never link to each other through any value.
   They can still link to items outside the rule. Use this for one capture batch, one feed, or one analyst's test
   uploads.
5. Over-correlation: for each (kind, value), count the distinct units carrying it, excluding scoped-out pairs. If the
   count is above K, the value gets status `over_correlating` with its count: no bits, no edge, listed in a
   dedicated view. The check is recomputed whenever the corpus changes, so a value that falls to K or below links
   again. Bulk imports that are themselves hubs do not lift an existing block.
6. CIDR containment (optional edge type): an observed IP links to an observed network prefix that contains it, and a
   prefix links to observed IPs inside it, only when the prefix is at least /8 for IPv4 or /16 for IPv6 (MISP's
   guard). For HYDRA a much stricter floor is recommended (e.g. /22 or longer for IPv4, /40 for IPv6), with bits
   scaled to prefix size.
7. Fuzzy-hash pre-filter: for CTPH-style hashes, shorten runs of four or more identical characters to three in both
   parts of the hash, index every 7-character window, take only items sharing at least one window as candidates,
   then run the full comparison and keep pairs scoring at or above the threshold (MISP: 40 on a 0-100 scale). For
   TLSH, use an equivalent cheap bucket (builder's own design, e.g. a prefix of the digest body) before full
   distance.
8. Full rebuild visits each unordered pair once, from the lower to the higher item id.

## Parameters and thresholds
- K = 20 units by default (MISP default), configurable per kind.
- CIDR floors: /8 (IPv4) and /16 (IPv6) in MISP; HYDRA should use stricter floors.
- Fuzzy threshold 40 (ssdeep scale); window length 7; run cap 3.

## Outputs
- Per (kind, value): status (linking, excluded:<pattern>, scoped:<rule>, over_correlating:<count>), plus bits when
  linking.
- Over-correlation table: value, kind, count, first time blocked.
- Evidence-panel text, e.g. "over-correlating: in 27 units (> 20), ignored", "excluded by pattern *.example-cdn.net".

## Edge cases
- Redacted values (`[REDACTED]`) never link and never count.
- A value in exactly K units still links. Block only above K.
- Small corpora: K larger than the corpus means no blocks, and rarity bits still apply.
- Scope rules and over-correlation interact: count units after removing scoped-out pairs.
- Exclusion markers inside the value itself: patterns are analyst input; escape or reject ambiguous ones.

## What NOT to replicate
- No code, identifiers, table names, Redis key names, comments or method structure from MISP.
- Do not copy MISP's non-correlating type list verbatim. Derive HYDRA's list from its own gene kinds.
- Do not replicate the database-specific query optimisations, job progress handling or ACL behaviours.

## Suggested acceptance tests
1. 30 synthetic incidents, one IP in 25 of them (K=20): the IP has status over_correlating(25), 0 bits, no edge;
   remove 10 of those incidents: the IP links again on recompute.
2. Exclusion `*.cdn-example.test` stops `a.cdn-example.test` from linking and names the pattern; `cdn-example.test.evil`
   still links.
3. Scope rule covering incidents 1-5 (same batch): no edges among them through a shared value; edge 1-to-6 still
   present.
4. IP 203.0.113.9 and prefix 203.0.113.0/24 observed: linked; prefix 10.0.0.0/7: never linked.
5. Two fuzzy hashes sharing no 7-character window are never fully compared (count comparisons in a test).

## Similarity-check reminder
Before merging, run the similarity check of the new HYDRA files against https://github.com/MISP/MISP (no long
identical token sequences; no shared identifiers). Record the result here and add the DISCLOSURE.md line: "behaviour
studied from MISP (AGPL-3.0); independent implementation, no MISP code".
