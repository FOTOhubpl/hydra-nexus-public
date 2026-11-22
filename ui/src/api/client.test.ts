import { describe, expect, it } from "vitest";
import type { Query } from "@tanstack/react-query";
import {
  apiFetch,
  ApiError,
  extractErrorMessage,
  getHealth,
  getJob,
  importFiles,
  IMPORT_FIELD,
  isTerminalStatus,
  jobRefetchInterval,
  listArtifacts,
  shouldRetry,
} from "./client";
import type { Job } from "./types";
import { jsonResponse, mockFetch } from "../test/utils";

async function catchError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError);
    return error as ApiError;
  }
  throw new Error("expected the call to reject");
}

describe("apiFetch", () => {
  it("prefixes /api and parses JSON", async () => {
    const fetch = mockFetch(async () => jsonResponse({ status: "ok", version: "0.1.0" }));
    await expect(getHealth()).resolves.toEqual({ status: "ok", version: "0.1.0" });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]![0]).toBe("/api/health");
  });

  it("turns a rejected fetch into an offline ApiError", async () => {
    mockFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    const error = await catchError(getHealth());
    expect(error.kind).toBe("network");
    expect(error.status).toBeNull();
    expect(error.isOffline).toBe(true);
    expect(error.message).toBe("Backend unreachable");
  });

  it("treats proxy errors (502/503/504) as offline", async () => {
    mockFetch(async () => new Response("", { status: 502, statusText: "Bad Gateway" }));
    const error = await catchError(getHealth());
    expect(error.kind).toBe("http");
    expect(error.status).toBe(502);
    expect(error.isOffline).toBe(true);
  });

  it("surfaces FastAPI string details", async () => {
    mockFetch(async () => jsonResponse({ detail: "Job not found" }, { status: 404, statusText: "Not Found" }));
    const error = await catchError(getJob("missing"));
    expect(error.status).toBe(404);
    expect(error.message).toBe("Job not found");
    expect(error.isNotImplemented).toBe(true);
    expect(error.isClientError).toBe(true);
    expect(error.path).toBe("/v1/jobs/missing");
  });

  it("flattens FastAPI validation errors", async () => {
    mockFetch(async () =>
      jsonResponse({ detail: [{ loc: ["query", "limit"], msg: "must be > 0", type: "value_error" }] }, { status: 422 }),
    );
    const error = await catchError(listArtifacts({ limit: 0, offset: 0 }));
    expect(error.status).toBe(422);
    expect(error.message).toBe("query.limit: must be > 0");
  });

  it("falls back to the status line for empty error bodies", async () => {
    mockFetch(async () => new Response(null, { status: 500, statusText: "Internal Server Error" }));
    const error = await catchError(apiFetch("/boom"));
    expect(error.message).toBe("500 Internal Server Error");
  });

  it("reports malformed JSON as a parse error", async () => {
    mockFetch(async () => new Response("{not json", { status: 200, headers: { "content-type": "application/json" } }));
    const error = await catchError(apiFetch("/bad"));
    expect(error.kind).toBe("parse");
  });

  it("returns undefined for 204 responses", async () => {
    mockFetch(async () => new Response(null, { status: 204 }));
    await expect(apiFetch("/empty", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("encodes path segments and query parameters", async () => {
    const fetch = mockFetch(async () => jsonResponse({ items: [], total: 0 }));
    await listArtifacts({ limit: 25, offset: 50 });
    await getJob("a/b c").catch(() => undefined);
    expect(fetch.mock.calls[0]![0]).toBe("/api/v1/artifacts?limit=25&offset=50");
    expect(fetch.mock.calls[1]![0]).toBe("/api/v1/jobs/a%2Fb%20c");
  });
});

describe("importFiles", () => {
  const entry = () => ({ file: new File(["hello"], "a.txt", { type: "text/plain" }), path: "dir/a.txt" });

  it("posts multipart form data and returns the job id on 202", async () => {
    const fetch = mockFetch(async () => jsonResponse({ job_id: "job-1" }, { status: 202 }));
    await expect(importFiles([entry()])).resolves.toEqual({ job_id: "job-1" });
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe("/api/v1/import");
    expect(init?.method).toBe("POST");
    const body = init?.body as FormData;
    expect(body).toBeInstanceOf(FormData);
    const file = body.get(IMPORT_FIELD) as File;
    expect(file.name).toBe("dir/a.txt");
    // multipart boundary must be left to the browser
    expect(new Headers(init?.headers).has("Content-Type")).toBe(false);
  });

  it("rejects a 2xx that is not 202", async () => {
    mockFetch(async () => jsonResponse({ job_id: "job-1" }, { status: 200 }));
    const error = await catchError(importFiles([entry()]));
    expect(error.kind).toBe("contract");
  });

  it("rejects a 202 without job_id", async () => {
    mockFetch(async () => jsonResponse({}, { status: 202 }));
    const error = await catchError(importFiles([entry()]));
    expect(error.kind).toBe("contract");
    expect(error.message).toMatch(/job_id/);
  });

  it("refuses an empty upload without calling the backend", async () => {
    const fetch = mockFetch(async () => jsonResponse({ job_id: "x" }, { status: 202 }));
    await catchError(importFiles([]));
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("job polling helpers", () => {
  const fakeQuery = (data?: Partial<Job>, error?: unknown) =>
    ({ state: { data, error: error ?? null } }) as unknown as Query<Job, Error>;

  it("recognises terminal statuses", () => {
    expect(isTerminalStatus("done")).toBe(true);
    expect(isTerminalStatus("failed")).toBe(true);
    expect(isTerminalStatus("running")).toBe(false);
    expect(isTerminalStatus("bogus")).toBe(false);
  });

  it("polls until a terminal status, then stops", () => {
    expect(jobRefetchInterval(fakeQuery(), 1000)).toBe(1000);
    expect(jobRefetchInterval(fakeQuery({ status: "running" }), 1000)).toBe(1000);
    expect(jobRefetchInterval(fakeQuery({ status: "done" }), 1000)).toBe(false);
    expect(jobRefetchInterval(fakeQuery({ status: "failed" }), 1000)).toBe(false);
  });

  it("stops on 4xx and backs off while offline", () => {
    const notFound = new ApiError({ kind: "http", message: "nope", path: "/v1/jobs/x", status: 404 });
    const offline = new ApiError({ kind: "network", message: "down", path: "/v1/jobs/x" });
    expect(jobRefetchInterval(fakeQuery(undefined, notFound), 1000)).toBe(false);
    expect(jobRefetchInterval(fakeQuery(undefined, offline), 1000)).toBe(3000);
  });

  it("retries network failures once and never 4xx", () => {
    const offline = new ApiError({ kind: "network", message: "down", path: "/x" });
    const notFound = new ApiError({ kind: "http", message: "nope", path: "/x", status: 404 });
    expect(shouldRetry(0, offline)).toBe(true);
    expect(shouldRetry(1, offline)).toBe(false);
    expect(shouldRetry(0, notFound)).toBe(false);
  });
});

describe("extractErrorMessage", () => {
  it("handles common error shapes", () => {
    expect(extractErrorMessage({ message: "m" }, "f")).toBe("m");
    expect(extractErrorMessage({ error: "e" }, "f")).toBe("e");
    expect(extractErrorMessage("plain text", "f")).toBe("plain text");
    expect(extractErrorMessage(undefined, "fallback")).toBe("fallback");
  });
});
