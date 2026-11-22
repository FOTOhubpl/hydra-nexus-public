"""JURY-READY reports around a dataset analyse run, outside the frozen scoring paths (eval/manifest.py SCORING_PATHS
lists hydra/pipeline.py, so nothing here lives there).

- `jury_analyse(paths, settings, **kw)`: `pipeline.analyse` under the adapter's dataset rules
  (`nask_folder.dataset_rules`: wrapper folders, ZIP of incident ZIPs, refused input archive recorded instead of
  raised) + `metrics.import_report` + `metrics.references`. Used by `hydra analyse` and the GUI/API corpus upload.
- `dataset_import_report`: every file or archive of the input that did not become an artifact, with a Polish status.
- `reference_status`: which optional reference sets (face gallery, logo templates, MISP warninglists, PSL, CERT
  Polska list) are present, with Polish notes when one is missing.
Read only; no gene, score or decision depends on anything here.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

from hydra import import_progress
from hydra.config import Settings
from hydra.redact import redact_text

IMPORT_REPORT_MAX_FILES = 500

SKIP_CODE_PL = {
    "ZIP_PATH_TRAVERSAL": "ścieżka wychodzi poza archiwum (path traversal)",
    "ZIP_SYMLINK": "dowiązanie symboliczne w archiwum",
    "FILE_TOO_LARGE": "plik przekracza limit rozmiaru",
    "FILE_EMPTY": "pusty plik (0 bajtów)",
    "HEIC_UNREADABLE": "obraz HEIC nieczytelny (brak dekodera: ffmpeg >= 7.1 lub sips)",
    "ZIP_BOMB_SUSPECTED": "archiwum odrzucone: podejrzenie bomby ZIP (zbyt wysoki stopień kompresji)",
    "ZIP_TOO_MANY_ENTRIES": "archiwum odrzucone: zbyt wiele plików",
    "ZIP_TOO_LARGE": "archiwum odrzucone: zbyt duże po rozpakowaniu",
    "ZIP_NO_SPACE": "archiwum odrzucone: za mało miejsca na dysku",
    "ZIP_SIZE_MISMATCH": "archiwum odrzucone: rozmiar wpisu niezgodny z nagłówkiem",
    "ZIP_UNREADABLE": "archiwum uszkodzone lub zaszyfrowane (hasło)",
    "ZIP_ENTRY_UNREADABLE": "wpis archiwum nieczytelny (zaszyfrowany hasłem lub uszkodzony)",
    "ZIP_ENTRY_ENCRYPTED": "wpis archiwum zaszyfrowany hasłem (nie jest rozpakowywany)",
    "ZIP_NAME_TOO_LONG": "nazwa wpisu w archiwum zbyt długa",
    "HIDDEN_NOT_DATA": "plik ukryty, który nie jest danymi",
    "SYMLINK_NOT_FOLLOWED": "dowiązanie symboliczne (nie jest śledzone)",
    "LOOSE_ROOT_FILE": ("luźny plik w katalogu głównym zbioru (nie należy do żadnego incydentu;"
                        " umieść go w folderze incydentu)"),
}


def _skip_code_pl(code: str) -> str:
    return SKIP_CODE_PL.get(code) or SKIP_CODE_PL.get(code.split(":")[0]) or code


def dataset_import_report(paths: list[Path], incident_ids: list[str], loose: list[str],
                          rejected: list[dict[str, str]], settings: Settings) -> dict[str, Any]:
    """Files skipped inside an incident (empty, too large, hidden, symlink, unreadable inner ZIP), entries dropped
    from an archive (traversal, symlink, encrypted), archives refused whole (bomb, too many entries) and loose files
    at the dataset root; plus notes for category folders imported as one incident."""
    from hydra.adapters.nask_folder import (
        _is_archive,
        _is_evidence_file,
        _is_page_unit,
        _subdirs,
        is_incident_folder,
        load_incident,
        zip_skipped,
    )
    from hydra.db import connect

    incidents = []
    with closing(connect(settings.db_path, readonly=True)) as conn:
        for iid in dict.fromkeys(incident_ids):
            inc = load_incident(conn, iid)
            if inc is not None:
                incidents.append(inc)
    files: list[dict[str, Any]] = []
    for inc in incidents:
        meta = inc.meta or {}
        for e in [*meta.get("import_errors", []), *meta.get("skipped", [])]:
            files.append({"incident_id": inc.id, "path": str(e.get("path", "")), "code": str(e.get("code", ""))})
    for p in paths:
        p = Path(p).expanduser().resolve()
        try:
            archives = [p] if p.is_file() else sorted(f for f in p.iterdir() if f.is_file() and _is_archive(f))
        except OSError:
            archives = []
        for a in archives:
            for e in zip_skipped(a, settings):
                files.append({"incident_id": None, "path": f"{a.name}!/{e.get('path', '')}",
                              "code": str(e.get("code", ""))})
    files += [{"incident_id": None, "path": r["path"], "code": r["code"]} for r in rejected]
    files += [{"incident_id": None, "path": n, "code": "LOOSE_ROOT_FILE"} for n in loose]
    by_code: dict[str, int] = {}
    for f in files:
        f["path"] = redact_text(f["path"])
        f["status_pl"] = _skip_code_pl(f["code"])
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1
    notes: list[str] = []
    for p in paths:  # category folders (dataset/kategoria/incydent_N) are imported as one incident each
        p = Path(p).expanduser().resolve()
        if not p.is_dir() or is_incident_folder(p):
            continue
        for d in sorted(_subdirs(p)):
            if is_incident_folder(d):
                continue
            units = [x for x in d.rglob("*") if x.is_dir() and len(x.relative_to(d).parts) <= 3
                     and not _is_page_unit(x) and any(_is_evidence_file(f) for f in x.iterdir())]
            if len(units) >= 2:
                notes.append(f"Folder „{redact_text(d.name)}” ma {len(units)} podfolderów z materiałem i został"
                             " zaimportowany jako jeden incydent. Jeśli to kategoria wielu incydentów, przeanalizuj"
                             " go osobno (hydra analyse <ten folder>).")
    stored = sum(len(i.artifacts) for i in incidents)
    summary = (f"Zaimportowano {len(incidents)} incydentów ({stored} plików)."
               + (f" Pominięto {len(files)} plików lub archiwów (szczegóły w liście)." if files else
                  " Żaden plik nie został pominięty."))
    return {"incidents": len(incidents), "files_stored": stored, "skipped_files": len(files),
            "rejected_archives": len(rejected), "by_code": dict(sorted(by_code.items())),
            "files": files[:IMPORT_REPORT_MAX_FILES], "truncated": len(files) > IMPORT_REPORT_MAX_FILES,
            "summary_pl": summary, "notes_pl": notes[:50]}


def reference_status(settings: Settings) -> dict[str, Any]:
    """Face gallery / logo templates are built from organiser material (scripts/build_vis_reference.py) and are
    NOT shipped: without them the gallery `person_named` match and ORB logo matching emit nothing (unknown, never a
    penalty); face vectors, names from alt text and the brand lexicon still work. External snapshots come from the
    offline bundle (scripts/reference_data.py); without them warninglist tier overrides are off."""
    from hydra.enrich import cert_pl, warninglists
    from hydra.reference_build import reference_dir

    rd = reference_dir(settings)
    faces = bool(rd and (rd / "faces" / "gallery.json").is_file())
    logos = bool(rd and (rd / "logos").is_dir() and any((rd / "logos").glob("*.png")))
    ext = settings.data_dir / "external" if settings.data_dir is not None else None
    try:
        misp = (warninglists.snapshot_dir() / "lists").is_dir()
    except Exception:  # noqa: BLE001 — status only
        misp = False
    try:
        cert = (cert_pl.snapshot_dir() / "domains.json").is_file()
    except Exception:  # noqa: BLE001
        cert = bool(ext and (ext / "cert" / "domains.json").is_file())
    psl = bool(ext and (ext / "psl" / "public_suffix_list.dat").is_file())
    notes: list[str] = []
    missing = [n for n, ok in (("galeria twarzy", faces), ("wzorce logo", logos)) if not ok]
    if missing:
        notes.append(f"Brak bazy referencyjnej ({', '.join(missing)}): dopasowanie twarzy do galerii osób nazwanych w"
                     " materiale i dopasowanie logo do wzorców są wyłączone (geny nieznane, nie kara). Wektory twarzy,"
                     " nazwiska z tekstu alternatywnego i słownik marek działają. Bazę buduje się lokalnie z"
                     " zaimportowanych materiałów: przycisk „Zbuduj galerię referencyjną z zaimportowanych materiałów”"
                     " (Pulpit, Ustawienia) albo `hydra reference build`, potem „Przelicz analizę” (README,"
                     " „Galeria referencyjna”).")
    missing_ext = [n for n, ok in (("listy ostrzeżeń MISP", misp), ("lista sufiksów publicznych PSL", psl),
                                   ("lista ostrzeżeń CERT Polska", cert)) if not ok]
    if missing_ext:
        notes.append(f"Brak danych referencyjnych offline ({', '.join(missing_ext)}): nadpisania poziomów z list"
                     " ostrzeżeń są wyłączone, działają listy wbudowane. Instalacja paczki:"
                     " scripts/reference_data.py install hydra-reference-data-<data>.tar.zst"
                     " (docs/release/JURY_PACKAGE.md §0).")
    return {"face_gallery": faces, "logo_templates": logos, "misp_warninglists": misp, "psl": psl,
            "cert_pl": cert, "notes_pl": notes, "note_pl": " ".join(notes) or None}


def list_import(paths: list[Path], settings: Settings) -> None:
    """IMPORT-PROGRESS: the incident folders the dataset import will walk, with their file and byte counts, given to
    the job's `import_progress` sink so the import bar is determinate (`nask_folder._walk` is the walk that
    `import_incident` does; ZIPs are already unpacked content-addressed by the API's dataset check, so this re-uses
    them). Called before `jury_analyse` takes its REJECTED_ROOT_ARCHIVES mark, so refused archives this listing
    records are not reported twice. Best effort: any error leaves the total unknown (the UI shows an indeterminate
    bar)."""
    from hydra.adapters import nask_folder
    from hydra.adapters.datasets import CORPUS_FILE

    sink = import_progress.current()
    if sink is None:
        return
    folders: dict[Path, tuple[int, int]] = {}
    incidents = 0
    try:
        with nask_folder.dataset_rules():
            for p in paths:
                p = Path(p).expanduser().resolve()
                if p.is_dir() and (p / CORPUS_FILE).is_file():
                    return  # a materialised external corpus has its own importer: total stays unknown
                for folder in nask_folder.discover_incidents(p, settings):
                    incidents += 1
                    if folder.is_dir():
                        files = nask_folder._walk(folder)
                        folders[folder] = (len(files), sum(f.stat().st_size for f in files))
    except Exception:  # noqa: BLE001 — progress only; the import itself reports every problem
        return
    sink.expect(folders, incidents=incidents)


def jury_analyse(paths: list[Path], settings: Settings, *, entry: Any = None, **kw: Any):  # noqa: ANN201
    """`entry` (default `pipeline.analyse`; the API passes `pipeline.analyse_corpus`) under the dataset rules, with
    the two reports added to `metrics` of the returned RunResult (in memory and in the caller's output; the stored
    run row is the pipeline's own)."""
    from hydra import pipeline
    from hydra.adapters import nask_folder

    if import_progress.current() is not None:
        list_import(paths, settings)
    seen = len(nask_folder.REJECTED_ROOT_ARCHIVES)
    with nask_folder.dataset_rules():
        res = (entry or pipeline.analyse)(paths, settings, **kw)
    metrics = getattr(res, "metrics", None)
    if not isinstance(metrics, dict):
        return res
    rejected = nask_folder.REJECTED_ROOT_ARCHIVES[seen:]
    loose = list(metrics.get("loose_root_files") or [])
    for p in paths:  # a dataset ZIP: its loose root files, like those of a dataset folder (content-addressed unpack)
        p = Path(p).expanduser().resolve()
        try:
            if p.is_file() and nask_folder._is_archive(p) and not any(r["path"] == p.name for r in rejected):
                loose += [f"{p.name}/{n}" for n in nask_folder.loose_root_files(nask_folder.unpack_zip(p, settings))]
        except Exception:  # noqa: BLE001 — report only
            pass
    metrics["import_report"] = dataset_import_report(paths, list(res.incidents), loose, rejected, settings)
    metrics["references"] = reference_status(settings)
    return res
