import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Thumb } from "../components/evidence";
import { THUMB_FAILURE_TTL_MS, markThumbFailed, resetThumbFailures, thumbFailed } from "./thumbFailures";

const ARTIFACT = "a".repeat(64);

beforeEach(() => {
  window.localStorage.clear();
  resetThumbFailures();
});
afterEach(cleanup);

describe("B12: a failing thumbnail is not re-requested on every navigation within a session", () => {
  it("a failed thumbnail is remembered across mounts in the same session, with a neutral label", () => {
    const { unmount } = render(<MemoryRouter><Thumb artifactId="sha-bad" alt="x.jpg" size={104} /></MemoryRouter>);
    const img = screen.getByRole("img", { name: "x.jpg" });
    expect(img.getAttribute("src")).toContain("/v1/artifacts/sha-bad/thumb");
    fireEvent.error(img);
    expect(screen.getByTestId("thumb-broken")).toHaveTextContent("nie udało się wczytać podglądu");
    unmount();
    render(<MemoryRouter><Thumb artifactId="sha-bad" alt="x.jpg" size={104} /></MemoryRouter>);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByTestId("thumb-broken")).toBeInTheDocument();
  });

  it("healthy artifacts are unaffected and the memory is per id", () => {
    markThumbFailed("a");
    expect(thumbFailed("a")).toBe(true);
    expect(thumbFailed("b")).toBe(false);
    render(<MemoryRouter><Thumb artifactId="b" alt="ok" /></MemoryRouter>);
    expect(screen.getByRole("img", { name: "ok" })).toBeInTheDocument();
  });
});

describe("CR-ASTRA-RT-01: a failed load is retried and never persisted", () => {
  it("a failure expires after the TTL", () => {
    markThumbFailed("a", 1_000);
    expect(thumbFailed("a", 1_000 + THUMB_FAILURE_TTL_MS - 1)).toBe(true);
    expect(thumbFailed("a", 1_000 + THUMB_FAILURE_TTL_MS)).toBe(false);
  });

  it("nothing is written to localStorage", () => {
    markThumbFailed("a");
    expect(window.localStorage.length).toBe(0);
  });

  it("clicking the placeholder retries the image", () => {
    render(<MemoryRouter><Thumb artifactId={ARTIFACT} alt="retry me" size={104} /></MemoryRouter>);
    fireEvent.error(screen.getByRole("img", { name: "retry me" }));
    fireEvent.click(screen.getByTestId("thumb-broken"));
    expect(screen.getByRole("img", { name: "retry me" })).toBeInTheDocument();
  });

  // The three probes below are Astra's independent reproducer (qa/astra-retest-8b4b35f-evidence/astraRetest.test.tsx).
  it("does not classify an unqualified image-load error as confirmed corruption", () => {
    render(<MemoryRouter><Thumb artifactId={ARTIFACT} alt="synthetic valid image" size={104} /></MemoryRouter>);
    fireEvent.error(screen.getByRole("img", { name: "synthetic valid image" }));
    expect(screen.queryByText("obraz uszkodzony")).toBeNull();
  });

  it("allows the same image to load on a new visit after a temporary request failure", () => {
    const first = render(<MemoryRouter><Thumb artifactId={ARTIFACT} alt="synthetic valid image" /></MemoryRouter>);
    fireEvent.error(screen.getByRole("img", { name: "synthetic valid image" }));
    first.unmount();
    resetThumbFailures(); // New module instance still reads the same browser localStorage.
    render(<MemoryRouter><Thumb artifactId={ARTIFACT} alt="synthetic valid image" /></MemoryRouter>);
    expect(screen.queryByRole("img", { name: "synthetic valid image" })).not.toBeNull();
  });

  it("control: a thumbnail without an error remains loadable on the next visit", () => {
    const first = render(<MemoryRouter><Thumb artifactId={ARTIFACT} alt="synthetic valid image" /></MemoryRouter>);
    expect(screen.getByRole("img", { name: "synthetic valid image" })).toHaveAttribute(
      "src", expect.stringContaining(ARTIFACT),
    );
    first.unmount();
    resetThumbFailures();
    render(<MemoryRouter><Thumb artifactId={ARTIFACT} alt="synthetic valid image" /></MemoryRouter>);
    expect(screen.getByRole("img", { name: "synthetic valid image" })).toBeInTheDocument();
  });
});
