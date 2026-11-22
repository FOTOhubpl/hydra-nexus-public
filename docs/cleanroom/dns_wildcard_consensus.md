# Clean-room behaviour spec: wildcard-aware DNS consensus

## Source studied
- URL: https://github.com/laramies/theHarvester
- Commit: HEAD of the default branch, committed 2026-11-21T16:58:01Z (shallow clone; hash not recorded)
- Licence: GPL-2.0 (GitHub API, 2026-11-21)
- Files read: `theHarvester/lib/dns_consensus.py` (validation and classification), `theHarvester/lib/recursive_dns.py`
  (caller), `theHarvester/lib/takeover_evidence.py` (evidence shape only)
- Date studied: 2026-11-21, by research agent c2_infra. Builders must not open these files.
- Related permissive prior art (concept only): OWASP Amass rejects answers when a per-registrable-domain wildcard
  detector fires (`engine/plugins/support/resolvers.go`, Apache-2.0).

## Purpose
Decide, for a hostname seen in an incident, whether its DNS answer is specific to that hostname or comes from a
wildcard zone that answers any label. If the answer is wildcard-indistinguishable, the IP/CNAME evidence belongs to
the zone (registrable domain), not to the host. Many random subdomains of one wildcard zone must not count as many
independent host-level matches. A second goal is a majority vote across resolvers, so that one censoring, failing or
filtering resolver does not decide.

## Inputs
- Candidate hostname (normalised: lower case, no trailing dot, IDNA A-label).
- Its registrable domain (apex) from the PSL helper HYDRA already uses (tldextract with private suffixes).
- A list of exactly three distinct resolver addresses (configurable; e.g. two public resolvers plus one more).
- Optional query budget (maximum number of DNS queries for the whole run).
- The replay cache: in replay mode all answers come from the cache, and the random control labels must be stored with
  the record so a replay is deterministic.

## Behaviour (numbered steps)
1. Normalise the candidate. If it is not under the apex, or it equals the apex, there are no intermediate depths and
   the only control depth is the apex itself.
2. Build the list of control depths: the apex, then each parent of the candidate that is strictly longer than the
   apex and strictly shorter than the candidate. Example: candidate `a.b.c.example.pl`, apex `example.pl` -> depths
   `example.pl`, `c.example.pl`, `b.c.example.pl`.
3. For every depth, create three control names: a fresh random label (long enough to be unregistered, e.g. a
   prefix plus 32 hex characters) prepended to the depth.
4. Query the candidate once on each of the three resolvers. Query each control name on each resolver. The records are
   A, AAAA and the CNAME chain.
5. Normalise every response: sort and canonicalise IPv4/IPv6 strings, lower-case CNAME targets and strip trailing
   dots, keep the response code (NOERROR, NXDOMAIN, NODATA, SERVFAIL, ERROR), and record an error type instead of
   raising.
6. A response's signature is the set of its addresses and CNAMEs. A response has records if it has at least one
   address or CNAME.
7. Classify, in this order:
   a. Wildcard-indistinguishable: some candidate response with records has a signature equal to the signature of
      some control response at the closest depth (the deepest one, with the most labels) that also has records.
   b. Current: at least two of the three candidate responses contain an address.
   c. Not current: at least two candidate responses are clean negatives (no address, no error, code NOERROR,
      NXDOMAIN or NODATA).
   d. Resolver-disputed: anything else.
   If the query budget ran out during this candidate, the class is resolver-disputed.
8. The host's usable records are the union of the candidate answers only (never the controls).
9. Keep every individual answer (resolver, query name, depth, code, records) as evidence.

## Use in HYDRA (gene effects; decided by the builder with the architect)
- Wildcard-indistinguishable: emit a platform-tier gene meaning "zone answers any label" for the apex. Re-key the
  host's IP, /24 and CNAME genes to the apex (one observation per zone), and mark host-level DNS genes as derived
  from a wildcard.
- Current: unchanged behaviour, with resolver agreement noted.
- Not current: DNS genes status `unavailable` (taken down). Never a penalty.
- Resolver-disputed: `unavailable`, with a note. Never a negative signal.

## Parameters and thresholds
- Resolvers: exactly 3 distinct addresses (refuse to run otherwise).
- Controls: 3 random names per depth.
- Majority: 2 of 3 for current and for not current.
- Wildcard check uses only the closest (deepest) control depth. Shallower depths are queried for evidence.
- Per-query timeout: same as the existing dig call (4 s, 1 try). Overall budget configurable.

## Outputs
Per candidate: class (one of four), the union of candidate records, the list of all individual answers, the control
names used, and the apex. Cached under the existing enrichment cache key scheme, so replays are offline and
deterministic.

## Edge cases
- Wildcard CNAME to a CDN, with control answers that differ in IP rotation order: compare sets, not lists. If CDN
  rotation gives different IP sets per query, the signatures may not match. Then also treat "same CNAME target" as
  equal (builder decision; document it).
- Only a deeper level is wildcarded (`*.b.example.pl`): handled by the closest depth.
- The apex itself is the candidate: control is a random label under the apex.
- IDN names: work on A-labels.
- Private-suffix platforms (e.g. a hosting suffix in the PSL private section): the apex is the customer label, which
  is correct.
- Resolver returns a block-page IP (filtering resolver): it looks like current on one resolver only, so it falls to
  disputed unless two agree.

## What NOT to replicate
- No code, identifiers, class or enum names, data structures or comments from the source files.
- Do not replicate the takeover-evidence model or the recursive discovery loop (out of scope).
- Do not add brute-force subdomain discovery. HYDRA only validates hosts it already observed.

## Suggested acceptance tests
1. Replay fixture: zone `wild.test` answers 203.0.113.7 for every label. Candidates `x1.wild.test` and `x2.wild.test`
   are wildcard-indistinguishable; HYDRA produces one apex-level IP gene and zero host-level IP links between two
   incidents that differ only in subdomain.
2. Fixture where the candidate answers 198.51.100.5 and controls are NXDOMAIN: current; host-level genes unchanged.
3. One resolver SERVFAIL, two answer: current. Two SERVFAIL: disputed, gene status unavailable.
4. Deep wildcard `*.b.example.test` with candidate `a.b.example.test`: wildcard detected at depth `b.example.test`.
5. Determinism: a second run in replay mode reuses the stored control names and gives identical output.

## Similarity-check reminder
Before merging, run the similarity check of the new HYDRA files against https://github.com/laramies/theHarvester (no
long identical token sequences; no shared identifiers beyond generic DNS terms). Record the result here and add the
DISCLOSURE.md line: "behaviour studied from theHarvester (GPL-2.0); independent implementation, no theHarvester code".

## Implementation record
- 2026-11-21, ENRICH-PLUS builder : `hydra/enrich/dns_consensus.py`, gene effects in `hydra/genome/inf.py`, tests `tests/genes_inf/test_dns_consensus.py` (all five suggested acceptance tests plus the ADOPT #11 fifty-subdomain test). Built from this spec only; the source was not opened. Builder decisions: wildcard match needs two resolvers; equal CNAME targets count as equal; budget-exhausted probes are not cached. Details: ADR-007 amendment 2026-11-21, internal notes, not published section 9.
- Similarity check: pending (to be run by a research agent; the builder may not open the source).
