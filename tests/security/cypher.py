"""Independent lexer for the emitted Cypher subset; no database or execution of statements."""

from __future__ import annotations


def statements(text: str) -> list[str]:
    """Split statements outside strings/comments, replacing literal contents with a safe marker."""
    result, current = [], []
    i = 0
    while i < len(text):
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            assert end != -1, "Unclosed block comment"
            i = end + 2
            continue
        if text[i] in "'\"":
            quote = text[i]
            current.append("'LITERAL'")
            i += 1
            while i < len(text) and text[i] != quote:
                if text[i] == "\\":
                    i += 1
                i += 1
            assert i < len(text), "Unclosed Cypher string"
            i += 1
            continue
        if text[i] == ";":
            if (statement := "".join(current).strip()):
                result.append(statement)
            current = []
        else:
            current.append(text[i])
        i += 1
    assert not "".join(current).strip(), "Unterminated Cypher statement"
    return result
