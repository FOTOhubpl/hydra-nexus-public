# Clean-room spec: phone-number tiering (behaviour studied from PhoneInfoga; length weight from followthemoney)

## Source studied
- https://github.com/sundowndev/phoneinfoga, commit 55807b0, GPL-3.0. Files read: `lib/number/number.go`,
  `lib/remote/googlesearch_scanner.go` (research side only), 2026-11-21.
- followthemoney phone type (`types/phone.py`, MIT), for the length-based specificity idea.

## Purpose
Phone numbers are strong operator evidence only when they are specific to the operator. Bank hotlines, public
receive-SMS numbers and premium short codes are shared by unrelated parties and must not link campaigns.

## Inputs
A raw phone string from OCR, page text or a chat log; an optional default region (PL).

## Behaviour
1. Normalise to E.164 with default region PL; record the number type (mobile, fixed, VoIP, toll-free, premium,
   short code) with a permissive phone library (for example the Apache-2.0 `phonenumbers` port).
2. Specificity weight: 0 below 7 national digits, rising linearly to full weight at 11 digits.
3. A number listed as an official hotline of a brand in HYDRA's brand lexicon becomes boilerplate tier.
4. A number on HYDRA's own list of public receive-SMS numbers becomes boilerplate tier. The list is built
   independently; the source only showed that such sites exist.
5. Toll-free, premium and short codes are platform tier.
6. VoIP numbers lose about 2 bits (lower cost to rotate, less specific to one operator). This is an assumption.
7. A number present in more than 3 campaigns that share no other operator-tier gene is demoted to platform tier and
   raised as an analyst alert.
8. Any other number is operator tier. The gene always records the tier reason and the list version used.

## Parameters and thresholds
7 and 11 digits; 2-bit VoIP penalty; demotion when seen in more than 3 otherwise unrelated campaigns. All are
assumptions to be checked on the corpus.

## Outputs
A phone gene with E.164 value, type, tier, tier reason and specificity weight.

## Edge cases
Numbers written with spaces, dots, the 0048 or +48 prefix, or homoglyph digits; numbers split across OCR lines.

## What NOT to replicate
Search-engine dorks, online scanners and lookups, the source's list of sites, Go structure and names.

## Suggested acceptance tests
1. Two pages sharing a bank's hotline: no campaign link from the phone.
2. Two pages sharing an unlisted mobile number: an operator-tier link.
3. A 6-digit code gets weight 0.
4. A number in 5 otherwise unrelated campaigns is demoted and alerted.

## Similarity-check reminder
Run the token similarity check against PhoneInfoga before merge and record the result here.

## Similarity-check result (2026-11-21, builder OPERATOR-GENES)
- Implementation: `hydra/genome/operator_ids.py` (`phone_profile`, `specificity`, `find_intl_phones`, `tier_phone`),
  `hydra/genome/assets/phone_lists.json`, tests `tests/genes_txt/test_operator_ids.py`. Written from this spec and
  the `phonenumbers` documentation only; the builder did not open PhoneInfoga.
- Reference: sundowndev/phoneinfoga @ 55807b05b753dab7bc6fe67403ca385668dda179 (shallow clone on the server in
  `/tmp/opgenes_simcheck/repos`, never read; 122 text files, 123 360 tokens).
- Method: `python3 -I scripts/simcheck_cleanroom.py <our files> -- <repo>` (language-agnostic tokens without
  comments; aggregate numbers only).

| Our file | Tokens | Longest raw run | Longest normalised run | Shared raw 12-grams | Shared 12+ char strings |
|---|---|---|---|---|---|
| hydra/genome/operator_ids.py | 5863 | 4 | 25 | 0 | 0 |
| hydra/genome/assets/phone_lists.json | 58 | 4 | 44 | 0 | 0 |
| tests/genes_txt/test_operator_ids.py | 2191 | 4 | 19 | 0 | 0 |

The normalised run of 44 in the JSON file is the shape of a string-to-string map (`"STR": "STR",` repeated), not
shared content (raw run 4). Conclusion: no long identical token sequences, no shared literals or identifiers.
