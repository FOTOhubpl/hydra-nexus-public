import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AssignmentMeta } from "../../api/task";
import { DecisionRule } from "../../components/DecisionRule";
import { GeneMatchTable, ProbBar, Waterfall } from "../../components/evidence";
import { BottomStatusBar, StatusBar } from "../../components/StatusBar";
import { formatBytes } from "../../lib/format";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { fmt } from "./MethodologyPage";
import { StageRail } from "./StageRail";

// shape of hydra/assign.py explain() output; values are illustrative fixtures, not results
const conformal = {
  method: "split_conformal@1", eps: 0.1, n_calibration: 5, decided_by: "threshold", usable: false, set_size: 1,
  prediction_set: ["C-a"], p_values: { "C-a": 0.6667, "C-b": 0.1667 }, excluded_by_evidence_gate: ["C-c"],
  note_pl: "Przy n=5 < 20 decyzję podejmuje próg bitów.",
};
const metaAboveThresholdRuleNotMet: AssignmentMeta = {
  top_candidates: [{ campaign_id: "C-a", support: 12.4, operator_bits: 2, campaign_by_family: { TXT: 8, VIS: 4.4 } }],
  margin_bits: 3.5, missing_bits_to_link: 0, link_threshold: 10, link_rule_met: false, conformal,
};

describe("decision rule (Assignment.meta)", () => {
  it("shows support vs threshold, missing bits, margin and the unmet strong-evidence rule", () => {
    render(<DecisionRule meta={metaAboveThresholdRuleNotMet} />);
    const box = screen.getByTestId("decision-rule");
    expect(within(box).getByText("12,4 b")).toBeInTheDocument();
    expect(within(box).getByText("10,0 b")).toBeInTheDocument();
    expect(within(box).getByText("próg osiągnięty")).toBeInTheDocument();
    expect(within(box).getByText("0 b (próg osiągnięty)")).toBeInTheDocument();
    expect(within(box).getByText("niespełniony")).toBeInTheDocument();
    expect(within(box).getByText("+3,5 b")).toBeInTheDocument();
    expect(within(box).getByText(/Wsparcie osiąga próg, ale brakuje silnego dowodu/)).toBeInTheDocument();
    expect(within(box).getByText("przesłanki łączenia niespełnione")).toBeInTheDocument();
  });

  it("shows missing bits below the threshold and no contradiction note", () => {
    render(<DecisionRule meta={{ ...metaAboveThresholdRuleNotMet, top_candidates: [{ campaign_id: "C-a", support: 3 }], missing_bits_to_link: 7 }} />);
    expect(screen.getByText("poniżej progu")).toBeInTheDocument();
    expect(screen.getByText("7,0 b")).toBeInTheDocument();
    expect(screen.queryByText(/Wsparcie osiąga próg/)).toBeNull();
  });

  it("renders the conformal prediction set as a list, not JSON", () => {
    render(<DecisionRule meta={metaAboveThresholdRuleNotMet} />);
    const set = screen.getByTestId("conformal-set");
    const list = within(set).getByRole("list", { name: "Kampanie w zbiorze predykcji" });
    expect(within(list).getByText("C-a")).toBeInTheDocument();
    expect(within(list).getByText("p-wartość konformalna (nie prawdopodobieństwo kampanii) = 0,67")).toBeInTheDocument();
    expect(within(set).getByText(/zbiór tylko raportowany/)).toBeInTheDocument();
    expect(set.textContent).not.toMatch(/[{}"]/);
  });

  it("renders nothing for runs without rule fields", () => {
    const { container } = render(<DecisionRule meta={{}} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("honest labels", () => {
  it("labels an uncalibrated score as a score, not a probability", () => {
    render(<ProbBar p={0.9} calibrated={false} />);
    expect(screen.getByText("wynik nieskalibrowany (nie prawdopodobieństwo)")).toBeInTheDocument();
  });

  it("names the cap correction row in Polish in the waterfall and the reasons table", () => {
    const cap = { family: "INF", kind: "cap_correction", value_a: null, value_b: null, bits: -4, provenance: "INF", explanation_pl: "Limit rodziny" };
    const reason = { family: "INF", kind: "domain", value_a: "a.example", value_b: "a.example", bits: 12, provenance: "OBS", explanation_pl: "Ta sama domena" };
    const { container } = render(<Waterfall familyBits={{ INF: 12 }} totalBits={8} missing={[]} reasons={[reason, cap] as never} contradictions={[]} />);
    expect(screen.getByText("korekta limitu rodziny")).toBeInTheDocument();
    expect(container.textContent).not.toContain("cap_correction");
    render(<GeneMatchTable rows={[cap] as never} />);
    expect(screen.getAllByText("korekta limitu rodziny").length).toBeGreaterThanOrEqual(2);
  });
});

describe("Polish number formatting", () => {
  it("uses a decimal comma consistently for rates and pl-PL grouping for counts", () => {
    expect(fmt(1, true)).toBe("1,000");
    expect(fmt(0.946, true)).toBe("0,946");
    expect(fmt(0.5)).toBe("0,500");
    expect(fmt(12345)).toMatch(/^12\s345$/);
    expect(formatBytes(1536)).toBe("1,5 KB");
  });
});

describe("phone layout (390 px)", () => {
  it("simulation stage tabs scroll horizontally below md instead of squeezing 8 columns", () => {
    render(<StageRail stage={2} u={0.5} go={() => undefined} />);
    const rail = screen.getByTestId("stage-rail");
    expect(rail.className).toContain("overflow-x-auto");
    expect(rail.className).toContain("md:grid-cols-8");
    expect(rail.className).not.toMatch(/(^|\s)grid-cols-8/);
    for (const li of rail.querySelectorAll("li")) expect(li.className).toContain("shrink-0");
    expect(screen.getByRole("button", { current: "step" })).toHaveTextContent("Rzadkość");
  });

  it("header search collapses to an icon below sm and keeps its accessible name", () => {
    mockFetch(async () => jsonResponse({ items: [] }));
    renderWithProviders(<StatusBar title="Jak to działa" />);
    const search = screen.getByRole("button", { name: "Szukaj (⌘K)" });
    const text = within(search).getByText(/Szukaj incydentów/);
    expect(text.className).toMatch(/(^|\s)hidden(\s|$)/);
    expect(text.className).toContain("sm:inline");
  });

  it("bottom status bar hides mode/run details below sm and does not wrap", async () => {
    mockFetch(async () => jsonResponse({ active_jobs: 0, offline: true, enrichment_mode: "replay", run_id: "analyse-x", contracts_version: "2.2.0", audit: { ok: true, events: 3, first_bad_seq: null }, reviews: 0 }));
    renderWithProviders(<BottomStatusBar />);
    const details = await screen.findByTestId("status-details");
    expect(details.className).toMatch(/(^|\s)hidden(\s|$)/);
    expect(details.className).toContain("sm:inline-flex");
    expect(screen.getByTestId("bottom-status").className).toContain("whitespace-nowrap");
    expect(screen.getByText("łańcuch poprawny")).toBeInTheDocument();
  });
});
