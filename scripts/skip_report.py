"""Release report of every skip / xfail declared in the test suite (S00, release profile).

    uv run python scripts/skip_report.py                       # -> docs/release/SKIP_XFAIL.md
    uv run python scripts/skip_report.py --out /tmp/x.md       # elsewhere
    uv run python scripts/skip_report.py --junit /tmp/log/*.xml # add the outcomes of a real run (junit XML)
    uv run python scripts/skip_report.py --check               # exit 1 when a known-defect marker has no CR id

Static part: an AST scan of tests/**/*.py (pytest.mark.skip/skipif/xfail, pytestmark, pytest.skip(),
pytest.importorskip(), pytest.xfail()) and a regex scan of ui/e2e/*.spec.ts (test.skip/fixme/fail). Each entry gets
a category, the CR ids named in its reason, and an owner: the `owner:` / `Owner:` text of that CR in
docs/CONTRACT_REQUESTS.md when present, else the stream that owns the test directory.
Runtime part (--junit): which tests actually skipped / xfailed / xpassed on this host, with their reasons.
The scan only reads the repo; it imports nothing from tests/ or hydra/.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
CR_RE = re.compile(r"\bCR-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d+\b|\bCR-[A-Z]+-[A-Z]+\d+\b")

# Stream owning a test directory (docs/PLAN_MAX.md streams, docs/CONTRACT_REQUESTS.md requesters).
DIR_OWNER = {
    "adapters": "ADAPT", "adversarial": "ASTRA red-team (tests) / module owner (fix)", "core": "CORE",
    "datasets": "EVAL", "e2e": "E2E-FIX", "eval": "EVAL", "genes_dfk": "DFK", "genes_inf": "INF",
    "genes_pl": "PL-SCAM", "genes_prov": "PROV", "genes_txt": "TXT", "genes_vis": "VIS", "genome": "CORE",
    "hunt": "HUNT", "inputs": "INPUTS", "llm": "LLM", "perf": "PERF", "pipeline_audit": "ASTRA pipeline audit",
    "public": "ENRICH", "security": "SECURITY", "ui_api": "UI", "watch": "WATCH", "extract_accuracy": "RELEASE",
    "": "architect", "ui/e2e": "UI",
}


@dataclass
class Entry:
    path: str
    line: int
    kind: str  # skip | skipif | xfail | importorskip | runtime-skip | runtime-xfail | test.skip | test.fail | ...
    scope: str  # test name, class, or "module"
    condition: str
    reason: str
    crs: list[str] = field(default_factory=list)
    category: str = ""
    owner: str = ""


def _src(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v.value) + "}"
                       for v in node.values)
    return ast.unparse(node)


def _pytest_attr(func: ast.AST) -> str | None:
    """'skip' for pytest.mark.skip / pytest.skip, 'xfail' for pytest.mark.xfail, ... else None."""
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    parts.reverse()
    if not parts or parts[0] != "pytest":
        return None
    if len(parts) == 3 and parts[1] == "mark" and parts[2] in {"skip", "skipif", "xfail"}:
        return parts[2]
    if len(parts) == 2 and parts[1] in {"skip", "importorskip", "xfail"}:
        return {"skip": "runtime-skip", "xfail": "runtime-xfail", "importorskip": "importorskip"}[parts[1]]
    return None


def _marker_expr(node: ast.AST) -> tuple[str, ast.Call | None] | None:
    if isinstance(node, ast.Call):
        kind = _pytest_attr(node.func)
        return (kind, node) if kind else None
    kind = _pytest_attr(node)  # bare @pytest.mark.skip / xfail
    return (kind, None) if kind else None


def _entry_from_call(path: str, line: int, kind: str, call: ast.Call | None, scope: str) -> Entry:
    cond, reason = "", ""
    if call is not None:
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        args = list(call.args)
        if kind == "skipif":
            cond = _src(args[0]) if args else _src(kw.get("condition"))
            reason = _src(kw.get("reason"))
        elif kind == "xfail":
            cond = _src(args[0]) if args and not (isinstance(args[0], ast.Constant)
                                                   and isinstance(args[0].value, str)) else ""
            reason = _src(kw.get("reason")) or (_src(args[0]) if args and not cond else "")
            if "strict" in kw:
                cond = (cond + " " if cond else "") + f"strict={_src(kw['strict'])}"
            if "raises" in kw:
                cond = (cond + " " if cond else "") + f"raises={_src(kw['raises'])}"
        elif kind == "importorskip":
            cond = "import " + (_src(args[0]) if args else _src(kw.get("modname")))
            reason = _src(kw.get("reason")) or f"{cond} unavailable"
        else:  # skip, runtime-skip, runtime-xfail
            reason = _src(args[0]) if args else _src(kw.get("reason"))
    return Entry(path, line, kind, scope, cond, reason)


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, aliases: dict[str, tuple[str, ast.Call | None]]):
        self.path, self.aliases, self.entries, self.stack = path, aliases, [], []
        self.alias_uses: dict[str, list[str]] = {}

    def _scope(self) -> str:
        return ".".join(self.stack) or "module"

    def _decorators(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        name = ".".join([*self.stack, node.name])
        for dec in node.decorator_list:
            m = _marker_expr(dec)
            if m:
                self.entries.append(_entry_from_call(self.path, dec.lineno, m[0], m[1], name))
            elif isinstance(dec, ast.Name) and dec.id in self.aliases:
                self.alias_uses.setdefault(dec.id, []).append(name)

    def visit_FunctionDef(self, node):  # noqa: N802
        self._decorators(node)
        self.stack.append(node.name)
        for child in [*node.args.defaults, *node.body]:  # not the decorators: _decorators reported them
            self.visit(child)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_ClassDef(self, node):  # noqa: N802
        self._decorators(node)
        self.stack.append(node.name)
        for child in node.body:
            self.visit(child)
        self.stack.pop()

    def visit_Assign(self, node):  # noqa: N802
        # pytestmark = pytest.mark.skipif(...)  (module or class level); aliases are handled in scan_py
        if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            values = node.value.elts if isinstance(node.value, ast.List | ast.Tuple) else [node.value]
            for v in values:
                m = _marker_expr(v)
                if m:
                    self.entries.append(_entry_from_call(self.path, node.lineno, m[0], m[1],
                                                         self._scope() + " (pytestmark)"))
            return
        if isinstance(node.value, ast.Call) and not self.stack and any(
                isinstance(t, ast.Name) and t.id in self.aliases for t in node.targets):
            return  # alias definition, reported with its uses
        self.generic_visit(node)

    def visit_Call(self, node):  # noqa: N802
        kind = _pytest_attr(node.func)
        if kind in {"runtime-skip", "runtime-xfail", "importorskip"}:
            self.entries.append(_entry_from_call(self.path, node.lineno, kind, node, self._scope()))
        elif kind in {"skip", "skipif", "xfail"} and self.stack:
            # marker built inside a function (e.g. collection hooks, pytest.param(marks=...))
            self.entries.append(_entry_from_call(self.path, node.lineno, kind, node, self._scope()))
        self.generic_visit(node)


def scan_py(path: Path) -> list[Entry]:
    rel = path.relative_to(REPO).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    aliases: dict[str, tuple[str, ast.Call | None]] = {}
    alias_line: dict[str, int] = {}
    for node in tree.body:  # module-level `needs_ffmpeg = pytest.mark.skipif(...)`
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            m = _marker_expr(node.value)
            if m and node.targets[0].id != "pytestmark":
                aliases[node.targets[0].id] = m
                alias_line[node.targets[0].id] = node.lineno
    sc = _Scanner(rel, aliases)
    sc.visit(tree)
    for name, (kind, call) in aliases.items():
        uses = sc.alias_uses.get(name, [])
        e = _entry_from_call(rel, alias_line[name], kind, call, f"{name} -> " + (", ".join(uses) or "(unused)"))
        sc.entries.append(e)
    return sc.entries


TS_RE = re.compile(r"\btest\.(skip|fixme|fail)\s*\((.*)")


def scan_ts(path: Path) -> list[Entry]:
    rel = path.relative_to(REPO).as_posix()
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith(("*", "//")):
            continue
        m = TS_RE.search(line)
        if m:
            args = m.group(2).rstrip(");").strip()
            strings = re.findall(r"`([^`]*)`|\"([^\"]*)\"|'([^']*)'", args)
            reason = " ".join(next(s for s in g if s) for g in strings if any(g)) or args
            cond = args.split(",")[0] if "," in args else ""
            out.append(Entry(rel, i, "test." + m.group(1), "playwright", cond, reason))
    return out


def load_cr_owners() -> dict[str, str]:
    """CR id -> owner text from docs/CONTRACT_REQUESTS.md headings or Owner: lines in the section."""
    path = REPO / "docs" / "CONTRACT_REQUESTS.md"
    if not path.is_file():
        return {}
    owners: dict[str, str] = {}
    current: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            current = CR_RE.findall(line)
            m = re.search(r"\(owner:?\s*([^)]+)\)", line, re.I)
            for cr in current:
                if m:
                    owners[cr] = m.group(1).strip().strip("`")
            continue
        m = re.match(r"\s*-?\s*(?:Date/requester:.*?)?\**Owner\**:\s*(.+)", line)
        if m:
            text = re.split(r"\.\s+Status", m.group(1))[0].strip().rstrip(".")
            for cr in current:
                owners.setdefault(cr, text)
    return owners


def cr_status(cr: str) -> str:
    """'fixed' / 'done' / 'open' from the CR's section text (best effort)."""
    path = REPO / "docs" / "CONTRACT_REQUESTS.md"
    if not path.is_file():
        return "unknown"
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^#+ {re.escape(cr)}\b.*?(?=^#+ )", text, re.S | re.M)
    if not m:
        return "not in CONTRACT_REQUESTS.md"
    body = m.group(0).lower()
    if re.search(r"status[^\n]*\b(fixed|done|resolved|closed)\b", body):
        return "marked fixed/done (marker should be gone)"
    return "open"


ENV_HINTS = ("missing", "not fetched", "not installed", "not downloaded", "no ttf", "not seeded", "cannot create",
             "unavailable", "no heic", "no shipped prior", "weights")


def categorise(e: Entry) -> str:
    text = (e.reason + " " + e.condition).lower()
    if e.kind in {"xfail", "runtime-xfail", "test.fail", "test.fixme"}:
        return "known defect" if e.crs else "known defect (no CR id)"
    if "blocked" in text:
        return "blocked (external)"
    if "hydra_sample_test" in text or "organiser" in text:
        return "gated organiser data"
    if "-m e2e" in text or "e2e suite" in text:
        return "suite selection"
    if "projects" in text or "runs in" in text:
        return "suite selection"
    if "e2e_server_json" in text:
        return "environment (test server)"
    if any(h in text for h in ENV_HINTS) or e.kind == "importorskip":
        return "environment (tool/model)"
    return "data-dependent"


DEMO_IMPACT = {
    "known defect": "yes: documented defect on the tested path",
    "known defect (no CR id)": "unknown: marker lacks a CR id",
    "blocked (external)": "no: external artefact unavailable",
    "gated organiser data": "no: covered by the separate `sample` step",
    "suite selection": "no: runs in its own step",
    "environment (tool/model)": "only if the tool/model is absent on the demo host (`just doctor`)",
    "environment (test server)": "no",
    "data-dependent": "no",
}


def owner_for(e: Entry, cr_owners: dict[str, str]) -> str:
    for cr in e.crs:
        if cr in cr_owners:
            return cr_owners[cr]
    if e.category == "environment (tool/model)":
        return "P01 / JURY-DOCKER (environment)"
    p = e.path
    if p.startswith("ui/e2e"):
        return DIR_OWNER["ui/e2e"]
    parts = p.split("/")
    sub = parts[1] if len(parts) > 2 else ""
    return DIR_OWNER.get(sub, sub.upper() or "architect")


def collect() -> list[Entry]:
    entries: list[Entry] = []
    for path in sorted((REPO / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        entries += scan_py(path)
    ui = REPO / "ui" / "e2e"
    if ui.is_dir():
        for path in sorted(ui.glob("*.spec.ts")):
            entries += scan_ts(path)
    cr_owners = load_cr_owners()
    for e in entries:
        e.crs = sorted(set(CR_RE.findall(e.reason + " " + e.condition)))
        e.category = categorise(e)
        e.owner = owner_for(e, cr_owners)
    entries.sort(key=lambda e: (e.path, e.line))
    return entries


@dataclass
class Outcome:
    test: str
    outcome: str  # skipped | xfailed | xpassed
    reason: str


def parse_junit(paths: list[Path]) -> list[Outcome]:
    out: list[Outcome] = []
    for p in paths:
        try:
            root = ET.parse(p).getroot()
        except (ET.ParseError, OSError) as exc:
            print(f"warning: {p}: {exc}", file=sys.stderr)
            continue
        for tc in root.iter("testcase"):
            name = f"{tc.get('classname', '')}::{tc.get('name', '')}"
            sk = tc.find("skipped")
            if sk is not None:
                kind = "xfailed" if sk.get("type") == "pytest.xfail" else "skipped"
                out.append(Outcome(name, kind, (sk.get("message") or "").strip()))
            # strict xpass is reported as a failure; non-strict xpass has no element in junit (passed)
    return out


def _md(s: str, n: int = 160) -> str:
    s = re.sub(r"/(?:private/)?(?:var/folders|tmp)/\S*", "<tmp>", s)  # host temp paths are not facts
    s = re.sub(r"/(?:Users|home)/[^/\s]+", "~", s)
    s = " ".join(s.split()).replace("|", "\\|")
    return s if len(s) <= n else s[: n - 1] + "…"


def render(entries: list[Entry], outcomes: list[Outcome] | None, junit: list[Path]) -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True,
                             text=True, check=False).stdout.strip() or "unknown"
    except OSError:
        sha = "unknown"
    now = datetime.now(ZoneInfo("Europe/Warsaw")).strftime("%Y-%m-%d %H:%M %Z")
    cats = Counter(e.category for e in entries)
    lines = [
        "# Skip / xfail inventory (release report)",
        "",
        f"Generated by `uv run python scripts/skip_report.py` at commit `{sha}`, {now}. Do not edit by hand.",
        "Static scan of `tests/**/*.py` and `ui/e2e/*.spec.ts`; owner = owner of the named CR in",
        "`docs/CONTRACT_REQUESTS.md`, else the stream owning the test directory. A known-defect marker documents an",
        "open CR; it is not evidence of a fix (plan §0). The release profile (`scripts/e2e_all.sh --release`) treats",
        "any required step with xfailed/skipped-only results as PARTIAL.",
        "",
        "## Summary by category",
        "",
        "| Category | Declarations | Demo impact |",
        "|---|---:|---|",
    ]
    for cat, n in sorted(cats.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| {cat} | {n} | {DEMO_IMPACT.get(cat, '')} |")
    lines += ["", f"Total declarations: {len(entries)}.", ""]

    defects = [e for e in entries if e.category.startswith("known defect")]
    lines += ["## Known defects (xfail / test.fail)", ""]
    if defects:
        lines += ["| Location | Test | CR | CR status | Owner | Reason |", "|---|---|---|---|---|---|"]
        for e in defects:
            crs = ", ".join(e.crs) or "**none**"
            st = "; ".join(cr_status(c) for c in e.crs) or "-"
            lines.append(f"| `{e.path}:{e.line}` | `{_md(e.scope, 60)}` | {crs} | {st} | {_md(e.owner, 40)} | "
                         f"{_md(e.reason)} |")
    else:
        lines.append("None declared.")
    lines += ["", "## All declarations", "",
              "| Location | Kind | Scope | Condition | Reason | Category | CR | Owner |",
              "|---|---|---|---|---|---|---|---|"]
    for e in entries:
        lines.append(f"| `{e.path}:{e.line}` | {e.kind} | `{_md(e.scope, 70)}` | {_md(e.condition, 70) or '-'} | "
                     f"{_md(e.reason) or '-'} | {e.category} | {', '.join(e.crs) or '-'} | {_md(e.owner, 40)} |")
    if outcomes is not None:
        oc = Counter(o.outcome for o in outcomes)
        lines += ["", "## Runtime outcomes (junit of a real run)", "",
                  f"From junit XML: {', '.join('`' + p.name + '`' for p in junit)}. "
                  f"{oc.get('skipped', 0)} skipped, {oc.get('xfailed', 0)} xfailed.", ""]
        by_reason = Counter((o.outcome, _md(o.reason, 140)) for o in outcomes)
        lines += ["| Outcome | Count | Reason |", "|---|---:|---|"]
        for (kind, reason), n in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| {kind} | {n} | {reason or '-'} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "release" / "SKIP_XFAIL.md")
    ap.add_argument("--junit", type=Path, nargs="*", default=[], help="junit XML files from a real run")
    ap.add_argument("--check", action="store_true", help="exit 1 if a known-defect marker names no CR id")
    args = ap.parse_args(argv)
    entries = collect()
    junit = [p for p in args.junit if p.is_file()]
    outcomes = parse_junit(junit) if args.junit else None
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(entries, outcomes, junit), encoding="utf-8")
    bad = [e for e in entries if e.category == "known defect (no CR id)"]
    print(f"{len(entries)} skip/xfail declarations, {sum(e.category.startswith('known defect') for e in entries)} "
          f"known defects ({len(bad)} without CR id) -> {args.out}")
    return 1 if args.check and bad else 0


if __name__ == "__main__":
    sys.exit(main())
