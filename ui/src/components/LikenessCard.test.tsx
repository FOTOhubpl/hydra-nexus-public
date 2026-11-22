import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { LikenessCard as Card } from "../api/likeness";
import { jsonResponse, mockFetch, renderWithProviders } from "../test/utils";
import { LikenessCard } from "./LikenessCard";

// Hand-written synthetic fixture (never organiser data).
const CARD: Card = {
  incident_id: "syn-1",
  run_id: "syn-run-1",
  title_pl: "Nadużycie wizerunku — karta triażu analityka",
  disclaimer_pl: "Karta triażu: to nie jest wykrywanie deepfake ani reguła łączenia kampanii.",
  triggered: true,
  present: 3,
  total: 5,
  strength: "umiarkowane",
  signals: [
    {
      key: "public_figure", label_pl: "Osoba publiczna", state: "present", summary_pl: "Wymieniona osoba publiczna",
      hint_pl: null, checks: [],
      items: [{ family: "TXT", kind: "person_named", value: "Jan Przykładowy", quality: 0.95, provenance: "OBS",
        label_pl: null, other_incident: null, evidence: { artifact_id: "abcdef0123456789", char_range: [3, 18] } }],
    },
    {
      key: "financial_promise", label_pl: "Obietnica finansowa", state: "present", summary_pl: "Obietnica zysku",
      hint_pl: null, checks: [], items: [],
    },
    {
      key: "media_brand", label_pl: "Podszycie pod markę medialną", state: "absent", summary_pl: "Brak marki",
      hint_pl: null, checks: [], items: [],
    },
    {
      key: "reused_media", label_pl: "Ponownie użyty obraz", state: "present", summary_pl: "Ten sam obraz w innym incydencie",
      hint_pl: null, checks: [],
      items: [{ family: "VIS", kind: "pdq", value: null, quality: null, provenance: "INF", label_pl: "Bliski duplikat",
        other_incident: "syn-2", evidence: {} }],
    },
    {
      key: "ai_generator", label_pl: "Sygnał generatora AI", state: "unknown", summary_pl: "Nie dało się sprawdzić",
      hint_pl: "porównanie z oryginalnym nagraniem", items: [],
      checks: [
        { key: "c2pa", label_pl: "Manifest C2PA", state: "absent", summary_pl: "nie wykryto" },
        { key: "watermark", label_pl: "Znak wodny generatora", state: "unknown", summary_pl: "brak obrazów" },
      ],
    },
  ],
  limits_pl: ["Zrzuty ekranu usuwają metadane."],
};

afterEach(() => vi.unstubAllGlobals());

describe("LikenessCard", () => {
  it("renders signals, states, strength and disclaimer", async () => {
    mockFetch(async () => jsonResponse(CARD));
    renderWithProviders(<LikenessCard incidentId="syn-1" runId="syn-run-1" />);
    expect(await screen.findByText(/to nie jest wykrywanie deepfake/)).toBeInTheDocument();
    expect(screen.getByText("umiarkowane")).toBeInTheDocument();
    expect(screen.getByText("(3/5 sygnałów obecnych)")).toBeInTheDocument();
    expect(screen.getAllByText("obecny")).toHaveLength(3);
    expect(screen.getAllByText("nieobecny")).toHaveLength(2);
    expect(screen.getByText(/brak znaku nie świadczy o autentyczności/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "syn-2" })).toHaveAttribute("href", "/incident/syn-2");
    expect(screen.getByText("Jan Przykładowy")).toBeInTheDocument();
  });

  it("shows AudioSeal as a not-counted tool mark and the block as unknown, never authentic", async () => {
    const ai = {
      key: "ai_generator", label_pl: "Sygnał generatora AI", state: "unknown" as const, checked: true,
      state_label_pl: "nieznane", summary_pl: "Pochodzenie nieznane: tylko obserwacje niewliczane do siły",
      hint_pl: null, items: [],
      checks: [
        { key: "audio_watermark", label_pl: "Znak wodny AudioSeal", state: "present" as const, role: "tool_mark" as const,
          counted: false, state_label_pl: "wykryto", summary_pl: "wykryto znak AudioSeal (zakres znaku)" },
      ],
    };
    mockFetch(async () => jsonResponse({ ...CARD, present: 0, strength: "brak", signals: [ai] }));
    renderWithProviders(<LikenessCard incidentId="syn-1" />);
    expect(await screen.findByText("nieznane")).toBeInTheDocument();
    expect(screen.getByText("wykryto")).toBeInTheDocument();
    expect(screen.getByText(/znak narzędzia, nie rodzaj treści; niewliczane do siły/)).toBeInTheDocument();
    expect(screen.queryByText(/autentyczn(e|y)\b/)).not.toBeInTheDocument();
  });

  it("degrades to a small note on 404", async () => {
    mockFetch(async () => jsonResponse({ detail: "not found" }, { status: 404 }));
    renderWithProviders(<LikenessCard incidentId="nope" />);
    await waitFor(() => expect(screen.getByText(/Karta niedostępna/)).toBeInTheDocument());
  });
});
