"""PhishPLENS adapter on a synthetic fixture (no real e-mails)."""

from __future__ import annotations

from pathlib import Path

from hydra.adapters.datasets import phishplens

HDR = "file,classification,language,style,sender_suspicion_level,link_suspicion_level," \
      "attachments_suspicion_level,impersonation_likelihood,interaction_required,num_attachments,attachment_list\n"

PLAIN = (b"From: Bezpieczenstwo <a@example.invalid>\r\nSubject: =?utf-8?q?Has=C5=82o_wygas=C5=82o?=\r\n"
         b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n"
         b"Twoje has=C5=82o wygas=C5=82o. Zachowaj je teraz.\r\n")
HTML_ONLY = (b"From: Shop <s@example.invalid>\r\nSubject: Promo\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
             b"<html><style>p{}</style><p>Big&nbsp;sale</p><script>x()</script><div>today</div></html>\r\n")
MULTI = (b"From: x@example.invalid\r\nSubject: Faktura\r\nMIME-Version: 1.0\r\n"
         b"Content-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
         b"W zalaczniku faktura.\r\n--B\r\nContent-Type: text/html\r\nContent-Disposition: attachment; "
         b"filename=f.html\r\n\r\n<p>evil</p>\r\n--B--\r\n")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "phishplens"
    uni = root / "extracted" / "university"
    (uni / "PL").mkdir(parents=True)
    (uni / "EN").mkdir()
    (uni / "PL" / "001.eml").write_bytes(PLAIN)
    (uni / "PL" / "002.eml").write_bytes(MULTI)
    (uni / "EN" / "003.eml").write_bytes(HTML_ONLY)
    (uni / "labels_university.csv").write_text(
        HDR + "001.eml,phishing,pl,urgent,high,high,none_present,obvious,click_link,0,\n"
              "002.eml,phishing,pl,neutral,high,none_present,medium,obvious,open_attachment,1,f.html\n"
              "003.eml,spam,en,promotional,low,low,none_present,none,click_link,0,\n"
              "004.eml,ham,pl,neutral,low,low,none_present,none,none,0,\n")  # 004 has no file: skipped
    cr = root / "extracted" / "crafted" / "PL"
    cr.mkdir(parents=True)
    (cr / "001.eml").write_bytes(PLAIN.replace(b"wygas=C5=82o.", b"zmienione."))
    (root / "extracted" / "crafted" / "labels_crafted.csv").write_text(
        HDR + "001.eml,ham,pl,neutral,low,none_present,none_present,none,none,0,none\n")
    return root


def test_index_classes_lang_and_subsets(tmp_path: Path) -> None:
    rows = phishplens.index(_root(tmp_path))
    assert {r.key for r in rows} == {"university/001", "university/002", "university/003", "crafted/001"}
    assert all(r.label is None for r in rows)
    by = {r.key: r for r in rows}
    assert by["university/001"].group == "phishing" and by["university/001"].extra["lang"] == "pl"
    assert by["crafted/001"].group == "ham" and by["crafted/001"].extra["subset"] == "crafted"
    assert by["university/003"].extra["impersonation_likelihood"] == "none"
    pl = [r for r in rows if phishplens.where(lang="pl")(r)]
    assert len(pl) == 3
    assert [r.key for r in rows if phishplens.where(lang="pl", cls="phishing")(r)] == ["university/001",
                                                                                     "university/002"]


def test_load_text_from_plain_html_and_skips_attachments(tmp_path: Path) -> None:
    root = _root(tmp_path)
    incs = {i.key: i for i in phishplens.load(root, phishplens.index(root))}
    t1 = incs["university/001"].files["ad_text.txt"].decode()
    assert t1.startswith("Subject: Hasło wygasło\nFrom: Bezpieczenstwo <a@example.invalid>\n\n")
    assert "Twoje hasło wygasło" in t1
    t2 = incs["university/002"].files["ad_text.txt"].decode()
    assert "faktura" in t2 and "evil" not in t2
    t3 = incs["university/003"].files["ad_text.txt"].decode()
    assert "Big sale" in t3 and "today" in t3 and "x()" not in t3 and "<p>" not in t3
    one = incs["university/001"]
    assert one.source == "ext:phishplens" and one.label is None
    assert one.group_label == "ext:phishplens:phishing"
    assert one.meta["lang"] == "pl" and one.meta["subset"] == "university" and set(one.files) == {"ad_text.txt"}
