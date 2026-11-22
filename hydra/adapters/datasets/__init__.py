"""External datasets → HYDRA incidents, kept apart from organiser data by the corpus flag (docs/datasets/EXTERNAL.md).

Mentor rule (docs/NASK_TASK.md, 2026-11-21 ~16:00): larger external datasets may train/calibrate the system and
enlarge the corpus if they stay clearly separated from organiser data and their origin and licence are stated.
Every incident made here has `Incident.source = "ext:<dataset>"` (corpus `ext:<dataset>` in `hydra.corpus`),
`Incident.label = "ext:<dataset>:<label>"` and `meta.group_label = "ext:<dataset>:<group>"`.

API
- `list_datasets()` → `[DatasetInfo]` (name, licence, verdict, modality, label kind)
- `iter_incidents(name, root, limit=None, **select)` → `(files, meta)` pairs: `files` maps a file name to
  bytes or a source path; `meta` carries id, source, label, group_label, roles and dataset fields.
  `select`: `min_group`, `per_group_cap`, `unlabelled` (see `_common.select`) plus adapter filters
  (`lang`, `split`, `phish_only` for phreshphish, `scam_only` for purino-scam-ads, `tld` for web-legit-tranco).
- `materialise(mix, root, out)` writes incident folders (`<out>/<dataset>__<key>/`) that
  `hydra.adapters.nask_folder` imports unchanged, plus `<out>/hydra_corpus.json` (per-folder source, labels,
  role overrides) and `<out>/CORPUS.md`.
- `import_corpus(out, settings)` imports such a folder with the corpus flag and labels (never as nask-sample).

CLI: `python -m hydra.adapters.datasets {list,materialise,import}`; then `hydra analyse --corpus ext:<name>`.
Do NOT run `hydra analyse <corpus-folder>` on a materialised folder: path imports are tagged as organiser data.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

from hydra.adapters.datasets import (
    certpl_tollfraud_2026,
    certpl_warnings,
    csirt_knf,
    kad_pl,
    mipd,
    phish_screens,
    phishdecloaker_kits,
    phishing_alive_7days,
    phishing_websites_80k,
    phishplens,
    phishtank,
    phiusiil,
    phreshphish,
    phreshphish_2k,
    polfake,
    political_ads_pl,
    purino_scam_ads,
    putra_legit,
    putra_phishing,
    scamworld,
    smishing_imc25,
    web_legit,
    xfact_pl,
)
from hydra.adapters.datasets._common import DatasetInfo, ExtIncident, Row, select

__all__ = ["DatasetInfo", "ExtIncident", "list_datasets", "iter_incidents", "iter_ext", "materialise",
           "import_corpus", "CORPUS_FILE"]

_REGISTRY: dict[str, ModuleType] = {m.INFO.name: m for m in (phreshphish, phreshphish_2k, purino_scam_ads,
                                                              phish_screens, putra_phishing, certpl_warnings,
                                                              phishdecloaker_kits,
    phishplens,
                                                              phishtank, phiusiil, smishing_imc25,
                                                              kad_pl, phishplens, scamworld, xfact_pl,
                                                              certpl_tollfraud_2026, putra_legit,
                                                              phishing_websites_80k, phishing_alive_7days,
                                                              polfake, mipd, political_ads_pl, csirt_knf, web_legit)}
SELECT_KEYS = {"limit", "min_group", "per_group_cap", "unlabelled"}
CORPUS_FILE = "hydra_corpus.json"


def list_datasets() -> list[DatasetInfo]:
    return [m.INFO for m in _REGISTRY.values()]


def _module(name: str) -> ModuleType:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown external dataset {name!r}; known: {sorted(_REGISTRY)}") from None


def dataset_root(name: str, root: Path) -> Path:
    """`root` may be the dataset folder itself or the external-big folder holding `<dir>/`."""
    root = Path(root).expanduser()
    sub = root / _module(name).INFO.dir
    return sub if sub.is_dir() else root


def rows(name: str, root: Path, **opts: Any) -> list[Row]:
    """Index rows after the adapter filter and the stratified selection (no content loaded)."""
    m = _module(name)
    r = dataset_root(name, root)
    filt = {k: v for k, v in opts.items() if k not in SELECT_KEYS}
    where = m.where(**filt) if hasattr(m, "where") and filt else None
    if filt and not hasattr(m, "where"):
        raise TypeError(f"{name}: unsupported options {sorted(filt)}")
    sel = {k: v for k, v in opts.items() if k in SELECT_KEYS}
    return select(m.index(r), where=where, **sel)


def iter_ext(name: str, root: Path, limit: int | None = None, **opts: Any) -> Iterator[ExtIncident]:
    """Selected items as `ExtIncident`s, streamed in the dataset's file order (deterministic); empty items
    (no file content at all) are dropped."""
    m = _module(name)
    picked = rows(name, root, limit=limit, **opts)
    yield from (x for x in m.load(dataset_root(name, root), picked) if x.files)


def iter_incidents(name: str, root: Path, limit: int | None = None,
                   **opts: Any) -> Iterator[tuple[dict[str, bytes | Path], dict[str, Any]]]:
    for x in iter_ext(name, root, limit=limit, **opts):
        yield x.files, meta_of(x)


def meta_of(x: ExtIncident) -> dict[str, Any]:
    info = _module(x.dataset).INFO
    # adapter meta first, then the reserved fields: an adapter field named e.g. "source" can never replace the
    # corpus flag (source = ext:<dataset>) or the labels
    return {**{k: v for k, v in x.meta.items() if k != "label_kind"},
            "id": x.incident_id, "dataset": x.dataset, "key": x.key, "source": x.source, "label": x.label,
            "group_label": x.group_label, "roles": dict(x.roles), "licence": info.licence, "verdict": info.verdict,
            "label_kind": info.label_kind}


def folder_name(x: ExtIncident) -> str:
    return f"{x.dataset}__{x.incident_id.removeprefix(f'ext-{x.dataset}-')}"


def write_incident(x: ExtIncident, out: Path) -> Path:
    d = out / folder_name(x)
    d.mkdir(parents=True, exist_ok=True)
    for name, data in x.files.items():
        dst = d / name
        if d.resolve() not in dst.resolve().parents:
            raise ValueError(f"unsafe file name {name!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, Path):
            if not dst.exists() or dst.stat().st_size != data.stat().st_size:
                shutil.copyfile(data, dst)
        elif not dst.exists() or dst.read_bytes() != data:
            dst.write_bytes(data)
    return d


def materialise(mix: list[tuple[str, dict[str, Any]]], root: Path, out: Path, *, clean: bool = False) -> dict:
    """Write a mixed external corpus. `mix` = [(dataset, {limit, min_group, per_group_cap, unlabelled, ...})].
    Idempotent: same inputs → same folders and bytes. `clean` removes folders not in this mix."""
    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict[str, Any]] = {}
    for name, opts in mix:
        for x in iter_ext(name, root, **opts):
            d = write_incident(x, out)
            m = meta_of(x)
            entries[d.name] = {k: m[k] for k in ("id", "dataset", "source", "label", "group_label", "roles",
                                                 "label_kind", "verdict")} | {"files": sorted(x.files)}
    if clean:
        for d in out.iterdir():
            if d.is_dir() and d.name not in entries and "__" in d.name:
                shutil.rmtree(d)
    stats = corpus_stats(entries)
    doc = {"version": 1, "note": "External corpus (not organiser data). Import with "
           "`python -m hydra.adapters.datasets import <this folder>`; never `hydra analyse <folder>`.",
           "mix": [[n, o] for n, o in mix], "stats": stats, "incidents": dict(sorted(entries.items()))}
    (out / CORPUS_FILE).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    (out / "CORPUS.md").write_text(corpus_md(doc))
    return doc


def corpus_stats(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    per: dict[str, Any] = {}
    for e in entries.values():
        s = per.setdefault(e["dataset"], {"incidents": 0, "labelled": 0, "labels": Counter(), "groups": Counter(),
                                          "verdict": e["verdict"], "label_kind": e["label_kind"]})
        s["incidents"] += 1
        s["groups"][e["group_label"]] += 1
        if e["label"]:
            s["labelled"] += 1
            s["labels"][e["label"]] += 1
    for s in per.values():
        sizes = sorted(s["labels"].values(), reverse=True)
        s["label_groups"] = len(sizes)
        s["multi_member_groups"] = sum(1 for v in sizes if v >= 2)
        s["largest_group"] = sizes[0] if sizes else 0
        s["labels"] = len(s["labels"])
        s["groups"] = dict(s["groups"])
    return {"incidents": len(entries), "datasets": per}


def corpus_md(doc: dict) -> str:
    st = doc["stats"]
    lines = ["# External corpus (NOT organiser data)", "",
             "Origin and licences: `docs/datasets/EXTERNAL.md`, `SOURCE.md` of each dataset under external-big.",
             "Import: `python -m hydra.adapters.datasets import <this folder>` (source `ext:<dataset>`, labels in "
             "`incidents.label` / `meta.group_label`), then `hydra analyse --corpus ext:<dataset>[,...]`.",
             "Never `hydra analyse <this folder>`: path imports are tagged `nask-sample`.", "",
             f"Incidents: {st['incidents']}", "",
             "| dataset | verdict | incidents | labelled | label kind | label groups | groups >= 2 | largest "
             "| classes |",
             "|---|---|---|---|---|---|---|---|---|"]
    for n, s in st["datasets"].items():
        lines.append(f"| {n} | {s['verdict']} | {s['incidents']} | {s['labelled']} | {s['label_kind']} | "
                     f"{s['label_groups']} | {s['multi_member_groups']} | {s['largest_group']} | {s['groups']} |")
    lines += ["", "Mix: " + json.dumps(doc["mix"])]
    return "\n".join(lines) + "\n"


def import_corpus(out: Path, settings: Any = None, *, writer: Any = None, store: Any = None,
                  only: set[str] | None = None) -> list[Any]:
    """Import a materialised corpus with `source = ext:<dataset>`, `label`, role overrides and
    `meta.group_label` / `meta.ext`. Idempotent (same bytes → same artifacts, same ids)."""
    from hydra.adapters.nask_folder import import_incident
    from hydra.config import get_settings
    from hydra.contracts import ArtifactRole
    from hydra.db import Writer
    from hydra.storage import ContentStore

    out = Path(out).expanduser().resolve()
    doc = json.loads((out / CORPUS_FILE).read_text())
    settings = settings or get_settings()
    settings.ensure_dirs()
    own = writer is None
    writer = writer or Writer(settings.db_path)
    store = store or ContentStore(settings.cas_dir)
    reports = []
    try:
        for folder, e in doc["incidents"].items():
            if only and e["dataset"] not in only:
                continue
            roles = {k: ArtifactRole(v) for k, v in (e.get("roles") or {}).items()}
            rep = import_incident(out / folder, writer, store, settings, incident_id=e["id"], source=e["source"],
                                  label=e["label"], roles=roles or None, prefix="ext")
            ext = {"dataset": e["dataset"], "label_kind": e["label_kind"], "verdict": e["verdict"]}
            if e.get("lang"):
                ext["lang"] = e["lang"]  # mass corpora (scripts/materialise_mass.py) carry lang per incident
            writer.run(lambda conn, iid=e["id"], g=e["group_label"], x=ext: _set_meta(conn, iid, g, x))
            reports.append(rep)
    finally:
        if own:
            writer.close()
    return reports


def _set_meta(conn: Any, iid: str, group_label: str | None, ext: dict) -> None:
    row = conn.execute("SELECT meta FROM incidents WHERE id = ?", (iid,)).fetchone()
    meta = json.loads(row["meta"] or "{}") if row else {}
    meta["group_label"] = group_label
    meta["ext"] = ext
    from hydra import events

    conn.execute("UPDATE incidents SET meta = ? WHERE id = ?", (events.canonical_json(meta), iid))
