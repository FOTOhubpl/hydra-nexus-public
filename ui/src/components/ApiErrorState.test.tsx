import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { ApiErrorState } from "./ApiErrorState";

describe("ApiErrorState (CR-ASTRA-10: Polish strings)", () => {
  it("offline: says the local server is unreachable and keeps the address", () => {
    render(<ApiErrorState error={new ApiError({ kind: "network", message: "Failed to fetch", path: "/v1/campaigns" })} resource="kampanie" />);
    expect(screen.getByText("Serwer lokalny niedostępny")).toBeInTheDocument();
    expect(screen.getByText(/Nie można wczytać: kampanie\..*127\.0\.0\.1:8000.*ponawia próbę automatycznie/)).toBeInTheDocument();
    expect(screen.queryByText(/Backend offline|Cannot load/)).toBeNull();
  });

  it("not implemented: Polish title and description, endpoint as detail", () => {
    render(<ApiErrorState error={new ApiError({ kind: "http", status: 404, message: "Not Found", path: "/v1/x" })} resource="podobne przypadki" endpoint="/v1/incidents/i1/similar" />);
    expect(screen.getByText("Punkt końcowy API jeszcze niedostępny")).toBeInTheDocument();
    expect(screen.getByText("Serwer nie udostępnia jeszcze: podobne przypadki.")).toBeInTheDocument();
    expect(screen.getByText("/v1/incidents/i1/similar")).toBeInTheDocument();
    expect(screen.queryByText(/not implemented|does not serve/)).toBeNull();
  });

  it("other errors: Polish title, raw server message and HTTP detail kept", () => {
    render(<ApiErrorState error={new ApiError({ kind: "http", status: 500, message: "database is locked", path: "/v1/review/queue" })} resource="kolejkę" />);
    expect(screen.getByText("Nie udało się wczytać: kolejkę")).toBeInTheDocument();
    expect(screen.getByText("database is locked")).toBeInTheDocument();
    expect(screen.getByText("HTTP 500 · /v1/review/queue")).toBeInTheDocument();
    expect(screen.queryByText(/Could not load/)).toBeNull();
  });
});
