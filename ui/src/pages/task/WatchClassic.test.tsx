import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { WatchClassic } from "./WatchClassic";

const status = {
  mode: "replay", replay: { file: "certpl-2026-11-21.jsonl", speed: 60, sha256: "a".repeat(64), events: 2, emitted: 2, done: true, window: ["2026-11-21T14:00:00+00:00", "2026-11-21T14:05:00+00:00"] },
  clock: "2026-11-21T14:05:00+00:00", events: 2, high: 1, share_high: 0.5, a_cut_bits: 6, in_live_campaign: 1, assigned_known: 1, family_link_known: 0,
  campaign_gene_hits: 1, enriched: 1, pending: 0, live_campaigns: 1, live_enrichment: false, corpus: "nask", seeded_history: 4,
  campaign_index: { campaigns: 3, run_id: "r", error: null }, top_families: [{ family: "oferta-#.sbs", domains: 5 }],
  top_live_campaigns: [{ id: "W-abc123", domains: 2, hosts: 3, sample: ["oferta-1.sbs"], keys: ["oferta-#.sbs"] }],
  sparkline: [{ minute: "2026-11-21T14:04", n: 1 }, { minute: "2026-11-21T14:05", n: 1 }],
};
const ev = (seq: number, host: string, decision: string, campaign: string | null) => ({
  seq, event_id: `w-${seq}`, source: "watch:certpl", host_defanged: host, first_seen: "2026-11-21T14:05:00+00:00", replay: true,
  live_campaign: seq === 1 ? "W-abc123" : null,
  score: { bits: 8, high: true, family: "oferta-#.sbs", registrable: "oferta-1.sbs", campaign_hints: [], brands: ["Allegro"],
    reasons: [{ code: "brand_subdomain", bits: 4, label_pl: "Marka w subdomenie obcej domeny", detail: "Allegro", campaign_id: null }] },
  assignment: { decision, campaign_id: campaign, campaign_label: campaign ? "Kampania testowa" : null, reason_pl: "r" },
  enrichment: null,
});

describe("WatchClassic (Odkrywanie domen)", () => {
  it("shows the REPLAY badge, defanged hosts, reasons and the assignment verdicts", async () => {
    mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/v1/watch/status")) return jsonResponse(status);
      if (url.includes("/v1/watch/recordings")) return jsonResponse({ recordings: [{ file: "certpl-2026-11-21.jsonl", lines: 2, bytes: 10 }] });
      return jsonResponse({ replay: true, events: [ev(1, "allegro[.]oferta-1[.]sbs", "new_campaign", null), ev(2, "olx[.]oferta-2[.]sbs", "assign", "C-1")] });
    });
    renderWithProviders(<WatchClassic />);
    expect(await screen.findByText("REPLAY 60×")).toBeInTheDocument();
    expect(await screen.findByText("allegro[.]oferta-1[.]sbs")).toBeInTheDocument();
    expect(screen.getAllByText(/Marka w subdomenie obcej domeny/).length).toBe(2);
    expect(screen.getByText("nowa kampania")).toBeInTheDocument();
    expect(screen.getByText(/pasuje do/)).toBeInTheDocument();
    expect(screen.getAllByText("W-abc123").length).toBeGreaterThan(0);
    expect(document.querySelector("a[href*='oferta']")).toBeNull(); // hosts are never links
  });

  it("alerts tab labels every alert as a candidate and shows matched clauses", async () => {
    mockFetch(async (input) => {
      const url = String(input);
      if (url.includes("/v1/watch/status")) return jsonResponse({ ...status, alerts: 1 });
      if (url.includes("/v1/watch/recordings")) return jsonResponse({ recordings: [] });
      if (url.includes("/v1/watch/alerts")) {
        return jsonResponse({
          fingerprints: { fingerprints: 3, sha256: "b".repeat(64), created_at: "2026-11-21T15:00:00+00:00", corpus: "nask" },
          note_pl: "", lead_time: [{ registrable: "x-1.sbs", watch_first_seen: "2026-11-21T10:00:00+00:00", watch_source: "watch:ct", cert_listed: "2026-11-21T12:00:00+00:00", lead_h: 2 }],
          alerts: [{ alert_seq: 1, event_id: "w-1", source: "watch:certpl", host_defanged: "olx[.]oferta-9[.]sbs", first_seen: "2026-11-21T14:05:00+00:00", replay: true, event_bits: 8, assignment: "new_campaign",
            alerts: [{ campaign_id: "C-1", campaign_label: "Testowa", group_id: null, level: "campaign", confidence: "średnia", bits: 7.5, campaign_bits: 6, status: "kandydat",
              matched: [{ kind: "family", value: "oferta-#.sbs", bits: 6, level: "campaign", label_pl: "ta sama rodzina nazw" }] }] }],
        });
      }
      return jsonResponse({ replay: true, events: [] });
    });
    renderWithProviders(<WatchClassic />);
    fireEvent.click(await screen.findByRole("tab", { name: /Alerty/ }));
    expect(await screen.findByText("olx[.]oferta-9[.]sbs")).toBeInTheDocument();
    expect(screen.getByText(/kandydat · kampania · średnia/)).toBeInTheDocument();
    expect(screen.getByText(/ta sama rodzina nazw/)).toBeInTheDocument();
    expect(screen.getByText(/nie jest potwierdzony członek/)).toBeInTheDocument();
    expect(screen.getByText("+2 h")).toBeInTheDocument();
  });
});
