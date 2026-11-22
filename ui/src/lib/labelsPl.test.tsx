import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { KindLabel } from "../components/evidence";
import { polishReason } from "./displayPl";
import { reasonKindLabel } from "./evidence";
import { geneKindPl, polishCodes, schemePl, schemesPl } from "./labelsPl";

describe("B9: Polish labels for gene kinds and schemes", () => {
  it("maps the codes seen in the audit", () => {
    for (const k of ["entity_phone", "tracker_id", "landing_host", "minhash_band", "pdq_canon", "page_title", "text_template_key", "entity_price", "phash_multicrop"]) {
      expect(geneKindPl(k)).not.toBe(k);
    }
    expect(geneKindPl("entity_phone")).toBe("numer telefonu");
    expect(reasonKindLabel("cap_correction")).toBe("korekta limitu rodziny");
    expect(geneKindPl("brand_new_kind")).toBe("brand_new_kind");
  });

  it("maps scheme ids, also in a list", () => {
    expect(schemePl("investment")).toBe("fałszywe inwestycje");
    expect(schemePl("parcel_fee")).toBe("dopłata do przesyłki");
    expect(schemesPl("gov_refund, investment")).toBe("zwrot podatku lub świadczenia, fałszywe inwestycje");
  });

  it("replaces snake_case codes in sentences but not gene keys or plain words", () => {
    expect(polishCodes("wspólny entity_phone i pdq_canon")).toBe("wspólny numer telefonu i odcisk obrazu (PDQ, kanoniczny)");
    expect(polishCodes("INF:tracker_id=abc logo")).toBe("INF:tracker_id=abc logo");
    expect(polishReason("wspólny tracker_id (TXT)")).toContain("identyfikator śledzący");
  });

  it("KindLabel shows Polish text and keeps the raw code in the tooltip", () => {
    render(<KindLabel kind="minhash_band" />);
    const el = screen.getByText("podobieństwo tekstu (MinHash)");
    expect(el).toHaveAttribute("title", "kod: minhash_band");
    expect(el).toHaveAttribute("data-kind", "minhash_band");
  });
});
