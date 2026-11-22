import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ArtifactGenes } from "../../api/artifacts";
import type { GeneMatch } from "../../api/task";
import { renderWithProviders } from "../../test/utils";
import { locatedReasons } from "./EvidenceCompare";
import { GenesPanel } from "./GenesPanel";

const data: ArtifactGenes = {
  artifact_id: "a".repeat(64), run_id: "syn-run-1", incidents_in_run: 6, total: 2, truncated: false,
  by_family: { VIS: 1, TXT: 1 }, by_status: { observed: 1, unavailable: 1 },
  genes: [
    { id: 1, family: "VIS", kind: "pdq", value: "syn-1-pdq", quality: 1, status: "observed", provenance: "OBS", extractor: "pdq@1",
      evidence: { artifact_id: "a".repeat(64), bbox: [1, 2, 30, 40] }, level: "asset", tier: null, doc_freq: 1, rarity_bits: 2.58,
      pairs: [{ a: "syn-1", b: "syn-2", run_id: "syn-run-1", bits: 5, probability: 0.99, hypothesis: "same_campaign", explanation_pl: "Ten sam obraz", side: "a", evidence: {}, other_evidence: null }] },
    { id: 2, family: "TXT", kind: "asr_transcript", value: null, quality: 1, status: "unavailable", provenance: "OBS", extractor: "aud_asr@1",
      evidence: { artifact_id: "a".repeat(64), note: "ASR off" }, level: "none", tier: null, doc_freq: null, rarity_bits: null, pairs: [] },
  ],
};

describe("GenesPanel", () => {
  it("lists genes with rarity, unavailable reasons and links to the pairs they fed", async () => {
    const onFocus = vi.fn();
    renderWithProviders(<GenesPanel data={data} onFocus={onFocus} pairHref={(a, b) => `/pair/${a}/${b}`} />);
    expect(screen.getByText("1 · 2,6 b")).toBeInTheDocument();
    expect(screen.getByText("ASR off")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "syn-1 ↔ syn-2" })).toHaveAttribute("href", "/pair/syn-1/syn-2");
    await userEvent.click(screen.getByRole("button", { name: /ramka 1,2,30,40/ }));
    expect(onFocus).toHaveBeenCalledWith(data.genes[0]);
    await userEvent.click(screen.getByLabelText(/tylko użyte w parach/));
    expect(screen.queryByText("asr_transcript")).not.toBeInTheDocument();
  });
});

describe("locatedReasons", () => {
  it("keeps only reasons that point into an artefact, strongest first", () => {
    const r = (kind: string, bits: number, art?: string): GeneMatch => ({ family: "TXT", kind, bits, provenance: "OBS", explanation_pl: kind, evidence_a: art ? { artifact_id: art } : null, evidence_b: null });
    expect(locatedReasons([r("a", 1, "x"), r("b", 5), r("c", -7, "y"), r("a", 1, "x")]).map((m) => m.kind)).toEqual(["c", "a"]);
  });
});
