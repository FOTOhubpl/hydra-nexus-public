"""Passwords typed as composed or decomposed Unicode (macOS NFD paste) verify the same (2026-11-22)."""
import unicodedata

from hydra.auth import hash_password, verify_password


def test_nfc_and_nfd_forms_verify_the_same():
    pw = "Zażółć-gęślą-jaźń-2026"
    nfd = unicodedata.normalize("NFD", pw)
    assert nfd != pw
    assert verify_password(nfd, hash_password(pw))
    assert verify_password(pw, hash_password(nfd))
    assert not verify_password(pw + "x", hash_password(pw))
