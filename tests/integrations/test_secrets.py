"""Secret store hygiene: 0600/0700 under the temporary HOME, value never echoed anywhere."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

from hydra.integrations import secrets
from integrations.conftest import SECRET


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_file_permissions_and_location(temp_home: Path, tmp_path: Path):
    st = secrets.set_secret("virustotal", "VT_API_KEY", SECRET)
    assert st == {"name": "VT_API_KEY", "configured": True, "source": "file", "last4": SECRET[-4:]}
    d = temp_home / ".config" / "hydra"
    f = d / "virustotal.env"
    assert f.is_file() and _mode(f) == 0o600 and _mode(d) == 0o700
    # nothing else written outside temp HOME/.config/hydra (except pytest's own tmp dirs)
    written = [p for p in tmp_path.rglob("*") if p.is_file() and SECRET in p.read_text(errors="ignore")]
    assert written == [f]
    assert secrets.get_secret("virustotal", "VT_API_KEY") == SECRET


def test_env_overrides_and_short_values(monkeypatch):
    monkeypatch.setenv("VT_API_KEY", "short")
    assert secrets.status("virustotal", "VT_API_KEY") == {"name": "VT_API_KEY", "configured": True,
                                                          "source": "env", "last4": None}


def test_clear(temp_home: Path):
    secrets.set_secret("urlscan", "URLSCAN_API_KEY", SECRET)
    assert secrets.clear_secret("urlscan", "URLSCAN_API_KEY")["configured"] is False
    assert not (temp_home / ".config" / "hydra" / "urlscan.env").exists()


def test_bad_names_rejected():
    for bad in ("lower", "A", "X-Y"):
        try:
            secrets.set_secret("urlscan", bad, SECRET)
        except secrets.SecretError as exc:
            assert SECRET not in str(exc)
        else:
            raise AssertionError(bad)


def _scan(root: Path, value: str) -> list[Path]:
    hits = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                if value.encode() in p.read_bytes():
                    hits.append(p)
            except OSError:
                pass
    return hits


def test_value_never_in_api_state_cache_or_logs(online_client, caplog, temp_home: Path):
    c = online_client
    caplog.set_level(logging.DEBUG)
    t = c.transport_rec
    t.add("virustotal.com", json.loads((Path(__file__).parent / "fixtures/virustotal/domain_malicious.json")
                                       .read_text()))
    bodies = []
    r = c.put("/v1/integrations/virustotal/secret", json={"name": "VT_API_KEY", "value": SECRET})
    assert r.status_code == 200
    bodies.append(r.text)
    for path, payload in [("/v1/integrations/virustotal/test", None),
                          ("/v1/integrations/virustotal/connect", {"secrets": {"VT_API_KEY": SECRET}}),
                          ("/v1/integrations/virustotal/run", {"indicators": ["energia-zysk.example"]}),
                          ("/v1/integrations/virustotal/preview", {"indicators": ["energia-zysk.example"]})]:
        r = c.post(path, json=payload)
        assert r.status_code == 200, (path, r.text)
        bodies.append(r.text)
    for path in ("/v1/integrations", "/v1/integrations/virustotal", "/openapi.json"):
        bodies.append(c.get(path).text)
    assert all(SECRET not in b for b in bodies)
    # the key really went to the provider (header), and nowhere else
    assert any(call["headers"].get("x-apikey") == SECRET for call in t.calls)
    settings = c.settings
    assert _scan(Path(settings.state_dir), SECRET) == []  # state.json, DB (audit), integrations dir
    assert _scan(Path(settings.cache_dir), SECRET) == []  # enrichment cache
    assert SECRET not in caplog.text
    assert [p.name for p in _scan(temp_home, SECRET)] == ["virustotal.env"]
    preview = c.post("/v1/integrations/virustotal/preview", json={"indicators": ["energia-zysk.example"]}).json()
    assert preview["payload"][0]["headers"]["x-apikey"] == "•••• " + SECRET[-4:]
    st = c.get("/v1/integrations/virustotal").json()["secrets"][0]
    assert st == {"name": "VT_API_KEY", "configured": True, "source": "file", "last4": SECRET[-4:]}
    assert os.environ.get("VT_API_KEY") is None
