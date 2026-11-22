/**
 * Multipart POST with upload progress (IMPORT-PROGRESS). `fetch` has no upload progress in browsers, so the request
 * goes through XMLHttpRequest: same URL rules as `apiFetch` (API_BASE + run selection), same-origin cookies
 * (`withCredentials`), the CSRF double-submit header of the auth layer (`authHeaders("POST")`, docs/SECURITY_AUTH.md),
 * the same 401/403 signals (`notifyAuthStatus`) and the same ApiError kinds (`aborted`, `network`, `http`, `parse`).
 */
import { authHeaders, notifyAuthStatus } from "./auth";
import { ApiError, apiUrl } from "./client";
import { withRun } from "../lib/runSelection";

export interface UploadProgress {
  loaded: number;
  total: number | null;
  /** performance.now() of the sample, for rates. */
  at: number;
}

export function postFormWithProgress<T>(
  path: string,
  form: FormData,
  { onProgress, signal, expect }: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal; expect?: readonly number[] } = {},
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new ApiError({ kind: "aborted", message: "Request aborted", path }));
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", apiUrl(withRun(path)));
    xhr.withCredentials = true;
    xhr.setRequestHeader("Accept", "application/json");
    for (const [k, v] of Object.entries(authHeaders("POST"))) xhr.setRequestHeader(k, v);
    const onAbort = () => xhr.abort();
    signal?.addEventListener("abort", onAbort, { once: true });
    const done = () => signal?.removeEventListener("abort", onAbort);
    xhr.upload.onprogress = (e) => onProgress?.({ loaded: e.loaded, total: e.lengthComputable ? e.total : null, at: performance.now() });
    xhr.onabort = () => { done(); reject(new ApiError({ kind: "aborted", message: "Request aborted", path })); };
    xhr.onerror = () => { done(); reject(new ApiError({ kind: "network", message: "Backend unreachable", path })); };
    xhr.onload = () => {
      done();
      const text = xhr.responseText ?? "";
      let body: unknown = text || undefined;
      if (text && /^[\s]*[[{"]/.test(text)) {
        try {
          body = JSON.parse(text) as unknown;
        } catch (cause) {
          reject(new ApiError({ kind: "parse", message: `Malformed JSON from ${path}`, path, status: xhr.status, body: text, cause }));
          return;
        }
      }
      if (xhr.status === 401 || xhr.status === 403) notifyAuthStatus(path, xhr.status, body);
      if (xhr.status < 200 || xhr.status >= 300) {
        const rec = body && typeof body === "object" ? (body as Record<string, unknown>) : {};
        const detail = rec.detail;
        const message = typeof detail === "string" ? detail
          : detail && typeof detail === "object" && typeof (detail as Record<string, unknown>).message === "string" ? String((detail as Record<string, unknown>).message)
          : `${xhr.status} ${xhr.statusText || "Request failed"}`.trim();
        reject(new ApiError({ kind: "http", message, path, status: xhr.status, body }));
        return;
      }
      if (expect && !expect.includes(xhr.status)) {
        reject(new ApiError({ kind: "contract", message: `Unexpected status ${xhr.status} from ${path} (expected ${expect.join("/")})`, path, status: xhr.status, body }));
        return;
      }
      resolve(body as T);
    };
    xhr.send(form);
  });
}
