import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { JobProgress, jobFraction } from "./JobProgress";
import { jsonResponse, mockFetch, renderWithProviders } from "../test/utils";

describe("jobFraction", () => {
  it("prefers processed/total, then progress, and is null when unknown", () => {
    expect(jobFraction({ status: "running", processed: 5, total: 20 })).toBe(0.25);
    expect(jobFraction({ status: "running", progress: 0.4 })).toBe(0.4);
    expect(jobFraction({ status: "running", progress: 40 })).toBe(0.4);
    expect(jobFraction({ status: "running" })).toBeNull();
    expect(jobFraction({ status: "done" })).toBe(1);
    expect(jobFraction(undefined)).toBeNull();
  });
});

describe("JobProgress", () => {
  it("shows status, progress and counts from the backend, and reports the terminal state", async () => {
    mockFetch(async () =>
      jsonResponse({ job_id: "j1", status: "done", processed: 3, total: 3, counts: { skipped: 1 } }),
    );
    const onTerminal = vi.fn();
    renderWithProviders(<JobProgress jobId="j1" onTerminal={onTerminal} />);

    expect(await screen.findByText("zakończone")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByText("pominięte")).toBeInTheDocument();
    expect(onTerminal).toHaveBeenCalledTimes(1);
    expect(onTerminal.mock.calls[0]![0]).toMatchObject({ status: "done" });
  });

  it("shows the job error message on failure", async () => {
    mockFetch(async () => jsonResponse({ job_id: "j2", status: "failed", error: "disk full" }));
    renderWithProviders(<JobProgress jobId="j2" />);
    expect(await screen.findByText("błąd")).toBeInTheDocument();
    expect(screen.getByText("disk full")).toBeInTheDocument();
  });

  it("marks the job offline when the backend is unreachable", async () => {
    mockFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    renderWithProviders(<JobProgress jobId="j3" />);
    expect(await screen.findByText("brak połączenia")).toBeInTheDocument();
  });
});
