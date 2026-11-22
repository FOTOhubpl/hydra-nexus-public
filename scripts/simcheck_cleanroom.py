"""Clean-room similarity check: our files against reference repositories in any language (CLAUDE.md two-team rule).

Usage:  python3 -I scripts/simcheck_cleanroom.py OUR_FILE... -- REF_DIR...

The builder never reads the reference sources; this script prints aggregate numbers only (no reference text).
Tokens: identifiers, numbers, string literals and single punctuation characters, after removing `#`, `//` and
`/* */` comments and whitespace. Per our file, against the union of all reference files (text files <= 2 MB):
(a) longest common contiguous run of raw tokens, (b) the same after normalisation (identifiers -> ID, strings -> STR,
numbers -> NUM), (c) number of distinct raw 12-token shingles shared with any reference file, and (d) shared string
literals of 12+ characters (count only; regex literals copied verbatim would show here).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`[^`]*`|\S")
COMMENTS = re.compile(r"/\*.*?\*/|(?<![:\"'\\])//[^\n]*|^\s*#[^\n]*|(?<=\s)#\s[^\n]*", re.S | re.M)
TEXT_EXT = {".go", ".php", ".py", ".js", ".ts", ".yml", ".yaml", ".json", ".yar", ".txt", ".md", ".rb", ".java",
            ".c", ".h", ".rs", ".sh", ".toml", ".cfg", ".ini", ".html", ".css"}
K_SHINGLE = 12


def tokens(text: str) -> list[str]:
    return TOKEN.findall(COMMENTS.sub(" ", text))


def norm(tok: str) -> str:
    if tok[0] in "\"'`":
        return "STR"
    if tok[0].isdigit():
        return "NUM"
    if tok[0].isalpha() or tok[0] == "_":
        return "ID"
    return tok


def grams(seq: list[str], k: int) -> set[int]:
    return {hash(tuple(seq[i: i + k])) for i in range(len(seq) - k + 1)}


def longest_run(ours: list[str], refs: list[list[str]]) -> int:
    lo, hi = 0, len(ours)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        mine = grams(ours, mid)
        if any(mine & grams(r, mid) for r in refs if len(r) >= mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def main(argv: list[str]) -> int:
    if "--" not in argv:
        print(__doc__)
        return 2
    i = argv.index("--")
    ours = [Path(p) for p in argv[:i]]
    ref_files = [p for d in argv[i + 1:] for p in Path(d).rglob("*")
                 if p.is_file() and p.suffix.lower() in TEXT_EXT and p.stat().st_size <= 2_000_000
                 and "/.git/" not in str(p)]
    refs = [tokens(p.read_text("utf-8", "replace")) for p in ref_files]
    refs_n = [[norm(t) for t in r] for r in refs]
    ref_sh = set().union(*(grams(r, K_SHINGLE) for r in refs)) if refs else set()
    ref_str = {t for r in refs for t in r if t[0] in "\"'`" and len(t) >= 14}
    ref_str = {t[1:-1] for t in ref_str}
    print(f"reference files: {len(ref_files)}, tokens: {sum(map(len, refs))}")
    print("| Our file | Tokens | Longest raw run | Longest normalised run | Shared raw 12-grams"
          " | Shared 12+ char strings |")
    print("|---|---|---|---|---|---|")
    for p in ours:
        t = tokens(p.read_text("utf-8", "replace"))
        tn = [norm(x) for x in t]
        sh = len(grams(t, K_SHINGLE) & ref_sh)
        strs = len({x[1:-1] for x in t if x[0] in "\"'`" and len(x) >= 14} & ref_str)
        print(f"| {p} | {len(t)} | {longest_run(t, refs)} | {longest_run(tn, refs_n)} | {sh} | {strs} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
