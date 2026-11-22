# Clean-room behaviour spec: Meta Ad Library bulk collector with snapshot media and removal status

## Source studied
- URL: https://github.com/Paularossi/AdDownloader
- Commit: a682599 (2026-07-06). Licence GPL-3.0. Studied 2026-11-21 by research agent c3_coord.
- Files read: `AdDownloader/adlib_api.py`, `AdDownloader/media_download.py`, `AdDownloader/helpers.py` (selected
  functions), README. No code copied. This spec is written from observed behaviour.

## Purpose
Collect ads from the Meta Ad Library API for a query, page through all results robustly, and keep the EU transparency
(DSA) fields. Fetch creative media from the public snapshot page and tell apart three snapshot states: media present,
text-only ad, and ad removed by the platform for a policy violation.

## Inputs
- Access token (environment variable; never written to disk or logs).
- Query: search terms or a list of page ids; reached countries (PL); delivery date min/max; ad type (ALL or political);
  optional languages, publisher platforms, media type, search type (exact phrase or unordered words).
- Output directory and run name.
- For the media step: ad ids with snapshot URLs, optional sample size and seed.

## Behaviour
1. Choose the field set from the ad type. For ALL ads: id, delivery start/stop, creative bodies, link captions,
   descriptions and titles, snapshot URL, beneficiary/payer list, bylines, languages, page id/name, publisher platforms,
   target ages/gender/locations, EU total reach, and the age-country-gender reach breakdown. For political ads, add spend,
   impressions, currency, demographic/regional distribution and estimated audience size.
2. Send the first request with the full parameter set. Follow `paging.next` iteratively (never recursively). The next URL
   already contains every parameter, so send no extra parameters after page 1.
3. Try each page up to 3 times. After each exception, wait 2^attempt seconds (1, 2, 4). If all attempts fail, stop and
   record the page number and URL, so a later run can resume from that cursor.
4. Stop cleanly on an error body (log the API message), a response without a data list, or an empty data list.
5. Save each raw page as a separate JSON file (page number, prefixed by the page-id batch when searching by pages).
6. Flatten the pages into one table. Expand the reach breakdown into per-age and per-gender columns for the target country.
   Scrub the token from every stored URL.
7. Media step (only for selected ads): open the snapshot URL in a headless browser with the UI language fixed and accept the
   cookie dialog. Wait for the creative. Try an ordered list of known locations for an image or video element. If none
   match, fall back to the largest visible image/video of at least 100x100 px. Download the media under the ad id.
8. If no media is found, search the page text for a short list of "this ad was removed / did not follow advertising
   standards" phrases. If one matches, status = removed_policy_violation. Otherwise status = no_media (text-only ad).
9. Pause about 1.5 s between snapshot visits. Optionally extract video frames at a fixed interval or count.
10. Deduplicate downloaded images by exact file hash and keep a map from duplicate to first-seen.

## Parameters and thresholds
- 3 retries per page with 1/2/4 s backoff. Snapshot pacing about 1.5 s. Media fallback minimum 100 px per side.
- API version configurable (the source pins v25.0 at the time of reading).

## Outputs
- Raw page JSON files, a flattened table, a media folder, a per-ad media status
  (image | video | no_media | removed_policy_violation | error), and a log with page counts and errors.
- HYDRA mapping: removed_policy_violation becomes a lifecycle event (`hydra/lifecycle.py`) and an observed
  platform-moderation fact on the incident. It is never a linking gene on its own.

## Edge cases
- Expired or invalid token: surface the error. A partial table must not be marked complete.
- Pages with `next` but no data: stop.
- Snapshot layouts change often. The structure-agnostic size fallback is the stable part.
- Removal phrases depend on the language. Fix one UI language or keep a per-language phrase list (PL and EN).
- Carousel ads have several media: keep them all with an index.

## What NOT to replicate
- GUI/Excel parameter dialogs, LDA/BLIP analysis, positional XPath lists (brittle), storing tokens or the
  long-lived-token exchange secret. Do not crawl snapshot pages in bulk: fetch only ads already flagged by HYDRA rules.

## Suggested acceptance tests
- Mocked API with 3 pages, where page 2 fails twice and then succeeds: all ads are collected once, and sleeps of 1 s and 2 s are observed.
- Mocked API where page 2 fails 3 times: run status partial and resume cursor = page 2 URL. The next run resumes there.
- Snapshot fixture with the removal banner gives removed_policy_violation. A text-only fixture gives no_media.
- No stored file contains the token string.

## Similarity-check reminder
Implement from this spec only. Do not open the GPL source while coding. Use your own identifiers and structure, and
run a similarity check against the studied files before merging.

## Implementation record
- 2026-11-21, ENRICH-PLUS builder sub-agent : `hydra/enrich/adlibrary.py` (`page_through`, `snapshot_status`, `body_key`), `hydra/scheduler.py` (`job_adlibrary_pull`), tests `tests/genes_inf/test_adlib_hardened.py`. Built from this spec and the public Meta Ad Library API documentation only; the source was not opened. Not implemented: headless snapshot media download and video frames (out of scope: no bulk snapshot crawling); removal status is read from snapshot pages an analyst saved.
- Similarity check: pending (to be run by a research agent; the builder may not open the source).
