"""Synthetic web material (fictional domains under .example / .test, invented ids). Never organiser data."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from hydra.contracts import Incident, IncidentArtifact
from hydra.genome.base import ResolvedArtifact, ResolvedIncident

PIXEL = "123456789012345"
TOKEN = "EAABsecretTOKENvalue1234567890abcdef"
LANDING = (
    "https://www.news-247daily-fixture.live/l1/?utm_visit=01923456-7abc-7def-8123-456789abcdef&sub1=camp42"
    "&access_token=" + TOKEN
)
ICO = b"\x00\x00\x01\x00" + b"fake-icon-bytes" * 50

HTML = f"""<!-- saved from url=(0099){LANDING} -->
<!DOCTYPE html><html lang="pl"><head><title>Szokujące wyznanie — Gazeta.pl</title>
<meta property="og:site_name" content="Gazeta.pl"><meta property="og:title" content="Szokujące wyznanie">
<link rel="canonical" href="https://wiadomosci.gazeta.pl/fake/1">
<link rel="icon" href="favicon.ico"><link rel="stylesheet" href="./Page_files/main.css">
<script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
<script src="./Page_files/bundle.app.js"></script>
<script>!function(f,b,e,v,n,t,s){{}}(window,document,'script');fbq('init', '{PIXEL}');fbq('track','PageView');
if (navigator.webdriver) {{ window.location.href = "https://white.safe-fixture.org/about"; }}</script>
<meta http-equiv="refresh" content="30; url=https://next-hop-fixture.org/r">
</head><body><header class="masthead">
<img class="logo" alt="Gazeta.pl" src="logo-gazeta.svg"><nav>Wiadomości</nav></header>
<div id="calcSlider" class="calculator-block">
<p>Zadzwoń: +48 601 234 567, napisz: kontakt@zysk-fikcja-fixture.pl</p></div>
<form id="lead" action="/api/lead.php" method="post"><input name="first_name"><input name="phone">
<input type="hidden" name="offer_uuid" value="11111111-2222-3333-4444-555555555555">
<input type="hidden" name="sub1"></form>
<div class="comments"><div class="fb-comment"><span class="comment-name">Anna Testowa</span>
<p class="comment-text">Zarobiłam w tydzień tyle co w miesiąc, polecam wszystkim sąsiadom.</p></div>
<div class="fb-comment"><span class="comment-name">Jan Fikcyjny</span>
<p class="comment-text">Najpierw nie wierzyłem, ale wypłata przyszła po dwóch dniach na konto.</p></div></div>
<footer>© Gazeta.pl fake footer</footer></body></html>"""

JS_BUNDLE = ("var api='/api/v1/trigger/field/'+uuid+'/';" + "function x(){return 1;}" * 200).encode()
CSS = (".calculator-block{color:red}" * 100).encode()


def har_doc() -> dict:
    def entry(
        url,
        status,
        mime,
        text=None,
        ip="203.0.113.10",
        headers=None,
        method="GET",
        post=None,
        redirect="",
        rtype="document",
        b64=False,
    ):
        content = {"mimeType": mime, "size": len(text or "")}
        if text is not None:
            content["text"] = text
            if b64:
                content["encoding"] = "base64"
        req = {"method": method, "url": url, "headers": []}
        if post:
            req["postData"] = post
        return {
            "startedDateTime": "2026-10-09T08:00:00.000Z",
            "time": 10,
            "_resourceType": rtype,
            "serverIPAddress": ip,
            "request": req,
            "response": {"status": status, "headers": headers or [], "redirectURL": redirect, "content": content},
        }

    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "WebInspector"},
            "pages": [{"title": "https://click.tds-hop-fixture.top/abc?key=k1&_subid=xyz"}],
            "entries": [
                entry(
                    "https://click.tds-hop-fixture.top/abc?key=k1&_subid=xyz",
                    302,
                    "text/html",
                    "",
                    redirect=LANDING,
                    ip="198.51.100.7",
                    headers=[
                        {"name": "Server", "value": "nginx"},
                        {"name": "Set-Cookie", "value": "_subid=abc; path=/"},
                    ],
                ),
                entry(
                    LANDING,
                    200,
                    "text/html",
                    HTML,
                    headers=[
                        {"name": "server", "value": "cloudflare"},
                        {"name": "cf-ray", "value": "8abc-WAW"},
                        {"name": "x-powered-by", "value": "PHP/8.3.1"},
                    ],
                ),
                entry(
                    "https://www.googletagmanager.com/gtag/js?id=G-ABCDEFGH12",
                    200,
                    "application/javascript",
                    "gtag('config','G-ABCDEFGH12');",
                    ip="142.250.1.1",
                    rtype="script",
                ),
                entry(
                    f"https://www.facebook.com/tr/?id={PIXEL}&ev=PageView",
                    200,
                    "image/gif",
                    None,
                    ip="157.240.1.1",
                    rtype="image",
                ),
                entry(
                    "https://www.news-247daily-fixture.live/favicon.ico",
                    200,
                    "image/x-icon",
                    base64.b64encode(ICO).decode(),
                    b64=True,
                    rtype="image",
                ),
                entry(
                    "https://www.news-247daily-fixture.live/api/v1/trigger/field/0f0f0f0f-1111-2222-3333-444444444444/",
                    200,
                    "application/json",
                    "{}",
                    method="POST",
                    rtype="xhr",
                    post={
                        "mimeType": "application/json",
                        "text": json.dumps(
                            {"landed_on_landings_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "fingerprint": "x"}
                        ),
                    },
                ),
            ],
        }
    }


def _art(root: Path, rel: str, data: bytes, role: str, mime: str = "") -> ResolvedArtifact:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    meta = IncidentArtifact(
        artifact_id=hashlib.sha256(data).hexdigest(), role=role, path_in_incident=rel, mime=mime, size=len(data)
    )
    return ResolvedArtifact(meta, p)


def web_incident(root: Path, iid: str = "t-1", *, har: bool = True, html: bool = True) -> ResolvedIncident:
    arts = []
    if har:
        arts.append(
            _art(root, "news-247daily-fixture.live.har", json.dumps(har_doc()).encode(), "har", "application/json")
        )
    if html:
        arts.append(_art(root, "Page.html", HTML.encode(), "landing_html", "text/html"))
        arts.append(_art(root, "Page_files/bundle.app.js.pobrane", JS_BUNDLE, "landing_asset"))
        arts.append(_art(root, "Page_files/main.css", CSS, "landing_asset"))
        arts.append(_art(root, "Page_files/jquery-3.6.0.min.js.pobrane", b"/*jq*/" + b"x" * 2000, "landing_asset"))
        arts.append(_art(root, "Page_files/favicon.ico", ICO, "landing_asset"))
    inc = Incident(id=iid, source="test", source_ref=iid, artifacts=[a.meta for a in arts])
    return ResolvedIncident(inc, arts)


@pytest.fixture(autouse=True)
def _synthetic_warninglists(monkeypatch):
    """INF tests use a tiny synthetic MISP warninglist snapshot, never the real one under HYDRA_DATA_DIR.
    Autouse in every test module that imports it (`from fixtures import _synthetic_warninglists`)."""
    monkeypatch.setenv("HYDRA_MISP_WL_DIR", str(Path(__file__).parent / "wl_fixture"))
