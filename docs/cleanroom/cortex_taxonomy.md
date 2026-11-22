# Clean-room spec: enrichment verdict taxonomy and observable validation (behaviour studied from Cortex-Analyzers)

## Source studied
- Repository: https://github.com/TheHive-Project/Cortex-Analyzers (last commit 2026-11-19, as seen 2026-11-21).
  Licence: AGPL-3.0.
- Files read (research side only): the ChainAbuse analyzer manifest and module, the observable validation analyzer,
  the FileInfo verdict builder.
- Read on 2026-11-21 by research agent c6_osint.

## Purpose
Every enrichment result (RDAP, DNS, crt.sh, ASN, CERT Polska list, MISP warninglists, future ones) is summarised
as short typed verdicts that the UI and exports can show uniformly, separate from the evidence bits.

## Inputs
- An enricher description: name, version, accepted indicator kinds (domain, url, ip, phone, iban, hash...), whether
  it needs network, and the verdict level rules.
- A raw enrichment record (already stored by HYDRA as an EnrichmentRecord).

## Behaviour
1. Each enricher declares its accepted kinds, version and network need in a small manifest.
2. The enricher returns the full raw report plus a list of verdicts. A verdict has: level (info, safe, suspicious,
   malicious), source (enricher name), predicate (what was measured) and value.
3. Level rules are declared in the manifest, as count thresholds. Example studied: zero abuse reports is safe,
   one to four suspicious, five or more malicious.
4. A built-in validation enricher marks an indicator suspicious if: its IDNA encoding fails, a hostname is longer
   than 255 characters, the label mixes scripts, or the string contains text-direction control characters
   (right-to-left override U+202E, left-to-right override U+202D, marks U+200E and U+200F, isolates U+2066 and
   U+2067).
5. Verdicts never add link bits. They are display and triage information only.
6. An enricher error becomes an info verdict with the error class, never an exception in the pipeline.
7. The enricher version is recorded with every verdict. In offline mode only enrichers with no network need run.

## Parameters and thresholds
Per-manifest count thresholds; the 255-character hostname limit; the control-character list above.

## Outputs
`verdicts` list per enrichment record and per indicator, included in the evidence export, STIX and MISP exports.

## Edge cases
Unicode normalisation is applied before the mixed-script check; punycode labels are decoded first.

## What NOT to replicate
Analyzer code, Python class layout, the Cortex job API and its JSON field names beyond the generic four-field verdict
idea.

## Suggested acceptance tests
1. A host on the CERT Polska list gives a malicious verdict.
2. A URL containing U+202E gives a suspicious verdict.
3. With HYDRA_OFFLINE=1 only offline enrichers run.
4. Exports carry the verdicts; link bits are unchanged with and without verdicts.

## Similarity-check reminder
Run the token similarity check against Cortex-Analyzers before merge and record the result here.
