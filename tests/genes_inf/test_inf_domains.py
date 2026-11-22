from __future__ import annotations

from fixtures import _synthetic_warninglists  # noqa: F401

from hydra.contracts import REDACTED
from hydra.enrich.domains import (
    icann_domain,
    is_boilerplate,
    path_skeleton,
    redact_text,
    redact_url,
    registrable_domain,
    url_label_skeleton,
)
from hydra.genome.webparse import favicon_hash, mmh3_32, norm_js


def test_mmh3_matches_reference_vectors():
    # reference values of mmh3.hash (MurmurHash3 x86_32, seed 0, signed)
    assert mmh3_32(b"") == 0
    assert mmh3_32(b"foo") == -156908512
    assert mmh3_32(b"hello") == 613153351
    assert isinstance(favicon_hash(b"\x00\x01icon"), int)


def test_registrable_domain_keeps_platform_tenants_apart():
    assert registrable_domain("a.b.shop-one.netlify.app") == "shop-one.netlify.app"
    assert icann_domain("a.b.shop-one.netlify.app") == "netlify.app"
    assert registrable_domain("www.x.co.uk") == "x.co.uk"
    assert registrable_domain("192.0.2.1") is None


def test_label_and_path_skeletons():
    assert url_label_skeleton("www.shop-42deals.xyz") == "L3.L4-D2L5.xyz"
    assert url_label_skeleton("acrossworld.live") == "L11.live"
    assert path_skeleton("/l/12345/0f0f0f0f-1111-2222-3333-444444444444/ab12cd34ef") == "/l/{num}/{uuid}/{tok10}"


def test_boilerplate_suffix_matching_on_label_boundary():
    assert is_boilerplate("www.googletagmanager.com")
    assert not is_boilerplate("notgoogle.com")


def test_redaction_of_tokens_and_local_paths():
    u = redact_url("https://x.example/a?access_token=EAABxyz1234567890abcdefghij&utm_id=abc&api_key=s3cr3t")
    assert "EAAB" not in u and "s3cr3t" not in u and "utm_id=abc" in u and REDACTED in u
    assert "Jan" not in redact_text("file:///C:/Users/Jan/Downloads/page.html")
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert redact_text(sha) == sha  # digests are not secrets


def test_norm_js_masks_ids_and_whitespace():
    a = "var id = 1234567890;\n  // c\nload('0f0f0f0f-1111-2222-3333-444444444444')"
    b = "var id=9876543210;\n   load('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')"
    assert norm_js(a) == norm_js(b)
