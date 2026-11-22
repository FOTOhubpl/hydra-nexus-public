import { describe, expect, it } from "vitest";
import { sharedRatio, wordDiff } from "./wordDiff";

describe("wordDiff", () => {
  it("marks shared and changed words", () => {
    const ops = wordDiff("Zarabiaj 5000 zł miesięcznie z Orlenem", "Zarabiaj 7000 zł miesięcznie z PGE");
    expect(ops).toEqual([
      { op: "same", text: "Zarabiaj" }, { op: "del", text: "5000" }, { op: "add", text: "7000" },
      { op: "same", text: "zł miesięcznie z" }, { op: "del", text: "Orlenem" }, { op: "add", text: "PGE" },
    ]);
    expect(sharedRatio(ops)).toBeCloseTo(4 / 6);
  });
});
