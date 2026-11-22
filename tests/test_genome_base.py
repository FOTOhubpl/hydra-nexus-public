from __future__ import annotations

from hydra.contracts import Family, Gene, Incident
from hydra.genome import base


def test_failing_extractor_yields_failed_gene_and_others_run(tmp_path, settings):
    saved = dict(base.REGISTRY)
    base.REGISTRY.clear()
    try:
        @base.register("boom", family=Family.VIS)
        def boom(inc, ctx):
            raise RuntimeError("bad image")

        @base.register("ok", family=Family.TXT)
        def ok(inc, ctx):
            return base.ExtractResult(genes=[Gene(incident_id=inc.incident.id, family=Family.TXT, kind="ok",
                                                  value="x", extractor="ok@1")])

        inc = base.ResolvedIncident(Incident(id="i1", source="t", source_ref="t"), [])
        res = base.run_all(inc, base.ExtractContext(settings=settings, work_dir=tmp_path))
        kinds = {(g.kind, g.status) for g in res.genes}
        assert ("boom", "failed") in kinds and ("ok", "observed") in kinds
    finally:
        base.REGISTRY.clear()
        base.REGISTRY.update(saved)


def test_slow_extractor_times_out_as_unavailable_and_others_run(tmp_path, settings, monkeypatch):
    """GAP-AUDIT B6: a hanging extractor (even one that swallows Exception) gives an `unavailable` timeout gene."""
    import time

    monkeypatch.setenv("HYDRA_EXTRACTOR_TIMEOUT_S", "0.3")
    saved = dict(base.REGISTRY)
    base.REGISTRY.clear()
    try:
        @base.register("hang", family=Family.VIS)
        def hang(inc, ctx):
            try:
                time.sleep(30)
            except Exception:  # noqa: BLE001 — extractors degrade on Exception; the limit is not one
                return base.ExtractResult()
            return base.ExtractResult()

        @base.register("ok", family=Family.TXT)
        def ok(inc, ctx):
            return base.ExtractResult(genes=[Gene(incident_id=inc.incident.id, family=Family.TXT, kind="ok",
                                                  value="x", extractor="ok@1")])

        inc = base.ResolvedIncident(Incident(id="i1", source="t", source_ref="t"), [])
        t0 = time.perf_counter()
        res = base.run_all(inc, base.ExtractContext(settings=settings, work_dir=tmp_path))
        assert time.perf_counter() - t0 < 5
        by = {g.kind: g for g in res.genes}
        assert by["hang"].status == "unavailable" and by["hang"].evidence.note.startswith("ExtractorTimeout:")
        assert by["ok"].status == "observed"
        import signal

        assert signal.getitimer(signal.ITIMER_REAL)[0] == 0  # timer cleared
        from hydra.pipeline import _RUNTIME_NOTE

        assert _RUNTIME_NOTE.match(by["hang"].evidence.note)  # retried by the gene cache on a later run
    finally:
        base.REGISTRY.clear()
        base.REGISTRY.update(saved)
