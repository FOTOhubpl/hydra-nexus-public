/**
 * Copyable request snippets (curl, Python httpx, JavaScript fetch) generated from the API console form.
 *
 * Quoting rules:
 * - shell: everything in single quotes; an embedded `'` becomes `'\''` (close, escaped quote, reopen), so JSON
 *   bodies with quotes, `$`, backticks or Polish characters are passed byte-for-byte.
 * - Python: JSON bodies become Python literals (None/True/False, strings via JSON escaping, which is valid
 *   Python string syntax); non-JSON bodies are sent as `content=` string literals.
 * - JavaScript: JSON bodies are pretty-printed object literals passed to JSON.stringify; strings via JSON escaping.
 * Auth: the API needs a session or a personal access token (Tokeny API). Snippets read the token from the environment
 * variable HYDRA_API_TOKEN (never embed a real token): curl `-H "Authorization: Bearer $HYDRA_API_TOKEN"`, httpx
 * `os.environ["HYDRA_API_TOKEN"]`, fetch a `HYDRA_API_TOKEN` constant. Browser EventSource cannot set headers and
 * relies on the session cookie.
 */

export const TOKEN_ENV = "HYDRA_API_TOKEN";
export const CURL_AUTH = `-H "Authorization: Bearer $${TOKEN_ENV}"`;

export interface SnippetRequest {
  method: string;
  /** Absolute base, e.g. `http://127.0.0.1:8770` (location.origin). */
  baseUrl: string;
  /** Path with query, e.g. `/v1/campaigns?limit=5` — the API path, without the `/api` UI prefix. */
  path: string;
  /** Raw body text (JSON or not); empty = no body. */
  body?: string;
  contentType?: string;
  /** SSE / long-lived stream: curl -N, EventSource. */
  streaming?: boolean;
  /** Add the bearer token header (default true). */
  auth?: boolean;
}

export type SnippetLang = "curl" | "python" | "javascript";

export const SNIPPET_LABELS: Record<SnippetLang, string> = {
  curl: "curl",
  python: "Python (httpx)",
  javascript: "JavaScript (fetch)",
};

/** POSIX shell single-quoting. */
export function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/** Joined URL; the base never carries a trailing slash twice. */
export function snippetUrl(req: SnippetRequest): string {
  const base = req.baseUrl.replace(/\/+$/, "");
  const path = req.path.startsWith("/") ? req.path : `/${req.path}`;
  return `${base}${path}`;
}

function parseJson(text: string | undefined): { ok: true; value: unknown } | { ok: false } {
  if (!text || !text.trim()) return { ok: false };
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch {
    return { ok: false };
  }
}

function hasBody(req: SnippetRequest): boolean {
  return Boolean(req.body && req.body.trim()) && !["GET", "HEAD"].includes(req.method.toUpperCase());
}

export function curlSnippet(req: SnippetRequest): string {
  const method = req.method.toUpperCase();
  const url = snippetUrl(req);
  const auth = req.auth !== false;
  if (req.streaming) {
    return `curl -N -H 'Accept: text/event-stream'${auth ? ` ${CURL_AUTH}` : ""} ${shellQuote(url)}`;
  }
  const parts = [`curl -sS${method !== "GET" ? ` -X ${method}` : ""} ${shellQuote(url)}`];
  if (auth) parts.push(CURL_AUTH);
  if (hasBody(req)) {
    const json = parseJson(req.body);
    const text = json.ok ? JSON.stringify(json.value) : (req.body as string);
    parts.push(`-H ${shellQuote(`Content-Type: ${req.contentType ?? "application/json"}`)}`);
    parts.push(`--data-raw ${shellQuote(text)}`);
  }
  return parts.join(" \\\n  ");
}

/** JSON value → Python literal (json.loads round-trips it). */
export function toPythonLiteral(value: unknown, indent = 0): string {
  const pad = "    ".repeat(indent + 1);
  const end = "    ".repeat(indent);
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "None";
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    return `[\n${value.map((v) => `${pad}${toPythonLiteral(v, indent + 1)},`).join("\n")}\n${end}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return "{}";
    return `{\n${entries.map(([k, v]) => `${pad}${JSON.stringify(k)}: ${toPythonLiteral(v, indent + 1)},`).join("\n")}\n${end}}`;
  }
  return JSON.stringify(String(value));
}

export function pythonSnippet(req: SnippetRequest): string {
  const method = req.method.toLowerCase();
  const url = JSON.stringify(snippetUrl(req));
  const auth = req.auth !== false;
  const head = auth ? ["import os", "", "import httpx", "", `headers = {"Authorization": f"Bearer {os.environ['${TOKEN_ENV}']}"}`] : ["import httpx"];
  const h = auth ? ", headers=headers" : "";
  if (req.streaming) {
    return [
      ...head,
      "",
      "# Strumień SSE: czytaj linie, aż przerwiesz (Ctrl+C).",
      `with httpx.stream("GET", ${url}, headers={${auth ? "**headers, " : ""}"Accept": "text/event-stream"}, timeout=None) as r:`,
      "    for line in r.iter_lines():",
      "        if line:",
      "            print(line)",
    ].join("\n");
  }
  const lines = [...head, ""];
  if (hasBody(req)) {
    const json = parseJson(req.body);
    if (json.ok) {
      lines.push(`payload = ${toPythonLiteral(json.value)}`);
      lines.push(`r = httpx.${method}(${url}, json=payload${h}, timeout=30)`);
    } else {
      lines.push(`body = ${JSON.stringify(req.body)}`);
      lines.push(
        `r = httpx.request(${JSON.stringify(req.method.toUpperCase())}, ${url}, content=body.encode("utf-8"), headers={${auth ? "**headers, " : ""}"Content-Type": ${JSON.stringify(req.contentType ?? "text/plain")}}, timeout=30)`,
      );
    }
  } else if (["get", "delete"].includes(method)) {
    lines.push(`r = httpx.${method}(${url}${h}, timeout=30)`);
  } else {
    lines.push(`r = httpx.request(${JSON.stringify(req.method.toUpperCase())}, ${url}${h}, timeout=30)`);
  }
  lines.push("print(r.status_code)");
  lines.push('print(r.json() if "json" in r.headers.get("content-type", "") else r.text)');
  return lines.join("\n");
}

export function javascriptSnippet(req: SnippetRequest): string {
  const url = JSON.stringify(snippetUrl(req));
  if (req.streaming) {
    return [
      "// Przeglądarka: EventSource wznawia połączenie sam i wysyła Last-Event-ID.",
      "// Nie ustawia nagłówków: uwierzytelnia się ciasteczkiem sesji zalogowanego panelu (ta sama domena).",
      `const es = new EventSource(${url});`,
      'for (const type of ["hello", "delta", "reset", "job", "watch"]) {',
      "  es.addEventListener(type, (e) => console.log(type, e.lastEventId, JSON.parse(e.data)));",
      "}",
      "// es.close() kończy subskrypcję.",
    ].join("\n");
  }
  const auth = req.auth !== false;
  const opts: string[] = [`  method: ${JSON.stringify(req.method.toUpperCase())},`];
  const authEntry = auth ? "Authorization: `Bearer ${" + TOKEN_ENV + "}`" : "";
  if (hasBody(req)) {
    const json = parseJson(req.body);
    opts.push(`  headers: { ${auth ? `${authEntry}, ` : ""}"Content-Type": ${JSON.stringify(req.contentType ?? "application/json")} },`);
    if (json.ok) {
      const literal = JSON.stringify(json.value, null, 2).replace(/\n/g, "\n  ");
      opts.push(`  body: JSON.stringify(${literal}),`);
    } else {
      opts.push(`  body: ${JSON.stringify(req.body)},`);
    }
  } else if (auth) opts.push(`  headers: { ${authEntry} },`);
  return [
    ...(auth ? [`const ${TOKEN_ENV} = "<token z Tokeny API>"; // nie zapisuj tokenu w repozytorium`] : []),
    `const res = await fetch(${url}, {`,
    ...opts,
    "});",
    'const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();',
    "console.log(res.status, data);",
  ].join("\n");
}

export function buildSnippet(lang: SnippetLang, req: SnippetRequest): string {
  if (lang === "curl") return curlSnippet(req);
  if (lang === "python") return pythonSnippet(req);
  return javascriptSnippet(req);
}
