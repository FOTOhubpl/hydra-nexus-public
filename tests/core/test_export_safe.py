"""Export serialisation helpers and context-aware redaction: safety plus no false positives on ordinary text."""

import csv
import io
import json

import pytest

from hydra.contracts import REDACTED
from hydra.export_safe import content_disposition, csv_cell, cypher_comment, cypher_string, safe_filename
from hydra.redact import redact_document, redact_obj, redact_text
from tests.security.cypher import statements


@pytest.mark.parametrize("payload", ["=1+1", "+1", "-1", "@SUM(1)", "\t=1", "\r=1", " =1", "＝1"])
def test_csv_cells_never_start_a_formula(payload):
    buf = io.StringIO()
    csv.writer(buf).writerow([csv_cell(payload)])
    cell = next(csv.reader(io.StringIO(buf.getvalue())))[0]
    assert cell.startswith("'")


def test_csv_cell_keeps_numbers_and_plain_text():
    assert csv_cell(-0.5) == -0.5 and csv_cell(3) == 3 and csv_cell(None) == ""
    assert csv_cell("Inwestycja w kryptowaluty") == "Inwestycja w kryptowaluty"
    assert csv_cell("a\x00b‮c") == "abc"


def test_cypher_literals_and_comments_cannot_open_statements():
    text = 'x"; CREATE (:Injected); // \\ \n\r  end'
    script = f"// run {cypher_comment(text)}\nMERGE (n:N {{v: {cypher_string(text)}}});\n"
    parsed = statements(script)
    assert len(parsed) == 1 and "Injected" not in parsed[0]
    assert cypher_string(None) == "null" and cypher_string(True) == "true" and cypher_string(2) == "2"


def test_download_names_are_header_safe():
    header = content_disposition('C-1"\r\nX-Injected: yes.zip')["Content-Disposition"]
    assert "\r" not in header and "\n" not in header and header.count('"') == 2
    assert safe_filename("../../etc/passwd") == "etc_passwd"
    assert safe_filename("") == "export"


def test_redaction_has_no_false_positives_on_ordinary_material():
    for text in ["Zainwestuj teraz: zysk 300%!", "key: value", '<input name="email" value="jan@example.pl">',
                 "https://paypal.com@phish.example/login", '{"kind": "operator_id", "key": "abc"}', 'x="1"']:
        assert redact_text(text) == text


def test_redaction_of_html_json_js_userinfo_and_har():
    assert REDACTED in redact_text("<input value='s3cr3t' type='password'>")
    assert REDACTED in redact_text('<meta name="csrf-token" content="QQQ">')
    assert "S3" not in redact_text('var apiKey = "S3cr3t";')
    assert redact_text("ftp://u:p4ss@host/x") == f"ftp://u:{REDACTED}@host/x"
    har = {"log": {"entries": [{"request": {"headers": [{"name": "Cookie", "value": "sid=1"},
                                                        {"name": "Accept", "value": "*/*"}],
                                            "cookies": [{"name": "anything", "value": "v"}]}}]}}
    out = redact_obj(har)["log"]["entries"][0]["request"]
    assert out["headers"][0]["value"] == REDACTED and out["headers"][1]["value"] == "*/*"
    assert out["cookies"][0]["value"] == REDACTED
    assert redact_obj({"lpkey": "h:0123456789ab", "password": "ab12" * 8}) == {
        "lpkey": "h:0123456789ab", "password": REDACTED}


def test_redact_document_is_byte_identical_when_clean():
    clean = b"<html><p>ok</p></html>"
    assert redact_document(clean, "text/html") is clean
    dirty = json.dumps({"request": {"headers": [{"name": "X-Api-Key", "value": "zzz"}]}}).encode()
    assert b"zzz" not in redact_document(dirty, "application/json")
