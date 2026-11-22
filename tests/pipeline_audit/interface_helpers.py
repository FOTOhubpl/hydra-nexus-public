"""Synthetic persisted revisions and real frontend request serialization for interface audits."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hydra import events
from hydra.contracts import Campaign, GeneMatch, PairScore
from hydra.db import Writer
from tests.security.helpers import seed

OLD_RUN = "run-interface-old"
NEW_RUN = "run-interface-new"
CAMPAIGN = "C-interface"
NEW_CAMPAIGN = "C-interface-new"
OLD_IDS = {"astra-a", "astra-b"}
NEW_IDS = {"interface-c", "interface-d"}
TOKEN = "PIPELINE_SYNTHETIC_CREDENTIAL_8k4"
SECRET = f"https://operator.example/?access_token={TOKEN}"


def seed_two_runs(settings, *, secret=False):
    """Two disjoint analyses use distinct campaign IDs, consistent with member-derived IDs."""
    title = SECRET if secret else "Synthetic older campaign"
    value = SECRET if secret else "operator-old"
    seed(settings, title=title, value=value, campaign_id=CAMPAIGN, run_id=OLD_RUN)
    stamp = "2026-11-21T19:00:00Z"
    reason = GeneMatch(family="INF", kind="operator_id", value_a="operator-new", value_b="operator-new",
                       bits=12, explanation_pl="Syntetyczny wspólny identyfikator operatora.")
    campaign = Campaign(id=NEW_CAMPAIGN, label="Synthetic newer campaign", members=sorted(NEW_IDS),
                        core_genome=[reason])
    pair = PairScore(a="interface-c", b="interface-d", reasons=[reason], family_bits={"INF": 12},
                     total_bits=12, probability=0.0, hypothesis="same_campaign", model="synthetic-interface@1")

    def insert(conn):
        conn.execute("INSERT INTO runs(id,kind,params,created_at,finished_at) VALUES (?,'analyse',?,?,?)",
                     (NEW_RUN, events.canonical_json({"synthetic": True}), stamp, stamp))
        for iid in sorted(NEW_IDS):
            conn.execute("INSERT INTO incidents(id,source,source_ref,title,observed_at,created_at)"
                         " VALUES (?,'interface-synthetic',?,?,?,?)",
                         (iid, f"synthetic/{iid}", "Synthetic newer incident", stamp, stamp))
            for kind, value in (("operator_id", "operator-new"), ("landing_host", "newer.example")):
                conn.execute("INSERT INTO genes(incident_id,family,kind,value,status,provenance,extractor,"
                             "dependency_group,created_at) VALUES (?,'INF',?,?,'observed','OBS',"
                             "'synthetic-interface@1','inf:operator:test',?)", (iid, kind, value, stamp))
            conn.execute("INSERT INTO campaign_members VALUES (?,?,?)", (NEW_RUN, NEW_CAMPAIGN, iid))
        conn.execute("INSERT INTO campaigns(id,run_id,label,confidence,detail) VALUES (?,?,?,?,?)",
                     (NEW_CAMPAIGN, NEW_RUN, campaign.label, campaign.confidence, campaign.model_dump_json()))
        conn.execute("INSERT INTO pairs(a,b,run_id,total_bits,probability,hypothesis,model,detail)"
                     " VALUES (?,?,?,?,?,?,?,?)", (pair.a, pair.b, NEW_RUN, pair.total_bits, pair.probability,
                                                  pair.hypothesis, pair.model, pair.model_dump_json()))
        for host in ("campaign.example", "newer.example"):
            conn.execute("INSERT INTO enrichment(key,source,query,fetched_at,status,summary)"
                         " VALUES (?,'rdap',?,?,'observed',?)",
                         (f"rdap|{host}", host, stamp, events.canonical_json({"registrar": "Synthetic Registrar"})))
        events.append(conn, "SYNTHETIC_INTERFACE_SEED", {"runs": [OLD_RUN, NEW_RUN]})

    writer = Writer(settings.db_path)
    try:
        writer.run(insert)
    finally:
        writer.close()


def frontend_requests(repo: Path, tmp: Path) -> dict:
    """Execute real TypeScript serializers; intercept fetch before any transport.

    The intercepted requests are subsequently sent through the actual ASGI app
    by the tests. No frontend/API implementation is copied into this helper.
    """
    entry = tmp / "interface-entry.ts"
    entry.write_text(f"""
import {{ campaignExportUrl, campaignAbusePackageUrl, campaignStixUrl, neo4jExportUrl }}
  from {json.dumps(str(repo / "ui/src/api/task.ts"))};
import {{ downloadSubgraph }} from {json.dumps(str(repo / "ui/src/components/graph/api.ts"))};
import {{ runSelection, withRun }} from {json.dumps(str(repo / "ui/src/lib/runSelection.ts"))};
async function main() {{
  runSelection.set({json.dumps(OLD_RUN)});
  let subgraph;
  globalThis.fetch = async (url, init) => {{
    subgraph = {{ url, method: init.method, body: JSON.parse(init.body) }};
    return {{ ok: true, blob: async () => new Blob(["synthetic"]) }};
  }};
  globalThis.document = {{
    createElement: () => ({{ click() {{}}, remove() {{}} }}),
    body: {{ append() {{}} }}
  }};
  await downloadSubgraph(["astra-a", "astra-b"], "json");
  const paths = ["/v1/campaigns", "/v1/graph", "/v1/system/flow", "/v1/review/queue",
    "/v1/genes/explain?kind=operator_id&value=operator-old", "/v1/analytics/indicators"];
  console.log(JSON.stringify({{
    campaign: campaignExportUrl({json.dumps(CAMPAIGN)}, "json"),
    abuse: campaignAbusePackageUrl({json.dumps(CAMPAIGN)}),
    stix: campaignStixUrl({json.dumps(CAMPAIGN)}),
    neo4j: neo4jExportUrl("nodes_csv"), subgraph,
    scoped: Object.fromEntries(paths.map(path => [path, withRun(path)]))
  }}));
}}
main().catch(error => {{ console.error(error); process.exitCode = 1; }});
""", encoding="utf-8")
    loader = tmp / "typescript-resolver.mjs"
    loader.write_text("""
export async function resolve(specifier, context, nextResolve) {
  try { return await nextResolve(specifier, context); }
  catch (error) {
    if (specifier.startsWith('.')) {
      for (const suffix of ['.ts', '.tsx', '/index.ts']) {
        try { return await nextResolve(specifier + suffix, context); } catch {}
      }
    }
    throw error;
  }
}
""", encoding="utf-8")
    executed = subprocess.run(["node", "--experimental-strip-types", "--experimental-loader", str(loader), str(entry)],
                              cwd=repo / "ui",
                              capture_output=True, text=True, timeout=30, check=False)
    assert executed.returncode == 0, executed.stderr
    return json.loads(executed.stdout)
