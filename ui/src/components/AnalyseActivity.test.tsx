import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { AnalyseProgress, ImportPhase, ProgressSample } from "../api/progress";
import { authHeaders } from "../api/auth";
import { deriveLog, etaSeconds, measuredRate, overallPercent } from "../lib/progressMath";
import { AnalyseActivity } from "./AnalyseActivity";

const T0 = Date.parse("2026-11-22T03:00:00Z");

function imp(over: Partial<ImportPhase> = {}): ImportPhase {
  return { phase: "importing", files_total: 1968, files_done: 1240, bytes_total: 1000, bytes_done: 600, rejected: 7,
    incidents_total: 20, incidents_done: 12, current: "12/reklama.png", percent: 63, ...over };
}

function snap(over: Partial<AnalyseProgress> = {}): AnalyseProgress {
  const stage = over.stage ?? "import";
  const keys = ["import", "genes", "scoring", "clustering", "calibration", "self_assign", "persist", "done"];
  const labels = ["Import", "Ekstrakcja genów", "Rzadkość i ocena par", "Grupowanie", "Kalibracja", "Samoprzypisanie", "Zapis", "Gotowe"];
  const idx = keys.indexOf(stage);
  const status = over.status ?? "running";
  return {
    job_id: "j1", status, mode: "corpus", source: "upload", stage, stage_label: labels[idx] ?? stage, stage_percent: null,
    counters: {}, overall_percent: 0, weights_source: "JURY_READINESS.md §3, N=50, budżet 8", import: imp(), core: {},
    stages: keys.map((k, i) => ({ key: k, label: labels[i]!, weight: 0.1, percent: null,
      state: status === "done" || i < idx ? "done" : i === idx ? (status === "failed" ? "failed" : "running") : "waiting" })),
    started_at: new Date(T0).toISOString(), finished_at: null, ...over,
  };
}

const at = (s: AnalyseProgress, sec: number): ProgressSample => ({ snap: s, at: T0 + sec * 1000 });

describe("AnalyseActivity", () => {
  it("uploading: bytes, rate and ETA after enough samples, cancel aborts", async () => {
    const onCancel = vi.fn();
    const samples = Array.from({ length: 6 }, (_, i) => ({ loaded: (i + 1) * 100 * 1024 * 1024, total: 1100 * 1024 * 1024, at: i * 1000 }));
    render(<AnalyseActivity upload={{ phase: "uploading", samples }} samples={[]} onCancel={onCancel} now={T0} />);
    const bar = screen.getByTestId("activity-stage-bar");
    expect(bar).toHaveAttribute("role", "progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "54");
    expect(screen.getByTestId("activity-stage-text").textContent).toMatch(/^Wysyłanie plików: 54% \(600,0 MB z 1,1 GB\) · 100,0 MB\/s · ok\. 5 s$/);
    expect(screen.getByText("Wysyłanie").closest("li")).toHaveAttribute("aria-current", "step");
    await userEvent.click(screen.getByRole("button", { name: "Anuluj" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("uploading: no rate or ETA with few samples", () => {
    render(<AnalyseActivity upload={{ phase: "uploading", samples: [{ loaded: 10, total: 100, at: 0 }, { loaded: 20, total: 100, at: 500 }] }} samples={[]} now={T0} />);
    expect(screen.getByTestId("activity-stage-text").textContent).toBe("Wysyłanie plików: 20% (20 B z 100 B)");
  });

  it("indeterminate while the archive is listed, then determinate import", () => {
    const { rerender } = render(<AnalyseActivity upload={{ phase: "done", samples: [] }} samples={[at(snap({ import: imp({ phase: "listing", files_total: null, percent: null, files_done: 0 }) }), 1)]} now={T0 + 2000} />);
    const bar = screen.getByTestId("activity-stage-bar");
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(bar).toHaveAttribute("aria-busy", "true");
    expect(screen.getByTestId("activity-stage-text")).toHaveTextContent("Rozpakowywanie…");
    rerender(<AnalyseActivity upload={{ phase: "done", samples: [] }} samples={[at(snap(), 3)]} now={T0 + 3000} />);
    expect(screen.getByTestId("activity-stage-bar")).toHaveAttribute("aria-valuenow", "63");
    expect(screen.getByTestId("activity-stage-text").textContent).toBe("Import: 63% (1240 z 1968 plików) · odrzucone: 7");
    expect(screen.getByTestId("activity-item")).toHaveTextContent("12/reklama.png");
    expect(screen.getByText("Wysyłanie").closest("li")).not.toHaveAttribute("aria-current");
  });

  it("indeterminate stage without a counter is labelled, not animated as progress", () => {
    render(<AnalyseActivity upload={null} samples={[at(snap({ stage: "calibration", overall_percent: 70, import: imp({ phase: "done", percent: 100 }) }), 1)]} now={T0 + 2000} />);
    expect(screen.getByTestId("activity-stage-text")).toHaveTextContent("Kalibracja… (ten etap nie raportuje licznika)");
    expect(screen.getByTestId("activity-stage-bar")).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByTestId("activity-overall-bar")).toHaveAttribute("aria-valuenow", "70");
  });

  it("done: 100 % only at Gotowe, counts and report link", () => {
    const done = snap({ stage: "done", status: "done", overall_percent: 100, finished_at: new Date(T0 + 9000).toISOString(), import: imp({ phase: "done", files_done: 1968, percent: 100, incidents_done: 20 }) });
    render(<AnalyseActivity upload={{ phase: "done", samples: [] }} samples={[at(snap({ overall_percent: 3 }), 1), at(done, 9)]} reportHref="#import-report" now={T0 + 10_000} />);
    expect(screen.getByTestId("activity-overall-bar")).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByTestId("activity-stage-bar")).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByTestId("activity-done")).toHaveTextContent("Import zakończony: 1968 plików, 20 incydentów, odrzucone: 7.");
    expect(screen.getByRole("link", { name: "Raport importu →" })).toHaveAttribute("href", "#import-report");
    expect(screen.queryByTestId("activity-stall")).toBeNull();
  });

  it("error: Polish message and retry", async () => {
    const onRetry = vi.fn();
    render(<AnalyseActivity upload={{ phase: "error", samples: [], error: "413 upload exceeds 2048 MB" }} samples={[]} onRetry={onRetry} now={T0} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Wysyłanie nie powiodło się: 413 upload exceeds 2048 MB");
    await userEvent.click(screen.getByRole("button", { name: "Spróbuj ponownie" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("failed import job is reported as such", () => {
    render(<AnalyseActivity upload={null} samples={[at(snap({ status: "failed", error_code: "OSError", import: imp({ phase: "failed" }) }), 1)]} onRetry={() => undefined} now={T0 + 2000} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Analiza przerwana błędem (OSError) podczas importu.");
  });

  it("cancelled", () => {
    render(<AnalyseActivity upload={{ phase: "cancelled", samples: [] }} samples={[]} onRetry={() => undefined} now={T0} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Wysyłanie anulowane. Nic nie zostało zaimportowane.");
    expect(screen.queryByTestId("activity-stage-bar")).toBeNull();
    expect(screen.getByText("Wysyłanie").closest("li")).toHaveTextContent("(błąd)");
  });

  it("stall indicator after 20 s without a changed snapshot, never while uploading", () => {
    const s = [at(snap({ stage: "genes", stage_percent: 0, counters: { done: 0, total: 50, unit: "incydentów" } }), 0)];
    const { rerender } = render(<AnalyseActivity upload={null} samples={s} now={T0 + 15_000} />);
    expect(screen.queryByTestId("activity-stall")).toBeNull();
    rerender(<AnalyseActivity upload={null} samples={s} now={T0 + 34_000} />);
    expect(screen.getByTestId("activity-stall")).toHaveTextContent("brak zdarzeń od 34 s — praca trwa: Ekstrakcja genów");
  });

  it("ETA hidden with few samples, shown (szacunek) once measured", () => {
    const mk = (n: number) => Array.from({ length: n }, (_, i) => at(snap({ import: imp({ files_done: 100 * (i + 1), percent: 5 * (i + 1) }) }), i));
    const { rerender } = render(<AnalyseActivity upload={null} samples={mk(3)} now={T0 + 3000} />);
    expect(screen.queryByTestId("activity-eta")).toBeNull();
    rerender(<AnalyseActivity upload={null} samples={mk(6)} now={T0 + 6000} />);
    expect(screen.getByTestId("activity-eta").textContent).toMatch(/^ok\. \d+ s do końca etapu \(szacunek\)$/);
  });

  it("reduced motion: indeterminate bar is static", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"), addEventListener: () => undefined, removeEventListener: () => undefined }));
    render(<AnalyseActivity upload={{ phase: "processing", samples: [{ loaded: 5, total: 5, at: 0 }] }} samples={[]} now={T0} />);
    const fill = screen.getByTestId("activity-stage-bar").firstElementChild!;
    expect(fill.className).not.toContain("animate-indeterminate");
    expect(screen.getByTestId("activity-stage-text")).toHaveTextContent("Rozpakowywanie…");
  });
});

describe("progress math", () => {
  it("overall percent is monotonic and 100 only when done", () => {
    const seq = [5, 12, 9, 40, 99.9].map((p, i) => at(snap({ overall_percent: p }), i));
    const vals = seq.map((_, i) => overallPercent(seq.slice(0, i + 1)));
    expect(vals).toEqual([5, 12, 12, 40, 99]);
    expect(overallPercent([...seq, at(snap({ status: "done", overall_percent: 100 }), 9)])).toBe(100);
  });

  it("rate needs samples and span; ETA from the measured rate", () => {
    expect(measuredRate([{ t: 0, v: 0 }, { t: 1000, v: 10 }])).toBeNull();
    const pts = Array.from({ length: 5 }, (_, i) => ({ t: i * 1000, v: i * 10 }));
    expect(measuredRate(pts)).toBe(10);
    expect(etaSeconds(40, 100, 10)).toBe(6);
    expect(etaSeconds(100, 100, 10)).toBeNull();
  });

  it("log keeps the last 50 events", () => {
    const many = Array.from({ length: 80 }, (_, i) => at(snap({ import: imp({ current: `f${i}.png`, files_done: i }) }), i));
    const log = deriveLog(many);
    expect(log).toHaveLength(50);
    expect(log[log.length - 1]!.text).toBe("Import: f79.png (79/1968)");
  });

  it("the XHR upload carries the auth layer's CSRF header", async () => {
    document.cookie = "hydra_csrf=tok123; path=/";
    expect(Object.values(authHeaders("POST"))).toContain("tok123");
    const sent: Record<string, string> = {};
    class FakeXhr {
      upload = { onprogress: null as unknown };
      status = 202; statusText = ""; responseText = '{"job_id":"j9"}';
      onload: (() => void) | null = null;
      withCredentials = false;
      open() {}
      setRequestHeader(k: string, v: string) { sent[k] = v; }
      send() { this.onload?.(); }
      abort() {}
    }
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    const { postFormWithProgress } = await import("../api/upload");
    await expect(postFormWithProgress("/v1/analyse", new FormData(), { expect: [202] })).resolves.toEqual({ job_id: "j9" });
    expect(Object.values(sent)).toContain("tok123");
    document.cookie = "hydra_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  });
});
