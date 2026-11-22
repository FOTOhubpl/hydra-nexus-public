import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ImagePane, type Overlay } from "./ImagePane";

const overlays: Overlay[] = [
  { type: "box", x: 10, y: 10, w: 50, h: 20, label: "region" },
  { type: "line", x1: 0, y1: 0, x2: 100, y2: 100, dashed: true },
  { type: "point", x: 5, y: 5 },
];

function load(img: HTMLElement, width = 200, height = 100) {
  Object.defineProperty(img, "naturalWidth", { value: width, configurable: true });
  Object.defineProperty(img, "naturalHeight", { value: height, configurable: true });
  fireEvent.load(img);
}

describe("ImagePane", () => {
  it("draws overlays in the image's natural pixel space once loaded", () => {
    const { container } = render(<ImagePane src="/x.png" alt="sample" overlays={overlays} />);
    expect(container.querySelector("svg")).toBeNull();
    load(screen.getByAltText("sample"));
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveAttribute("viewBox", "0 0 200 100");
    expect(svg.querySelectorAll("[data-overlay]")).toHaveLength(3);
    expect(screen.getByText("region")).toBeInTheDocument();
    expect(screen.getByText("200 × 100px")).toBeInTheDocument();
  });

  it("uses a unit viewBox for normalized coordinates and reports overlay clicks", () => {
    const onOverlayClick = vi.fn();
    const { container } = render(
      <ImagePane src="/x.png" alt="sample" coordinateSpace="normalized" overlays={overlays} onOverlayClick={onOverlayClick} />,
    );
    load(screen.getByAltText("sample"));
    expect(container.querySelector("svg")).toHaveAttribute("viewBox", "0 0 1 1");
    fireEvent.click(container.querySelector("[data-overlay='box']")!);
    expect(onOverlayClick).toHaveBeenCalledWith(overlays[0], 0);
  });

  it("hides overlays with the toggle and shows an empty state on load errors", () => {
    const { container } = render(<ImagePane src="/x.png" alt="sample" overlays={overlays} />);
    load(screen.getByAltText("sample"));
    fireEvent.click(screen.getByRole("checkbox", { name: /nakładki/i }));
    expect(container.querySelector("svg")).toBeNull();
    fireEvent.error(screen.getByAltText("sample"));
    expect(screen.getByText("Obraz niedostępny")).toBeInTheDocument();
  });
});
