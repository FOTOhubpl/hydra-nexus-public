import { describe, expect, it } from "vitest";
import type { Gene } from "../api/task";
import { kindOf } from "../api/artifacts";
import { boxMarks, findSpans, focusTermOf, geneTerms, layerOf, mmss, pickKeyframes, videoBboxScale } from "./artifactEvidence";

const g = (p: Partial<Gene>): Gene => ({ family: "TXT", kind: "ocr_line", value: "x", quality: 1, status: "observed", provenance: "OBS", evidence: {}, extractor: "t@1", ...p });

describe("artifact evidence helpers", () => {
  it("classifies preview kinds like the backend", () => {
    expect(kindOf("image/png")).toBe("image");
    expect(kindOf("image/svg+xml")).toBe("text");
    expect(kindOf("application/json", "siec.har", "har")).toBe("har");
    expect(kindOf("multipart/related", "page.mhtml")).toBe("html");
    expect(kindOf("video/mp2t")).toBe("video");
  });

  it("draws one box per bbox and filters by video time", () => {
    const genes = [
      g({ evidence: { bbox: [1, 2, 30, 40] } }),
      g({ kind: "phrase", evidence: { bbox: [1, 2, 30, 40] } }),
      g({ family: "ID", kind: "face", value: null, evidence: { bbox: [5, 5, 10, 10], timecode_s: 12 } }),
      g({ evidence: { bbox: [0, 0, 0, 5] } }),
      g({ evidence: {} }),
    ];
    const still = boxMarks(genes, { timecode: null });
    expect(still).toHaveLength(1);
    expect(still[0]!.genes).toHaveLength(2);
    expect(boxMarks(genes, { timecode: 12.3 }).map((m) => m.layer)).toEqual(["face"]);
    expect(boxMarks(genes, { timecode: 20 })).toHaveLength(0);
    expect(layerOf({ family: "VIS", kind: "logo" })).toBe("logo");
  });

  it("maps OCR boxes of video frames from the 1280 px working frame to source pixels", () => {
    const ocr = g({ evidence: { bbox: [100, 200, 50, 10], timecode_s: 3 } });
    const s = videoBboxScale(ocr, 1170, 2532);
    expect(s).toBeCloseTo(1280 / 2532);
    expect(boxMarks([ocr], { scaleOf: () => s })[0]!.bbox[1]).toBeCloseTo(200 / s);
    expect(videoBboxScale(g({ family: "VIS", evidence: { timecode_s: 3 } }), 1170, 2532)).toBe(1);
    expect(videoBboxScale(g({ evidence: {} }), 1170, 2532)).toBe(1);
  });

  it("highlights URLs, phones, trackers and gene values without overlaps", () => {
    const text = 'Zadzwoń +48 600 100 200 lub https://x.example/a?b=1 fbq("init", "1071875908635809")';
    const spans = findSpans(text, geneTerms([g({ kind: "tracker_id", family: "INF", value: "fb_pixel:1071875908635809" })]));
    const kinds = spans.map((s) => s.kind);
    expect(kinds).toContain("phone");
    expect(kinds).toContain("url");
    expect(kinds).toContain("gene");
    for (let i = 1; i < spans.length; i++) expect(spans[i]!.start).toBeGreaterThanOrEqual(spans[i - 1]!.end);
    const f = findSpans(text, [], "x.example");
    expect(f.find((s) => s.kind === "focus")).toBeTruthy();
  });

  it("picks evenly spread keyframes and formats time", () => {
    expect(pickKeyframes([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], 10, 4)).toEqual([0, 3, 6, 9]);
    expect(pickKeyframes([], 40, 4)).toEqual([5, 15, 25, 35]);
    expect(pickKeyframes([], null, 4)).toEqual([]);
    expect(mmss(75.4)).toBe("1:15,4");
  });

  it("uses the raw form of entities as the focus term", () => {
    expect(focusTermOf(g({ kind: "entity_phone", value: "+48600100200", evidence: { note: "600 100 200 (placeholder)" } }))).toBe("600 100 200");
    expect(focusTermOf(g({ kind: "tracker_id", value: "ga4:G-ABC123" }))).toBe("G-ABC123");
  });
});
