"""Named kit-indicator rule engine (clean-room, from docs/cleanroom/iok_kit_rules.md).

A rule is a small YAML file: a `detection` map of named selections over page facets, plus one `condition`
string. Rules are OUR OWN, written for generic or synthetic patterns only. No IOK rule text, field names or
structure is copied, translated or bundled.

Facets built from the incident's web bundle: title (list), hostname (str), html (str), dom (str), js (list),
css (list), requests (list), cookies (list), headers (list). A missing facet is an empty value, never an
error. A rule that fails to parse or to compile a regex is logged once and skipped; it never raises into the
pipeline. At most the first 3 MB of any facet value is scanned.

Positive match -> gene `kit_rule` (value = rule id, group inf:kit:kit_rule, kit tier). Negative match ->
a `boilerplate`-tier `kit_rule` marker that names the genes it suppresses (the scorer applies the
suppression; `suppressed_kinds` exposes the set for callers that are not allowed to edit the scorer).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from hydra.genome.webparse import MAX_TEXT_SCAN

log = logging.getLogger(__name__)

RULES_DIR = Path(__file__).parent / "lexicons" / "kit_rules"
LIST_FACETS = {"title", "js", "css", "requests", "cookies", "headers"}
STRING_FACETS = {"hostname", "html", "dom"}
_OPS = {"contains", "startswith", "endswith", "re"}
_WS = re.compile(r"\s+")


# ------------------------------------------------------------------------------------------- rule model


@dataclass
class Selection:
    # each entry: (facet_name, operator or "" for equality, values, all_flag)
    exprs: list[tuple[str, str, list[str], bool]] = field(default_factory=list)


@dataclass
class Rule:
    id: str
    title: str
    selections: dict[str, Selection]
    condition: str
    polarity: str = "positive"      # or "negative"
    tags: list[str] = field(default_factory=list)
    suppresses: list[str] = field(default_factory=list)
    quality: float = 1.0
    case_insensitive: bool = False
    _compiled_re: dict[tuple[str, str], re.Pattern[str]] = field(default_factory=dict)


@dataclass
class Match:
    rule: Rule
    true_selections: list[str]
    facet: str
    offset: int
    snippet: str


# ------------------------------------------------------------------------------------------- facets


@dataclass
class Facets:
    title: list[str] = field(default_factory=list)
    hostname: str = ""
    html: str = ""
    dom: str = ""
    js: list[str] = field(default_factory=list)
    css: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    cookies: list[str] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)

    def get(self, name: str):
        return getattr(self, name, None)


def _collapse(s: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFC", s))[:MAX_TEXT_SCAN]


def build_facets(b) -> Facets:
    """Build the facet record from a webparse.WebBundle. Binary/empty elements are skipped."""
    f = Facets()
    titles: list[str] = []
    for d in b.htmls:
        if d.error:
            continue
        if d.title:
            titles.append(d.title)
        if not f.html and d.raw:
            f.html = _collapse(d.raw)
        f.js.extend(s for s in d.inline_scripts if s)
        f.requests.extend(s for s in d.script_src if s)
        f.requests.extend(s for s in d.stylesheets if s)
    for h in b.hars:
        titles.extend(t for t in h.page_titles if t)
        for r in h.requests:
            u = str(r.get("url", ""))
            if u:
                f.requests.append(u)
        for body in h.bodies:
            if body.url.endswith("#post"):
                continue
            try:
                txt = body.data.decode("utf-8", "strict")
            except UnicodeDecodeError:
                continue  # binary body
            if body.mime == "text/html" and not f.html:
                f.html = _collapse(txt)
            elif "javascript" in body.mime or body.url.endswith(".js"):
                f.js.append(txt[:MAX_TEXT_SCAN])
            elif body.mime == "text/css" or body.url.endswith(".css"):
                f.css.append(txt[:MAX_TEXT_SCAN])
        for _host, hi in h.hosts.items():
            f.cookies.extend(sorted(hi.cookies))
            for s in sorted(hi.server):
                f.headers.append(f"Server: {s}")
            for p in sorted(hi.powered_by):
                f.headers.append(f"X-Powered-By: {p}")
    for a, data in b.assets:
        low = a.meta.path_in_incident.lower().split(".pobrane")[0]
        try:
            txt = data.decode("utf-8", "strict")
        except UnicodeDecodeError:
            continue
        if low.endswith((".js", ".mjs")):
            f.js.append(txt[:MAX_TEXT_SCAN])
        elif low.endswith(".css"):
            f.css.append(txt[:MAX_TEXT_SCAN])
    for u, _, _ in b.landing_urls:
        f.requests.append(u)
        if not f.hostname:
            h = (urlsplit(u).hostname or "").lower()
            if h:
                f.hostname = h
    f.title = [t for t in dict.fromkeys(titles)]
    f.dom = f.html  # saved pages: rendered DOM not separately available
    f.headers = sorted(dict.fromkeys(f.headers))
    return f


# ------------------------------------------------------------------------------------------- loading


def _parse_selection(rule: Rule, name: str, raw) -> Selection:
    sel = Selection()
    if not isinstance(raw, dict):
        raise ValueError(f"selection {name!r} is not a mapping")
    for expr, values in raw.items():
        parts = str(expr).split("|")
        facet = parts[0].strip()
        if facet not in LIST_FACETS and facet not in STRING_FACETS:
            raise ValueError(f"unknown facet {facet!r}")
        mods = [p.strip() for p in parts[1:]]
        all_flag = "all" in mods
        ops = [m for m in mods if m in _OPS]
        if len(ops) > 1:
            raise ValueError(f"selection {name!r}: more than one operator")
        op = ops[0] if ops else ""
        vals = [str(v) for v in (values if isinstance(values, list) else [values])]
        if op == "re":
            for v in vals:
                rule._compiled_re[(name, v)] = re.compile(v)  # may raise re.error -> rule skipped
        sel.exprs.append((facet, op, vals, all_flag))
    return sel


def _load_rule(path: Path) -> Rule | None:
    import yaml

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — bad YAML: skip, log once
        log.warning("kit rule %s: unreadable (%s); skipped", path.name, exc)
        return None
    if not isinstance(doc, dict):
        log.warning("kit rule %s: not a mapping; skipped", path.name)
        return None
    try:
        rule = Rule(
            id=str(doc.get("id") or path.stem),
            title=str(doc.get("title") or path.stem),
            selections={},
            condition=str(doc["detection"]["condition"]),
            polarity=str(doc.get("polarity", "positive")).lower(),
            tags=[str(t) for t in (doc.get("tags") or [])],
            suppresses=[str(s) for s in (doc.get("suppresses") or [])],
            quality=max(0.3, min(1.0, float(doc.get("quality", 1.0)))),
            case_insensitive=bool(doc.get("case_insensitive", False)),
        )
        for name, raw in doc["detection"].items():
            if name == "condition":
                continue
            rule.selections[name] = _parse_selection(rule, name, raw)
        if not rule.selections:
            raise ValueError("no selections")
    except (KeyError, ValueError, TypeError, re.error) as exc:
        log.warning("kit rule %s: invalid (%s); skipped", path.name, exc)
        return None
    return rule


@lru_cache(maxsize=1)
def load_rules() -> tuple[Rule, ...]:
    if not RULES_DIR.is_dir():
        return ()
    out = []
    for p in sorted(RULES_DIR.glob("*.yml")) + sorted(RULES_DIR.glob("*.yaml")):
        r = _load_rule(p)
        if r is not None:
            out.append(r)
    return tuple(out)


# ------------------------------------------------------------------------------------------- matching


def _satisfied(text: str, op: str, value: str, ci: bool, rule: Rule, sel_name: str) -> bool:
    if ci:
        text, value = text.lower(), value.lower()
    if op == "":
        return text == value
    if op == "contains":
        return value in text
    if op == "startswith":
        return text.startswith(value)
    if op == "endswith":
        return text.endswith(value)
    if op == "re":
        rx = rule._compiled_re.get((sel_name, value))
        if rx is None:
            rx = re.compile(value, re.I) if ci else re.compile(value)
        return rx.search(text[:MAX_TEXT_SCAN]) is not None
    return False


def _expr_true(facets: Facets, facet: str, op: str, values: list[str], all_flag: bool,
               rule: Rule, sel_name: str) -> tuple[bool, int]:
    fval = facets.get(facet)
    items = fval if isinstance(fval, list) else [fval or ""]
    items = [i[:MAX_TEXT_SCAN] if isinstance(i, str) else "" for i in items]

    def one(value: str) -> bool:
        return any(_satisfied(it, op, value, rule.case_insensitive, rule, sel_name) for it in items)

    ok = all(one(v) for v in values) if all_flag else any(one(v) for v in values)
    off = -1
    if ok:
        for it in items:
            for v in values:
                t, vv = (it.lower(), v.lower()) if rule.case_insensitive else (it, v)
                pos = t.find(vv) if op in ("", "contains", "startswith", "endswith") else (
                    m.start() if (m := rule._compiled_re.get((sel_name, v), re.compile(v)).search(it)) else -1)
                if pos >= 0:
                    off = pos
                    break
            if off >= 0:
                break
    return ok, off


def _eval_selection(facets: Facets, rule: Rule, sel: Selection) -> tuple[bool, int, str]:
    off, facet = -1, ""
    for (fc, op, values, all_flag) in sel.exprs:
        ok, o = _expr_true(facets, fc, op, values, all_flag, rule, _sel_name(rule, sel))
        if not ok:
            return False, -1, ""  # exprs inside one selection are ANDed
        if off < 0:
            off, facet = (o if o >= 0 else 0), fc
    return True, max(off, 0), facet


def _sel_name(rule: Rule, sel: Selection) -> str:
    for n, s in rule.selections.items():
        if s is sel:
            return n
    return ""


# ---- condition parser (selection names, and/or/not, parentheses, N/all/any of prefix*) ----

_TOK = re.compile(r"\(|\)|\band\b|\bor\b|\bnot\b|\d+\s+of\s+\w+\*?|\ball\s+of\s+\w+\*?|\bany\s+of\s+\w+\*?|[\w.*]+")


def _eval_condition(cond: str, truth: dict[str, bool]) -> bool:
    toks = _TOK.findall(cond or "")
    pos = 0

    def quant(tok: str) -> bool:
        head, _, prefix = tok.partition(" of ")
        prefix = prefix.strip()
        star = prefix.endswith("*")
        base = prefix[:-1] if star else prefix
        names = [n for n in truth if (n.startswith(base) if star else n == base)]
        hits = sum(1 for n in names if truth.get(n))
        head = head.strip()
        if head == "all":
            return bool(names) and hits == len(names)
        if head == "any":
            return hits >= 1
        return hits >= int(head)

    def parse_or():
        nonlocal pos
        val = parse_and()
        while pos < len(toks) and toks[pos] == "or":
            pos += 1
            val = parse_and() or val
        return val

    def parse_and():
        nonlocal pos
        val = parse_not()
        while pos < len(toks) and toks[pos] == "and":
            pos += 1
            val = parse_not() and val
        return val

    def parse_not():
        nonlocal pos
        if pos < len(toks) and toks[pos] == "not":
            pos += 1
            return not parse_not()
        return parse_atom()

    def parse_atom():
        nonlocal pos
        if pos >= len(toks):
            return False
        tok = toks[pos]
        if tok == "(":
            pos += 1
            val = parse_or()
            if pos < len(toks) and toks[pos] == ")":
                pos += 1
            return val
        pos += 1
        if " of " in tok:
            return quant(tok)
        return bool(truth.get(tok, False))

    try:
        return parse_or()
    except (ValueError, IndexError):
        return False


def evaluate(facets: Facets, rules=None) -> list[Match]:
    out: list[Match] = []
    for rule in (rules if rules is not None else load_rules()):
        results: dict[str, bool] = {}
        info: dict[str, tuple[int, str]] = {}
        for name, sel in rule.selections.items():
            ok, off, facet = _eval_selection(facets, rule, sel)
            results[name] = ok
            if ok:
                info[name] = (off, facet)
        if not _eval_condition(rule.condition, results):
            continue
        true_sels = [n for n in rule.selections if results[n]]
        off, facet = (info[true_sels[0]] if true_sels else (0, ""))
        snippet = ""
        fval = facets.get(facet)
        s = fval[0] if isinstance(fval, list) and fval else (fval if isinstance(fval, str) else "")
        if s:
            snippet = s[off:off + 120]
        out.append(Match(rule, true_sels, facet, off, snippet))
    return out


# ------------------------------------------------------------------------------------------- gene output


def emit_rule_genes(sink, b) -> None:
    facets = build_facets(b)
    for m in evaluate(facets):
        sels = ", ".join(m.true_selections)
        note = f"{m.rule.title} [{sels}] @ {m.facet}:{m.offset}"[:300]
        if m.rule.polarity == "negative":
            sink.add("kit_rule", m.rule.id, "boilerplate", quality=0.0,
                     note=f"negatywna: {note}; suppresses={','.join(m.rule.suppresses)}")
        else:
            sink.add("kit_rule", m.rule.id, "inf:kit:kit_rule", quality=m.rule.quality, note=note)


def suppressed_kinds(genes) -> set[str]:
    """Gene kinds that negative kit-rule markers ask the scorer to drop for this incident."""
    out: set[str] = set()
    for g in genes:
        if g.kind == "kit_rule" and g.dependency_group and ":boilerplate:" in g.dependency_group:
            note = g.evidence.note or ""
            if "suppresses=" in note:
                out.update(x for x in note.split("suppresses=", 1)[1].split(",") if x)
    return out
