"""Acceptance tests for the YAML kit-rule engine (hydra/genome/kit_rules.py). Synthetic rules and facets."""

from __future__ import annotations

import logging

from fixtures import _synthetic_warninglists, web_incident  # noqa: F401

from hydra.genome import base, kit_rules
from hydra.genome.kit_exfil import inf_kit
from hydra.genome.kit_rules import Facets, _load_rule, evaluate
from hydra.genome.webparse import _BUNDLES


def _rule(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return _load_rule(p)


# 1. html|contains|all of two hidden-input names fires only when both are present.
def test_contains_all_needs_every_value(tmp_path):
    r = _rule(tmp_path, "r.yml", """
id: two_hidden
title: Two hidden fields
detection:
  fields:
    html|contains|all:
      - 'name="a_field"'
      - 'name="b_field"'
  condition: fields
""")
    both = Facets(html='<input name="a_field"><input name="b_field">')
    one = Facets(html='<input name="a_field">')
    assert [m.rule.id for m in evaluate(both, [r])] == ["two_hidden"]
    assert evaluate(one, [r]) == []


# 2. A list facet: requests|endswith fires when any request URL ends with the token.
def test_list_facet_endswith_any_element(tmp_path):
    r = _rule(tmp_path, "r.yml", """
id: endpoint
title: Exfil endpoint
detection:
  ep:
    requests|endswith: /gate.php
  condition: ep
""")
    f = Facets(requests=["https://x.example/a.js", "https://x.example/gate.php"])
    assert len(evaluate(f, [r])) == 1
    assert evaluate(Facets(requests=["https://x.example/a.js"]), [r]) == []


# 3. condition `a and not b`.
def test_condition_and_not(tmp_path):
    r = _rule(tmp_path, "r.yml", """
id: a_not_b
title: A and not B
detection:
  a:
    html|contains: anchor_a
  b:
    html|contains: anchor_b
  condition: a and not b
""")
    assert len(evaluate(Facets(html="has anchor_a only"), [r])) == 1
    assert evaluate(Facets(html="has anchor_a and anchor_b"), [r]) == []
    assert evaluate(Facets(html="nothing"), [r]) == []


# 4. A negative rule marks a parked template and names the title gene it suppresses.
def test_negative_rule_suppresses_title(tmp_path):
    r = _rule(tmp_path, "r.yml", """
id: parked
title: Parked template
polarity: negative
suppresses:
  - page_title
detection:
  t:
    title|contains: parked domain
  condition: t
""")
    m = evaluate(Facets(title=["this is a parked domain page"]), [r])
    assert len(m) == 1 and m[0].rule.polarity == "negative"
    assert m[0].rule.suppresses == ["page_title"]

    # the boilerplate marker a negative rule produces is read back by suppressed_kinds
    genes = _fake_genes(kind="kit_rule", group="inf:boilerplate:kit_rule",
                        note="negatywna: Parked [t] @ title:0; suppresses=page_title,dom_skeleton_hash")
    assert kit_rules.suppressed_kinds(genes) == {"page_title", "dom_skeleton_hash"}


# 5. A bad regex in one rule leaves the others working and logs exactly one warning.
def test_bad_regex_skipped_with_one_warning(tmp_path, monkeypatch, caplog):
    good = tmp_path / "good.yml"
    good.write_text("""
id: good
title: Good rule
detection:
  g:
    html|contains: marker
  condition: g
""", encoding="utf-8")
    bad = tmp_path / "bad.yml"
    bad.write_text("""
id: bad
title: Bad regex
detection:
  b:
    html|re: '([unclosed'
  condition: b
""", encoding="utf-8")
    monkeypatch.setattr(kit_rules, "RULES_DIR", tmp_path)
    kit_rules.load_rules.cache_clear()
    with caplog.at_level(logging.WARNING, logger="hydra.genome.kit_rules"):
        rules = kit_rules.load_rules()
    kit_rules.load_rules.cache_clear()
    ids = {r.id for r in rules}
    assert ids == {"good"}
    warnings = [rec for rec in caplog.records if "bad.yml" in rec.getMessage()]
    assert len(warnings) == 1


# 6. The explanation contains the rule title and the selection names.
def test_explanation_has_title_and_selection_names(tmp_path):
    r = _rule(tmp_path, "r.yml", """
id: expl
title: Explainable kit
detection:
  anchor_one:
    html|contains: kitmark
  anchor_two:
    js|contains: exfilcall
  condition: anchor_one and anchor_two
""")

    class _Sink:
        def __init__(self):
            self.calls = []

        def add(self, kind, value, group, **kw):
            self.calls.append((kind, value, group, kw))

    sink = _Sink()
    facets = Facets(html="page kitmark here", js=["function x(){exfilcall()}"])
    for m in evaluate(facets, [r]):
        sels = ", ".join(m.true_selections)
        note = f"{m.rule.title} [{sels}] @ {m.facet}:{m.offset}"
        sink.add("kit_rule", m.rule.id, "inf:kit:kit_rule", note=note, quality=m.rule.quality)
    assert sink.calls
    note = sink.calls[0][3]["note"]
    assert "Explainable kit" in note
    assert "anchor_one" in note and "anchor_two" in note


# The shipped syn_lead_kit rule fires end-to-end through the inf_kit extractor on the web fixture.
def test_shipped_rule_fires_through_inf_kit(tmp_path, settings):
    _BUNDLES.clear()
    inc = web_incident(tmp_path)
    res = inf_kit(inc, base.ExtractContext(settings=settings, work_dir=tmp_path))
    rules = [g for g in res.genes if g.kind == "kit_rule"]
    assert ("syn_lead_kit", "inf:kit:kit_rule") in {(g.value, g.dependency_group) for g in rules}


# --- small helpers for the negative-rule test -----------------------------------------------------------


def _fake_genes(kind, group, note):
    class _Ev:
        def __init__(self, note):
            self.note = note

    class _G:
        def __init__(self, kind, group, note):
            self.kind = kind
            self.dependency_group = group
            self.evidence = _Ev(note)

    return [_G(kind, group, note)]
