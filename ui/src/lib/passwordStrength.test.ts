import { describe, expect, it } from "vitest";
import { estimateStrength, STRENGTH_LABELS } from "./passwordStrength";

describe("password strength estimate", () => {
  it("uses the five Polish labels", () => {
    expect(STRENGTH_LABELS).toEqual(["Bardzo słabe", "Słabe", "Średnie", "Silne", "Bardzo silne"]);
  });

  it("short passwords are very weak and fail the 12-character policy", () => {
    const s = estimateStrength("Ab1!");
    expect(s.score).toBe(0);
    expect(s.label).toBe("Bardzo słabe");
    expect(s.meetsPolicy).toBe(false);
    expect(s.hints.join(" ")).toContain("12");
  });

  it("common words and sequences are penalised", () => {
    expect(estimateStrength("password1234").score).toBeLessThanOrEqual(1);
    expect(estimateStrength("qwertyuiop12").score).toBeLessThanOrEqual(1);
  });

  it("containing the username caps the score and fails the policy", () => {
    const s = estimateStrength("Anna.Kowalska-2026!x", "anna.kowalska");
    expect(s.score).toBeLessThanOrEqual(1);
    expect(s.meetsPolicy).toBe(false);
    expect(s.hints).toContain("Hasło nie może zawierać nazwy użytkownika.");
  });

  it("long mixed passwords are strong; longer ones very strong", () => {
    const strong = estimateStrength("Kz7#pQ2!vL9@xR4m");
    expect(strong.score).toBeGreaterThanOrEqual(3);
    expect(strong.meetsPolicy).toBe(true);
    expect(estimateStrength("Mglisty-Poranek_nad-Wisla#2026").score).toBe(4);
  });

  it("score grows monotonically with length for the same pattern", () => {
    const scores = ["Rz#8wq", "Rz#8wqTm4!", "Rz#8wqTm4!Lp", "Rz#8wqTm4!LpVb7%", "Rz#8wqTm4!LpVb7%Kd2&"].map((p) => estimateStrength(p).score);
    for (let i = 1; i < scores.length; i++) expect(scores[i]).toBeGreaterThanOrEqual(scores[i - 1]!);
  });
});
