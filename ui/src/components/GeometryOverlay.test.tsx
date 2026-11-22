import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../test/utils";
import { GeometryOverlay } from "./GeometryOverlay";

const body = {
  a: "inc-a", b: "inc-b", version: "vis_geom@1", scoring_flags: {}, note_pl: "widok pomocniczy",
  thresholds: { min_inliers: 120, min_coverage: 0.15, crop_coverage: 0.25, crop_grid: 0.3 },
  pairs: [{
    unit_a: "u1", unit_b: "u2", artifact_a: "a".repeat(64), artifact_b: "b".repeat(64), bbox_a: null, bbox_b: null,
    class_a: "creative", class_b: "creative", dinov2_cos: 0.93, source_size_a: [400, 300], source_size_b: [320, 240],
    method: "orb", inliers: 512, coverage_a: 0.61, coverage_b: 0.48, grid_a: 0.7, grid_b: 0.5, mirrored: false,
    homography: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], anisotropy: 1.01, perspective: 0.001, scale: 0.8,
    size_a: [400, 300], size_b: [320, 240], same_asset: true, crop_match: true,
    points: [[0.1, 0.2, 0.15, 0.25], [0.5, 0.5, 0.52, 0.49], [0.9, 0.8, 0.88, 0.79]], explanation_pl: "x",
  }],
};

describe("GeometryOverlay", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("draws one inlier line per correspondence and states the verdict", async () => {
    const f = mockFetch(async () => jsonResponse(body));
    renderWithProviders(<GeometryOverlay a="inc-a" b="inc-b" />);
    await waitFor(() => expect(screen.getByTestId("geometry-overlay")).toBeTruthy());
    expect(String(f.mock.calls[0]![0])).toContain("/v1/pairs/inc-a/inc-b/geometry");
    expect(screen.getByTestId("geometry-lines").querySelectorAll("line").length).toBe(3);
    expect(screen.getByText(/kadr \/ zmiana rozmiaru/)).toBeTruthy();
    expect(screen.getByRole("img", { name: /512 zgodnych par punktów/ })).toBeTruthy();
  });

  it("says so when no keypoints match", async () => {
    mockFetch(async () => jsonResponse({ ...body, pairs: [] }));
    renderWithProviders(<GeometryOverlay a="inc-a" b="inc-b" />);
    await waitFor(() => expect(screen.getByText(/brak wspólnych punktów kluczowych/)).toBeTruthy());
  });

  it("renders nothing when the endpoint is missing", async () => {
    mockFetch(async () => jsonResponse({ detail: "Not Found" }, { status: 404 }));
    const { container } = renderWithProviders(<GeometryOverlay a="inc-a" b="inc-b" />);
    await waitFor(() => expect(container.textContent).toBe(""));
  });
});
