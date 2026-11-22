# HYDRA Watch: clean-room similarity check

Date: 2026-11-21 (Europe/Warsaw). Builder stream WATCH .

## Inputs
- Our files: `hydra/watch/*.py` (11 files incl. `__init__`), `hydra/api_watch.py`.
- Reference repositories, cloned with `--depth 1` into `/tmp/simcheck/repos` only (never opened or read by the
  builder; the script prints aggregate numbers only):

| Repository | Commit | Licence | Python files |
|---|---|---|---|
| x0rz/phishing_catcher | 089208e3b8f052d494767c066866e80869add0cf | GPL-3.0 | 47 in total |
| atenreiro/opensquat | 8476845019a3888d66a159f5abb5c4b23d6f9df4 | GPL-3.0 | (all three) |
| wesleyraptor/streamingphish | 036cd484addecee073459807472efc769aa2930f | Apache-2.0 | |

- Specs used for the implementation: `mvp-lab/research/cleanroom/{phishing_catcher,opensquat,streamingphish}.md`
  and `mvp-lab/research/REALTIME_DISCOVERY.md` (research side).

## Method
`tests/watch/tools/simcheck.py` (written for this check, run with `python3 -I`): Python `tokenize` tokens without
comments/whitespace. Per (our file, reference file) pair: (a) longest common contiguous run of raw tokens,
(b) the same after normalisation (identifiers → ID, strings → STR, numbers → NUM), (c) number of shared raw
12-token shingles. Maximum over all 47 reference files.

## Result (final, 13 files incl. fingerprints.py and ct_recorder.py)

| Our file | Tokens | Longest raw run | Longest normalised run | Shared raw 12-grams |
|---|---|---|---|---|
| hydra/watch/ct_recorder.py | 815 | 7 | 18 | 0 |
| hydra/watch/engine.py | 4619 | 9 | 21 | 0 |
| hydra/watch/enrich.py | 1107 | 12 | 19 | 1 |
| hydra/watch/events.py | 887 | 7 | 23 | 0 |
| hydra/watch/families.py | 1771 | 11 | 19 | 0 |
| hydra/watch/fingerprints.py | 2374 | 8 | 39 | 0 |
| hydra/watch/lexicon.py | 1242 | 8 | 27 | 0 |
| hydra/watch/link.py | 2399 | 11 | 27 | 0 |
| hydra/watch/names.py | 1436 | 8 | 21 | 0 |
| hydra/watch/recorder.py | 1163 | 9 | 22 | 0 |
| hydra/watch/score.py | 2738 | 8 | 27 | 0 |
| hydra/api_watch.py | 1029 | 6 | 19 | 0 |

The normalised run of 39 in fingerprints.py is the clause table (a dict literal of `"str": ("str", NUM, "str")`
tuples): identical *shape* only, no shared identifiers or literals (raw run 8).

The one shared 12-gram (enrich.py line 32) is the generic idiom `with self._lock: now = time.monotonic`.
Raw runs of 6–12 tokens are standard Python boilerplate (imports, `if __name__ == "__main__":`, dict literals).
Normalised runs of 19–27 tokens are generic statement shapes (e.g. sequences of keyword-argument calls); they
carry no identifiers or literals.

**Conclusion: no long identical token sequences; no code, lists or identifiers copied.** The keyword/brand/TLD
lists are our own Polish lists (`hydra/watch/lexicon.py`), not the reference tools' lists.

Re-run before a merge:
`python3 -I tests/watch/tools/simcheck.py hydra/watch hydra/api_watch.py -- /tmp/simcheck/repos/*`
