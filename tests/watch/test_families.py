from __future__ import annotations

from hydra.watch.families import FamilyIndex, LiveCampaigns


def test_live_campaign_from_template_burst():
    lc = LiveCampaigns()
    assert lc.add("transakcja1.sbs", "transakcja#.sbs") is None
    cid = lc.add("transakcja2.sbs", "transakcja#.sbs")
    assert cid and cid.startswith("W-")
    assert lc.campaign_of("transakcja1.sbs") == cid  # the earlier member joins retroactively
    assert lc.add("transakcja3.sbs", "transakcja#.sbs") == cid  # id stable as the campaign grows
    assert lc.count() == 1


def test_brand_subdomain_burst_on_one_domain():
    lc = LiveCampaigns()
    for h in ("allegro", "olx"):
        assert lc.add("burst123.sbs", "burst#.sbs", host=f"{h}.burst123.sbs") is None
    assert lc.add("burst123.sbs", "burst#.sbs", host="vinted.burst123.sbs") is not None


def test_infra_key_links_unrelated_names():
    lc = LiveCampaigns()
    lc.add("alpha.com", "alpha.com", ["ns:ns1.evil.com,ns2.evil.com"])
    assert lc.add("beta.com", "beta.com", ["ns:ns1.evil.com,ns2.evil.com"]) is not None
    assert lc.add("gamma.com", "gamma.com") is None


def test_family_window_expires():
    f = FamilyIndex()
    f.add("2026-11-01T00:00:00+00:00", "oferta-#.sbs", "oferta-1.sbs", None)
    f.add("2026-11-02T00:00:00+00:00", "oferta-#.sbs", "oferta-2.sbs", None)
    assert f.family_size("oferta-#.sbs") == 2
    f.add("2026-11-20T00:00:00+00:00", "x.sbs", "x.sbs", None)
    assert f.family_size("oferta-#.sbs") == 0


def test_shape_rarity_gate():
    from hydra.watch.link import shape_is_rare

    assert not shape_is_rare(0, 100)        # too little background: never trust a shape
    assert not shape_is_rare(300, 8000)     # 'L7.com'-like: common on the feed
    assert shape_is_rare(2, 8000)           # 1 in ~2700 names: may support assignment
