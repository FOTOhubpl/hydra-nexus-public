"""REST API of the integrations hub: statuses, 404/409/422/503, offline fail-closed, preview, export, run, presets."""

from __future__ import annotations

import json

import pytest

from hydra.integrations import registry
from integrations.conftest import SECRET, fixture

PLANNED = [c.id for c in registry.CONNECTORS if c.level == "planned"]


def test_list_shape(offline_client):
    j = offline_client.get("/v1/integrations").json()
    assert j["offline"] is True and len(j["connectors"]) == len(registry.CONNECTORS)
    assert {c["id"] for c in j["categories"]} == set(registry.CATEGORIES)
    assert {p["id"] for p in j["presets"]} >= {"cert-misp", "siem", "ads-meta"}
    by = {c["id"]: c for c in j["connectors"]}
    assert by["tiktok-commercial-content"]["status"] == "planned"
    assert not any(by["tiktok-commercial-content"][k] for k in ("can_connect", "can_test", "can_run", "can_export"))
    assert by["virustotal"]["status"] == "not_configured"
    assert by["csv-export"]["status"] == "configured" and by["csv-export"]["can_export"]
    assert by["openphish"]["status"] == "offline"  # live, nothing to configure, offline


def test_presets_route_not_captured(offline_client):
    r = offline_client.get("/v1/integrations/presets")
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_unknown_404(offline_client):
    assert offline_client.get("/v1/integrations/nope").status_code == 404
    assert offline_client.post("/v1/integrations/nope/test").status_code == 404
    assert offline_client.post("/v1/integrations/presets/nope/apply").status_code == 404


@pytest.mark.parametrize("cid", PLANNED)
def test_planned_409(offline_client, cid):
    c = offline_client
    assert c.post(f"/v1/integrations/{cid}/test").status_code == 409
    assert c.post(f"/v1/integrations/{cid}/connect", json={}).status_code == 409
    assert c.post(f"/v1/integrations/{cid}/run", json={}).status_code == 409
    assert c.get(f"/v1/integrations/{cid}/export").status_code == 409
    assert c.post(f"/v1/integrations/{cid}/preview", json={}).status_code == 409
    assert c.put(f"/v1/integrations/{cid}/secret", json={"name": "X_KEY", "value": SECRET}).status_code == 409
    assert c.delete(f"/v1/integrations/{cid}/secret", params={"name": "X_KEY"}).status_code == 409
    assert c.post(f"/v1/integrations/{cid}/enable", json={"enabled": True}).status_code == 409


def test_secret_validation(offline_client):
    c = offline_client
    assert c.put("/v1/integrations/virustotal/secret", json={"name": "OTHER", "value": SECRET}).status_code == 422
    assert c.put("/v1/integrations/virustotal/secret", json={"name": "VT_API_KEY", "value": "a\nb"}).status_code \
        == 422
    r = c.put("/v1/integrations/virustotal/secret", json={"name": "VT_API_KEY", "value": SECRET})
    assert r.json() == {"name": "VT_API_KEY", "configured": True, "source": "file", "last4": SECRET[-4:]}
    r = c.delete("/v1/integrations/virustotal/secret", params={"name": "VT_API_KEY"})
    assert r.json()["configured"] is False


@pytest.mark.parametrize("cid", ["virustotal", "urlscan", "urlhaus", "openphish", "misp", "splunk-hec"])
def test_offline_test_and_connect_fail_closed(offline_client, cid):
    c = offline_client
    conn = registry.get(cid)
    secrets = {n: (f"https://{cid}.example.org" if n.endswith("_URL") else SECRET) for n in conn.secrets}
    r = c.post(f"/v1/integrations/{cid}/test")
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["status"] in ("offline", "not_configured")
    r = c.post(f"/v1/integrations/{cid}/connect", json={"secrets": secrets or None})
    j = r.json()
    assert r.status_code == 200 and j["ok"] is False
    test_step = next(s for s in j["steps"] if s["id"] == "test")
    assert test_step["ok"] is False and "HYDRA_OFFLINE=0" in test_step["message_pl"]
    if conn.level == "live":
        assert j["connector"]["status"] == "offline" and j["connector"]["enabled"] is True
    r = c.post(f"/v1/integrations/{cid}/run", json={"indicators": ["energia-zysk.example"]})
    assert r.status_code == 503 and r.json()["status"] == "offline" and "HYDRA_OFFLINE" in r.json()["detail"]
    assert c.transport_rec.calls == []
    assert SECRET not in r.text + json.dumps(j)


def test_online_connect_test_run_virustotal(online_client):
    c = online_client
    c.transport_rec.add("virustotal.com/api/v3/domains/example.com", fixture("virustotal", "domain_clean.json"))
    c.transport_rec.add("virustotal.com/api/v3/domains/energia-zysk", fixture("virustotal", "domain_malicious.json"))
    r = c.post("/v1/integrations/virustotal/run", json={"indicators": ["energia-zysk.example"]})
    assert r.status_code == 409  # not enabled yet
    j = c.post("/v1/integrations/virustotal/connect", json={"secrets": {"VT_API_KEY": SECRET}}).json()
    assert j["ok"] is True and [s["id"] for s in j["steps"]] == ["secret", "test", "enable"]
    assert j["connector"]["status"] == "configured" and j["connector"]["enabled"]
    r = c.post("/v1/integrations/virustotal/run", json={"indicators": ["energia-zysk.example", "bad value", "x"]})
    j = r.json()
    assert r.status_code == 200 and j["ok"] and j["summary"] == {"malicious": 1}
    assert j["rejected"] == ["bad value", "x"]
    d = c.get("/v1/integrations/virustotal").json()
    types = {e["type"] for e in d["audit"]}
    assert {"INTEGRATION_REQUEST", "INTEGRATION_TEST", "INTEGRATION_RUN", "INTEGRATION_SECRET"} <= types
    assert d["last_run"]["ok"] and d["runs"] and d["usage"]["requests_60m"] == 2
    assert SECRET not in json.dumps(d)


def test_online_run_from_run_indicators(online_client):
    c = online_client
    c.transport_rec.add("urlhaus-api.abuse.ch/v1/host/", fixture("urlhaus", "host_ok.json"))
    c.put("/v1/integrations/urlhaus/secret", json={"name": "URLHAUS_AUTH_KEY", "value": SECRET})
    c.post("/v1/integrations/urlhaus/enable", json={"enabled": True})
    j = c.post("/v1/integrations/urlhaus/run", json={"campaign_id": "syn-c1"}).json()
    assert j["ok"] and j["results"] and j["results"][0]["verdict"] == "malicious"
    assert all(call["headers"]["Auth-Key"] == SECRET for call in c.transport_rec.calls)


def test_preview_misp_masks_headers_and_sends_nothing(online_client):
    c = online_client
    c.put("/v1/integrations/misp/secret", json={"name": "MISP_URL", "value": "https://misp.example.org"})
    c.put("/v1/integrations/misp/secret", json={"name": "MISP_KEY", "value": SECRET})
    j = c.post("/v1/integrations/misp/preview", json={"campaign_id": "syn-c1"}).json()
    assert j["method"] == "POST" and j["destination"] == "https://misp.example.org/events/add"
    assert j["headers"]["Authorization"] == "•••• " + SECRET[-4:]
    assert j["payload"]["Event"]["Attribute"] and j["sha256"] and j["bytes"] > 0
    assert c.transport_rec.calls == [] and SECRET not in json.dumps(j)


def test_preview_file_only_and_bad_format(offline_client):
    c = offline_client
    j = c.post("/v1/integrations/csv-export/preview", json={}).json()
    assert j["destination"] == "plik" and j["format"] == "csv" and "value" in j["payload"]
    assert c.post("/v1/integrations/csv-export/preview", json={"format": "misp"}).status_code == 422
    assert c.post("/v1/integrations/csv-export/preview", json={"run_id": "nope"}).status_code == 404


@pytest.mark.parametrize("cid,fmt,ctype", [("csv-export", "csv", "text/csv"), ("misp", "misp", "application/json"),
                                           ("elasticsearch", "es_bulk", "application/x-ndjson"),
                                           ("sql-export", "sql", "application/sql"),
                                           ("opencti-taxii", "stix21", "application/stix+json"),
                                           ("xarf", "xarf", "application/zip")])
def test_export_download_offline(offline_client, cid, fmt, ctype):
    params = {"format": fmt, **({"campaign_id": "syn-c1"} if fmt == "xarf" else {})}
    r = offline_client.get(f"/v1/integrations/{cid}/export", params=params)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(ctype) and "attachment" in r.headers["content-disposition"]
    d = offline_client.get(f"/v1/integrations/{cid}").json()
    assert any(e["type"] == "INTEGRATION_EXPORT" for e in d["audit"])


def test_push_splunk_online(online_client):
    c = online_client
    c.transport_rec.add("/services/collector/event", fixture("splunk-hec", "event.json"))
    j = c.post("/v1/integrations/splunk-hec/connect", json={"secrets": {
        "SPLUNK_HEC_URL": "https://splunk.example.org:8088", "SPLUNK_HEC_TOKEN": SECRET}}).json()
    assert j["steps"][1]["id"] == "test"
    c.post("/v1/integrations/splunk-hec/enable", json={"enabled": True})
    j = c.post("/v1/integrations/splunk-hec/run", json={}).json()
    assert j["ok"] and j["sent"]["bytes"] > 0
    call = c.transport_rec.calls[-1]
    assert call["headers"]["Authorization"] == f"Splunk {SECRET}"
    assert b"hydra:indicator" in call["body"]
    assert SECRET not in json.dumps(j)


def test_slack_webhook_url_is_a_secret(online_client):
    c = online_client
    hook_path = "/services/T0001/B0001/zXyWvUtSrQpOnMlK"
    hook = "https://hooks.slack.com" + hook_path
    c.transport_rec.add("hooks.slack.com", b"ok")
    j = c.post("/v1/integrations/slack/connect", json={"secrets": {"SLACK_WEBHOOK_URL": hook}}).json()
    assert j["ok"], j
    pv = c.post("/v1/integrations/slack/preview", json={}).json()
    assert pv["method"] == "POST" and "zXyWvUtSrQpOnMlK" not in json.dumps(pv)
    r = c.post("/v1/integrations/slack/run", json={})
    assert r.status_code == 200 and r.json()["ok"]
    assert "text" in json.loads(c.transport_rec.calls[-1]["body"])
    d = c.get("/v1/integrations/slack").json()
    req_events = [e for e in d["audit"] if e["type"] == "INTEGRATION_REQUEST"]
    assert req_events and all(hook_path not in json.dumps(e) for e in req_events)
    assert "zXyWvUtSrQpOnMlK" not in json.dumps(d) + c.get("/v1/integrations").text


def test_preset_apply_offline(offline_client):
    j = offline_client.post("/v1/integrations/presets/cert-misp/apply").json()
    assert j["preset"]["id"] == "cert-misp"
    by = {r["connector"]: r for r in j["results"]}
    assert by["opencti-taxii"]["ok"] and by["xarf"]["ok"]
    assert by["misp"]["status"] == "not_configured"
    assert offline_client.transport_rec.calls == []


def test_webhook_generic_managed_elsewhere(offline_client):
    assert offline_client.post("/v1/integrations/webhook-generic/connect", json={}).status_code == 409
    assert offline_client.post("/v1/integrations/webhook-generic/test").status_code == 409


def test_openapi_documents_endpoints(offline_client):
    spec = offline_client.get("/openapi.json").json()
    ops = [op for p, item in spec["paths"].items() if p.startswith("/v1/integrations") and "webhooks" not in p
           for op in item.values()]
    assert len(ops) >= 12
    for op in ops:
        assert op.get("summary") and op.get("description") and "integrations" in op.get("tags", [])
