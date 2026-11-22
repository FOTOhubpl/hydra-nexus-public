import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mmss, VideoSegmentStrip, type VideoSegment } from "./VideoSegmentStrip";

const seg: VideoSegment = {
  artifact_a: "a", artifact_b: "b", start_a: 4, end_a: 14, start_b: 6, end_b: 16, offset_s: 2, frames: 7, footage_df: 2,
  length_a: 18, length_b: 20, bits: 12, explanation_pl: "Wspólny fragment wideo",
};

describe("VideoSegmentStrip", () => {
  it("shows both time ranges and the offset", () => {
    render(<VideoSegmentStrip segment={seg} a="nask-1" b="nask-2" />);
    expect(screen.getByRole("img", { name: /nask-1: wspólny fragment 0:04–0:14 z 0:18/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /nask-2: wspólny fragment 0:06–0:16 z 0:20/ })).toBeInTheDocument();
    expect(screen.getByText(/przesunięcie \+2/)).toBeInTheDocument();
    expect(screen.getByText("7 dopasowanych klatek")).toBeInTheDocument();
  });

  it("marks common stock / TV footage as not counting", () => {
    render(<VideoSegmentStrip segment={{ ...seg, bits: 0, footage_df: 5 }} a="x" b="y" />);
    expect(screen.getByText(/nie liczy się do powiązania/)).toBeInTheDocument();
  });

  it("formats minutes and seconds", () => {
    expect(mmss(75.4)).toBe("1:15");
    expect(mmss(-3)).toBe("0:00");
  });
});
