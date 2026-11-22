import { describe, expect, it } from "vitest";
import { isOsJunk, splitUploads } from "./uploadPolicy";

describe("upload policy (audit A03)", () => {
  it("keeps hidden data files and drops OS junk", () => {
    expect(isOsJunk("61/strona_1/.har")).toBe(false);
    expect(isOsJunk("61/.info.json")).toBe(false);
    expect(isOsJunk("61/.DS_Store")).toBe(true);
    expect(isOsJunk("__MACOSX/61/._ad.png")).toBe(true);
    expect(isOsJunk("61/.git/config")).toBe(true);
    const { keep, junk } = splitUploads([{ path: "a/.har" }, { path: "a/Thumbs.db" }, { path: "a/ad.png" }]);
    expect(keep.map((e) => e.path)).toEqual(["a/.har", "a/ad.png"]);
    expect(junk.map((e) => e.path)).toEqual(["a/Thumbs.db"]);
  });
});
