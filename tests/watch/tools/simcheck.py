"""Token-sequence similarity check for the clean-room audit. Prints aggregate numbers only, never foreign code.

For every pair (our file, reference file): Python tokenizer tokens (comments/whitespace dropped),
(a) raw tokens and (b) normalised tokens (identifiers -> ID, strings -> STR, numbers -> NUM).
Reports the longest common contiguous token run and the count of shared 12-token shingles.

Usage: python3 -I tests/watch/tools/simcheck.py <our dirs/files...> -- <reference dirs...>
"""

from __future__ import annotations

import io
import keyword
import sys
import tokenize
from pathlib import Path

SKIP = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}


def toks(path: Path, norm: bool) -> list[str]:
    out: list[str] = []
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        for t in tokenize.generate_tokens(io.StringIO(src).readline):
            if t.type in SKIP:
                continue
            s = t.string
            if norm:
                if t.type == tokenize.NAME and not keyword.iskeyword(s):
                    s = "ID"
                elif t.type == tokenize.STRING:
                    s = "STR"
                elif t.type == tokenize.NUMBER:
                    s = "NUM"
            out.append(s)
    except (tokenize.TokenError, SyntaxError):
        pass
    return out


def longest_run(a: list[str], b: list[str]) -> int:
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def shingles(t: list[str], k: int = 12) -> set[tuple[str, ...]]:
    return {tuple(t[i:i + k]) for i in range(len(t) - k + 1)}


def files(args: list[str]) -> list[Path]:
    out: list[Path] = []
    for a in args:
        p = Path(a)
        out += sorted(p.rglob("*.py")) if p.is_dir() else [p]
    return out


def main(argv: list[str]) -> None:
    sep = argv.index("--")
    ours, refs = files(argv[:sep]), files(argv[sep + 1:])
    ref_t = {r: (toks(r, False), toks(r, True)) for r in refs}
    print(f"reference files: {len(refs)}; our files: {len(ours)}")
    print("our_file | tokens | longest_raw_run | longest_normalised_run | shared_raw_12grams")
    for o in ours:
        raw, norm = toks(o, False), toks(o, True)
        so = shingles(raw)
        best_raw = max((longest_run(raw, rr) for rr, _ in ref_t.values()), default=0)
        best_norm = max((longest_run(norm, rn) for _, rn in ref_t.values()), default=0)
        shared = sum(len(so & shingles(rr)) for rr, _ in ref_t.values())
        print(f"{o} | {len(raw)} | {best_raw} | {best_norm} | {shared}")


if __name__ == "__main__":
    main(sys.argv[1:])
