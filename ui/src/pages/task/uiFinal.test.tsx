import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import type { AssignmentMeta, PairScore } from "../../api/task";
import { ConformalList, DecisionRule } from "../../components/DecisionRule";
import { LabLink, topPair } from "../../components/LabLink";
import { Probability, ProbBar } from "../../components/evidence";
import { pointerText } from "../../lib/artifactEvidence";
import { isWholeFile, polishReason } from "../../lib/displayPl";
import { MetricsTable } from "./HuntPage";

// shapes of hydra/assign.py explain() and hydra/retrieval.py query_scope(); values are test fixtures, not results
const scope = {
  corpus_incidents: 400, mode: "indexed", truncated: true, bounded_by: ["postings_over_max_df_dropped", "knn_top_k"],
  holder_slots_not_reached: 37, postings_dropped: { domain: 2, phash: 1 }, postings_backstop_capped: { tracker_id: 1 }, postings_stop: 4,
};
const meta: AssignmentMeta = {
  top_candidates: [{ campaign_id: "C-a", support: 12, operator_bits: 9, campaign_by_family: { INF: 9, TXT: 3 } }],
  margin_bits: 2, missing_bits_to_link: 0, link_threshold: 10, link_rule_met: true, retrieval_scope: scope,
};
const pair = {
  a: "i1", b: "i2", family_bits: { INF: 9 }, missing_families: ["VIS", "NAR"], contradictions: [
    { family: "TXT", kind: "scheme_mismatch", bits: -3, provenance: "INF", explanation_pl: "Różny schemat oszustwa (TXT)" },
  ], reasons: [], total_bits: 6, probability: 0.4, calibrated: false, hypothesis: "abstain", model: "m",
} as unknown as PairScore;

describe("UI-FINAL: decision card", () => {
  it("renders the retrieval scope in Polish with a note when truncated", () => {
    render(<DecisionRule meta={meta} />);
    const box = screen.getByTestId("retrieval-scope");
    expect(within(box).getByText("Zakres wyszukiwania")).toBeInTheDocument();
    expect(within(box).getByText("indeksy rzadkich cech")).toBeInTheDocument();
    expect(within(box).getByText("tak")).toBeInTheDocument();
    expect(within(box).getByText("pominięte zbyt częste wartości, limit najbliższych sąsiadów (k-NN)")).toBeInTheDocument();
    expect(within(box).getByText("37")).toBeInTheDocument();
    expect(within(box).getByText("3")).toBeInTheDocument(); // 2 + 1 dropped postings
    expect(within(box).getByTestId("retrieval-scope-truncated")).toHaveTextContent("lista kandydatów jest próbką");
  });

  it("shows no truncation note when the scope is complete, and nothing when the scope is absent", () => {
    const { rerender } = render(<DecisionRule meta={{ ...meta, retrieval_scope: { mode: "all_pairs", truncated: false, bounded_by: [] } }} />);
    expect(screen.getByText("nie")).toBeInTheDocument();
    expect(screen.queryByTestId("retrieval-scope-truncated")).toBeNull();
    rerender(<DecisionRule meta={{ ...meta, retrieval_scope: null }} />);
    expect(screen.queryByTestId("retrieval-scope")).toBeNull();
  });

  it("renders meta.no_features as a held decision even without rule fields", () => {
    render(<DecisionRule meta={{ no_features: true }} />);
    expect(screen.getByTestId("no-features")).toHaveTextContent("brak cech — decyzja wstrzymana");
  });

  it("shows contradictions and missing families of the best pair with Polish family labels", () => {
    render(<DecisionRule meta={meta} bestPair={pair} />);
    const c = screen.getByTestId("assignment-caveats");
    expect(within(c).getByText("Sprzeczności najlepszej pary (1)")).toBeInTheDocument();
    expect(c).toHaveTextContent("Różny schemat oszustwa (tekst)");
    expect(c).toHaveTextContent("obraz, narracja");
    expect(c).not.toHaveTextContent("TXT");
  });

  it("topPair reads the top candidate's best pair; LabLink points at /lab/:incident", () => {
    expect(topPair({ candidates: [{ campaign_id: "C-a", probability: 0.5, support: 1, best_pair: pair }] })).toBe(pair);
    expect(topPair({ candidates: [] })).toBeNull();
    render(<MemoryRouter><LabLink incidentId="inc 1" /></MemoryRouter>);
    expect(screen.getByTestId("lab-link")).toHaveAttribute("href", "/lab/inc%201");
  });
});

describe("UI-FINAL: labels", () => {
  it("labels conformal p-values and the jackknife coverage", () => {
    render(<ConformalList c={{ prediction_set: ["C-a"], p_values: { "C-a": 0.5, "C-b": 0.1 }, empirical_coverage: 0.9 }} />);
    expect(screen.getByText("p-wartość konformalna (nie prawdopodobieństwo kampanii) = 0,50")).toBeInTheDocument();
    expect(screen.getByText("p-wartość konformalna (nie prawdopodobieństwo kampanii) = 0,10")).toBeInTheDocument();
    expect(screen.getByTestId("conformal-coverage")).toHaveTextContent("pokrycie zbioru surowego (jackknife), nie polityki końcowej: 0,90");
  });

  it("labels uncalibrated probability values as an uncalibrated score", () => {
    render(<><ProbBar p={0.7} calibrated={false} /><Probability p={0.7} calibrated={false} /></>);
    expect(screen.getByText("wynik nieskalibrowany (nie prawdopodobieństwo)")).toBeInTheDocument();
    expect(screen.getByLabelText("wynik nieskalibrowany 0,700")).toBeInTheDocument();
  });
});

describe("UI-FINAL: English leaks in reason text", () => {
  it("maps family codes and the bridge reason, leaving gene keys alone", () => {
    expect(polishReason("tylko z jednej rodziny cech (TXT) i bez")).toBe("tylko z jednej rodziny cech (tekst) i bez");
    expect(polishReason("z rodzin cech INF, VIS, ale")).toBe("z rodzin cech infrastruktura, obraz, ale");
    expect(polishReason("decyzją „nie może być połączone” (bridges cannot-link). Przypisanie"))
      .toBe("decyzją „nie może być połączone” (łączy kampanie rozdzielone decyzją analityka). Przypisanie");
    expect(polishReason("klucz INF:operator:domain")).toBe("klucz INF:operator:domain");
    expect(polishReason(null)).toBe("");
  });
});

describe("UI-FINAL: whole-file locators", () => {
  it("detects timecode 0 with a full-frame bbox, or whole-file kinds without a bbox", () => {
    expect(isWholeFile({ timecode_s: 0, bbox: [0, 0, 1920, 1080] })).toBe(true);
    expect(isWholeFile({ timecode_s: 0, bbox: [0, 0, 1920, 1080] }, null, { w: 1920, h: 1080 })).toBe(true);
    expect(isWholeFile({ timecode_s: 0, bbox: [0, 0, 640, 360] }, null, { w: 1920, h: 1080 })).toBe(false);
    expect(isWholeFile({ timecode_s: 0, bbox: [10, 20, 100, 50] })).toBe(false);
    expect(isWholeFile({ timecode_s: 3.5, bbox: [0, 0, 1920, 1080] })).toBe(false);
    expect(isWholeFile({ timecode_s: 0 }, "audio_fp_sig")).toBe(true);
    expect(isWholeFile({ timecode_s: 0 }, "av_sync_suspicion")).toBe(true);
    expect(isWholeFile({ timecode_s: 0 }, "deepfake_segment")).toBe(false);
  });

  it("pointerText shows 'cały plik' instead of a frame/time locator", () => {
    expect(pointerText({ timecode_s: 0, bbox: [0, 0, 512, 512] })).toBe("cały plik");
    expect(pointerText({ timecode_s: 0 }, "audio_fp_band")).toBe("cały plik");
    expect(pointerText({ timecode_s: 0, bbox: [5, 5, 10, 10] })).toBe("ramka 5,5,10,10 · t=0:00,0");
  });
});

describe("UI-FINAL: hunt metrics header", () => {
  it("names the counterfactual lead-time column honestly", () => {
    render(<MetricsTable rows={[]} />);
    expect(screen.getByText(/mediana wyprzedzenia wobec listy CERT w odtworzeniu kontrfaktycznym \(założony czas dostępności \+5 min\)/)).toBeInTheDocument();
  });
});
