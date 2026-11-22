"""Registry invariants: ids, enums, honest levels, data_out never contains forbidden fields."""

from __future__ import annotations

import importlib
import re

import pytest

from hydra.integrations import registry
from integrations.conftest import FIXTURES

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def test_ids_unique_and_slugs():
    ids = [c.id for c in registry.CONNECTORS]
    assert len(ids) == len(set(ids))
    assert all(SLUG.match(i) for i in ids)


@pytest.mark.parametrize("c", registry.CONNECTORS, ids=lambda c: c.id)
def test_enums_and_text(c):
    assert c.category in registry.CATEGORIES
    assert c.direction in registry.DIRECTIONS
    assert c.auth in registry.AUTHS
    assert c.level in registry.LEVELS
    assert set(c.formats) <= set(registry.FORMATS)
    assert set(c.data_out) <= set(registry.DATA_FIELDS)
    assert not set(c.data_out) & set(registry.FORBIDDEN_FIELDS)
    assert c.description_pl and c.docs_url.startswith("https://")
    assert isinstance(c.setup_pl, tuple)
    assert all(re.match(r"^[A-Z][A-Z0-9_]+$", s) for s in c.secrets)
    # no claim of real recordings anywhere in the registry text
    text = " ".join([c.notes_pl, c.description_pl, c.licence_pl]).lower()
    assert "nagrane z prawdziwych" not in text.replace("nie nagranych z prawdziwych", "")


@pytest.mark.parametrize("c", [c for c in registry.CONNECTORS if c.level == "planned"], ids=lambda c: c.id)
def test_planned_has_no_impl(c):
    assert c.live_impl is None and not c.formats and not c.secrets


@pytest.mark.parametrize("c", [c for c in registry.CONNECTORS if c.level == "live"], ids=lambda c: c.id)
def test_live_has_impl_and_fixtures(c):
    assert c.live_impl
    if c.id == "webhook-generic":
        return  # implemented and tested by the webhooks router (tests/integrations/test_webhooks*.py)
    importlib.import_module(c.live_impl)
    if c.id in ("crtsh", "rdap", "dns"):
        return  # existing enrichment modules with their own tests (tests/core)
    files = list((FIXTURES / c.id).glob("*.json"))
    assert files, f"no fixtures for live connector {c.id}"


def test_export_format_has_formats():
    for c in registry.CONNECTORS:
        if c.level == "export-format":
            assert c.formats, c.id


def test_ad_libraries_are_honest():
    lvl = {c.id: c.level for c in registry.CONNECTORS if c.category == "ad_transparency"}
    assert lvl["meta-adlibrary"] == "live"
    for cid in ("google-ads-transparency", "tiktok-commercial-content", "linkedin-ad-library", "x-ads-repository"):
        assert lvl[cid] == "planned"
    assert "brak publicznego api" in registry.get("google-ads-transparency").notes_pl.lower()


def test_openphish_sends_nothing_about_indicators():
    c = registry.get("openphish")
    assert c.data_out == () and "lokalnie" in c.description_pl.lower()


def test_presets_reference_known_connectors():
    ids = {c.id for c in registry.CONNECTORS}
    assert {"cert-misp", "siem", "ads-meta"} <= {p.id for p in registry.PRESETS}
    for p in registry.PRESETS:
        assert set(p.connectors) <= ids, p.id
