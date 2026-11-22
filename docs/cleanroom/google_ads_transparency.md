# Clean-room behaviour spec: Google Ads Transparency Center pivot (domain → advertiser → creatives)

## Source studied
- URL: https://github.com/faniAhmed/GoogleAdsTransparencyScraper
- Commit: d5ab6d8 (2023-07-23). Licence: MIT-style permission text in `Licensce.txt` (no SPDX id). Studied 2026-11-21
  by research agent c3_coord.
- Files read: `GoogleAds/main.py`, `GoogleAds/regions.py`, README. This is a behaviour spec rather than a vendored copy,
  because the code is stale and the endpoints are undocumented.

## Purpose
Take a landing-page registrable domain or an advertiser keyword from a scam ad, find the Google advertiser account(s)
whose ads point to that domain, and list their creatives. This gives HYDRA an operator-tier identifier (Google advertiser
id, verified advertiser name, region) for Google "Sponsorowane" ads.

## Inputs
- A domain (registrable, punycode) or a free-text advertiser keyword. Optional region (country). Page size and page token.

## Behaviour
1. Open a session and load the public Transparency Center landing page once to get cookies (with the region parameter
   if one is set).
2. Keyword mode: post a suggestion request with the keyword and small limits (10 and 10 in the source). The response lists
   advertiser candidates (id, display name, region) and domain suggestions.
3. Domain mode: post a creative search filtered by the domain. The response contains creatives with advertiser id and
   name. Take the distinct advertisers.
4. Advertiser mode: post a creative search filtered by advertiser id, page size 40, plus the region number if set. Follow the
   returned page token until it is absent or a max-pages cap is reached. Record the total ad count the service reports.
5. Creative lookup: for an (advertiser id, creative id) pair, request the creative details. Extract the preview link and,
   if present, the image or video URL, plus first/last shown dates and format [U: dates not parsed by the source].
6. All requests are form-encoded with one JSON-valued field. Responses are JSON with numeric string keys. Map them to named
   fields in one adapter so a schema change breaks in one place.
7. Cache every response (replay mode) with query, timestamp and sha256, as the other HYDRA enrichers do (`hydra/enrich/client.py`).

## Parameters and thresholds
- Page size 40. Max pages per advertiser 5 (HYDRA choice). 1 request per 3 s (HYDRA choice; the source has no pacing).
  On HTTP 429/403, refresh the session with exponential backoff, at most 3 tries.

## Outputs
- Gene candidates: `google_advertiser_id` (operator tier), `google_advertiser_name`, `google_advertiser_region`,
  `google_creative_count`. Creative ids/URLs go to the inbox as new incident candidates.

## Edge cases
- Domains shared by many advertisers (shorteners, large platforms): skip them using the HYDRA platform list.
- An empty result is not evidence of absence (the archive is partial and region-scoped).
- Endpoint or schema change: return status `unavailable` with a Polish message, as the Meta connector does.

## What NOT to replicate
- Hard-coded browser headers or cookie values, proxy rotation for evasion, bulk crawling of all advertisers. Respect
  Google's terms. Default off and analyst-triggered only.

## Suggested acceptance tests
- A recorded fixture for one domain yields exactly the advertiser ids in the fixture, cached and replayable offline.
- A schema-drift fixture (missing key) yields status `unavailable` without a crash.
- Two incidents with different domains but the same advertiser id get an operator-tier link with the explanation
  "same Google advertiser <id>".

## Similarity-check reminder
Write the client from this spec with your own identifiers. Run a similarity check against `GoogleAds/main.py` before merging.

## Implementation record
- 2026-11-21, ENRICH-PLUS: not implemented. The endpoints are undocumented and compliance with Google terms is unclear; left for a operator decision (ADR-007 amendment 2026-11-21, item 6).
