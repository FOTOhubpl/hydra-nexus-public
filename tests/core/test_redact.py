from hydra.contracts import REDACTED
from hydra.redact import redact_obj, redact_text, redact_url, secret_fingerprint


def test_url_secrets_are_redacted_and_other_params_kept():
    u = "https://x.example/?utm_source=fb&access_token=EAAFakeToken0FictionalValue9TestOnly0123456789abcd&key=abc123&ad_id=12"
    r = redact_url(u)
    assert "EAAU" not in r and "abc123" not in r
    assert "utm_source=fb" in r and "ad_id=12" in r and r.count(REDACTED) == 2
    assert redact_url(r) == r  # idempotent


def test_local_paths_jwt_and_text():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk"
    t = f"file:///C:/Users/JanKowalski/Downloads/p.html?utm_id=1 and {jwt}"
    r = redact_text(t)
    assert "JanKowalski" not in r and "eyJhbGci" not in r and "utm_id=1" in r
    assert redact_text("/Users/alice/x") == f"/Users/{REDACTED}/x"
    assert redact_text(None) is None


def test_obj_and_fingerprint():
    o = redact_obj({"api_key": "s3cr3t", "url": "https://a.b/?token=zz&x=1", "n": 3, "l": ["C:\\Users\\bob\\f"]})
    assert o["api_key"] == REDACTED and "zz" not in o["url"] and o["n"] == 3 and "bob" not in o["l"][0]
    assert secret_fingerprint("a") == secret_fingerprint("a") != secret_fingerprint("b")


def test_telegram_bot_token_is_redacted_keeping_the_id():
    tok = "123456789:AAFakeFictionalTokenValue0123456789ABCDE"
    secret = tok.split(":", 1)[1]
    assert redact_text(f"const t='{tok}';") == f"const t='123456789:{REDACTED}';"
    # also inside an api.telegram.org/bot<token>/ URL
    r = redact_text(f"https://api.telegram.org/bot{tok}/sendMessage?chat_id=-100")
    assert secret not in r and "bot123456789:" + REDACTED in r and "chat_id=-100" in r
    assert redact_url(f"https://api.telegram.org/bot{tok}/sendMessage").count(REDACTED) == 1


def test_discord_webhook_token_is_redacted_keeping_the_id():
    url = "https://discord.com/api/webhooks/123456789012345678/" + "T" * 68
    r = redact_text(url)
    assert "T" * 68 not in r
    assert "webhooks/123456789012345678/" + REDACTED in r
    # canary subdomain + api version
    r2 = redact_text("https://canary.discordapp.com/api/v10/webhooks/987654321098765432/" + "z" * 68)
    assert "z" * 68 not in r2 and "987654321098765432" in r2


def test_contract_fields_are_not_redacted_by_name():
    o = redact_obj({"enrichment_key": "ab12cd34ef56ab12", "dependency_group": "INF:operator:key",
                    "key": "f" * 64, "api_key": "hunter2hunter2", "lpkey": "h:0123456789abcdef"})
    assert o["enrichment_key"] == "ab12cd34ef56ab12" and o["dependency_group"] == "INF:operator:key"
    assert o["key"] == "f" * 64 and o["api_key"] == REDACTED and o["lpkey"] == "h:0123456789abcdef"
