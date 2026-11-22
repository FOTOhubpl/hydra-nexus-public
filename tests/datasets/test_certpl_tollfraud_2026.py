"""certpl-tollfraud-2026 adapter on a synthetic post (structure only; no real indicators)."""

from __future__ import annotations

from pathlib import Path

from hydra.adapters.datasets import certpl_tollfraud_2026 as m

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_T = "c" * 64

POST = f"""<html><body>
<table><tr><th>Aplikacja lub pakiet</th><th>Reklamy</th><th>Stan analizy</th></tr>
<tr><td>Fake Chat</td><td>12</td><td>Potwierdzona próbka</td></tr>
<tr><td>org.example.other.pkg</td><td>3</td><td>Niepotwierdzona</td></tr></table>
<table><tr><th>Wyświetlana nazwa profilu</th><th>Identyfikator profilu Meta</th><th>Adresy</th><th>Reklamy</th></tr>
<tr><td>Jan Test</td><td>61500000000001</td><td>Fake Chat, Fake Cleaner</td><td>9</td></tr></table>
<table><tr><th>Aplikacja</th><th>Pakiet</th><th>Domena reguł</th><th>Odzyskany obiekt lub adres</th></tr>
<tr><td>Fake Cleaner</td><td>org.example.cleaner.app</td><td>api.zzzqqq[.]link</td><td>obj1</td></tr></table>
<table><tr><th>Host API</th><th>Zaobserwowane adresy IP</th><th>SHA-256 certyfikatu TLS</th></tr>
<tr><td>api.infra[.]click</td><td>192.0.2[.]10, 192.0.2[.]11</td><td>{SHA_T}</td></tr></table>
<h2>Wskaźniki kompromitacji</h2>
<h3>Fake Chat</h3><ul>
<li>Pakiet: org.example.fakechat.app</li>
<li>SHA-256 bazowego APK: {SHA_A}</li>
<li>Endpoint reguł: api.rulehost[.]link/a/b</li>
<li>C2 payloadu: 192.0.2[.]1/x</li>
<li>Początkowe identyfikatory Biblioteki reklam Meta: 1100000000000001 , 1100000000000002</li></ul>
<h3>Inne reklamowane aplikacje należące do operacji</h3>
<p>Other App - org.example.other.pkg - 3 reklam bazowy APK: {SHA_B}</p>
</body></html>"""


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "certpl-tollfraud-2026"
    root.mkdir()
    (root / "post-pl-20260101T000000Z.html").write_text(POST, encoding="utf-8")
    return root


def test_parse_and_index(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rows = m.index(root)
    groups = sorted(r.group for r in rows)
    assert groups.count("app") == 3 and groups.count("profile") == 1 and groups.count("infra") == 1
    assert all(r.label == m.OPERATION for r in rows)
    assert len({r.key for r in rows}) == len(rows)
    iocs = m.iocs(root)
    assert {SHA_A, SHA_B, SHA_T} <= iocs["sha256"]
    assert iocs["package"] == {"org.example.fakechat.app", "org.example.cleaner.app", "org.example.other.pkg"}
    assert {"api.rulehost.link", "api.zzzqqq.link", "api.infra.click"} <= iocs["host"]
    assert {"192.0.2.1", "192.0.2.10", "192.0.2.11"} <= iocs["ip"]
    assert iocs["meta_profile_id"] == {"61500000000001"}
    assert iocs["ad_library_id"] == {"1100000000000001", "1100000000000002"}


def test_load(tmp_path: Path) -> None:
    root = _root(tmp_path)
    incs = {i.key: i for i in m.load(root, m.index(root))}
    chat = incs["app-org.example.fakechat.app"]
    assert chat.source == "ext:certpl-tollfraud-2026"
    assert chat.label == "ext:certpl-tollfraud-2026:operation:tollfraud-2026"
    assert chat.group_label == "ext:certpl-tollfraud-2026:app"
    assert chat.meta["leakage_risk"] is True and chat.meta["ads"] == 12
    ind = chat.files["indicators.txt"].decode()
    assert f"sha256: {SHA_A}" in ind and "package: org.example.fakechat.app" in ind
    assert "advertiser_profile_id: 61500000000001" in ind and "ad_library_id: 1100000000000001" in ind
    assert chat.files["url.txt"] == b"http://api.rulehost.link/\n"
    other = incs["app-org.example.other.pkg"]  # table row named by package merges with the named entry
    assert other.meta["app"] == "Other App" and other.meta["ads"] == 3
    prof = incs["profile-61500000000001"]
    pind = prof.files["indicators.txt"].decode()
    assert "package: org.example.cleaner.app" in pind and "package: org.example.fakechat.app" in pind
    infra = incs["infra-api.infra.click"]
    assert f"tls_sha256: {SHA_T}" in infra.files["indicators.txt"].decode()
    assert infra.roles == {"indicators.txt": "text"}
