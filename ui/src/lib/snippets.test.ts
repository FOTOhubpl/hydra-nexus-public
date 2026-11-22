import { describe, expect, it } from "vitest";
import { curlSnippet, javascriptSnippet, pythonSnippet, shellQuote, toPythonLiteral } from "./snippets";

const base = "http://127.0.0.1:8770";
const tricky = JSON.stringify({ value: "O'Reilly \"cytat\" zażółć $HOME `x`", n: null, ok: true, list: [1, false] });

/** Minimal POSIX parser for single-quoted words and backslash escapes: what the shell would pass. */
function shellUnquote(s: string): string {
  let out = "";
  let i = 0;
  while (i < s.length) {
    if (s[i] === "'") {
      const j = s.indexOf("'", i + 1);
      out += s.slice(i + 1, j);
      i = j + 1;
    } else if (s[i] === "\\") {
      out += s[i + 1];
      i += 2;
    } else {
      out += s[i];
      i += 1;
    }
  }
  return out;
}

describe("shell quoting", () => {
  it("round-trips quotes, dollars, backticks and Polish characters", () => {
    const q = shellQuote(tricky);
    expect(q.startsWith("'")).toBe(true);
    expect(shellUnquote(q)).toBe(tricky);
  });
});

describe("curl", () => {
  it("GET without -X and with a quoted URL", () => {
    expect(curlSnippet({ method: "get", baseUrl: base + "/", path: "/v1/campaigns?limit=5" })).toBe(`curl -sS '${base}/v1/campaigns?limit=5' \\\n  -H "Authorization: Bearer $HYDRA_API_TOKEN"`);
    expect(curlSnippet({ method: "get", baseUrl: base, path: "/health", auth: false })).toBe(`curl -sS '${base}/health'`);
  });
  it("POST JSON body compacted and safely quoted", () => {
    const s = curlSnippet({ method: "post", baseUrl: base, path: "/v1/watchlist", body: tricky });
    expect(s).toContain("-X POST");
    expect(s).toContain("-H 'Content-Type: application/json'");
    const data = s.split("--data-raw ")[1]!;
    expect(JSON.parse(shellUnquote(data))).toEqual(JSON.parse(tricky));
  });
  it("streaming uses -N and never a body", () => {
    expect(curlSnippet({ method: "get", baseUrl: base, path: "/v1/stream", streaming: true })).toBe(`curl -N -H 'Accept: text/event-stream' -H "Authorization: Bearer $HYDRA_API_TOKEN" '${base}/v1/stream'`);
  });
});

describe("python httpx", () => {
  it("converts JSON to Python literals", () => {
    expect(toPythonLiteral({ a: null, b: true, c: [false, 1.5], d: "ż\"'" })).toBe(
      '{\n    "a": None,\n    "b": True,\n    "c": [\n        False,\n        1.5,\n    ],\n    "d": "ż\\"\'",\n}',
    );
  });
  it("uses json= for JSON bodies and plain get otherwise", () => {
    const p = pythonSnippet({ method: "post", baseUrl: base, path: "/v1/watchlist", body: tricky });
    expect(p).toContain(`r = httpx.post("${base}/v1/watchlist", json=payload, headers=headers, timeout=30)`);
    expect(p).toContain(`headers = {"Authorization": f"Bearer {os.environ['HYDRA_API_TOKEN']}"}`);
    expect(p).toContain('"value": "O\'Reilly \\"cytat\\" zażółć $HOME `x`"');
    expect(p).toContain('"n": None');
    expect(pythonSnippet({ method: "get", baseUrl: base, path: "/health" })).toContain(`httpx.get("${base}/health", headers=headers, timeout=30)`);
  });
  it("non-JSON body goes as content=", () => {
    const p = pythonSnippet({ method: "post", baseUrl: base, path: "/x", body: "a'b\"c", contentType: "text/plain" });
    expect(p).toContain('body = "a\'b\\"c"');
    expect(p).toContain("content=body.encode");
  });
});

describe("javascript fetch", () => {
  it("embeds a JSON literal that evaluates to the same body", () => {
    const js = javascriptSnippet({ method: "post", baseUrl: base, path: "/v1/watchlist", body: tricky });
    const literal = js.split("body: JSON.stringify(")[1]!.split("),\n});")[0]!;
    expect(JSON.parse(literal)).toEqual(JSON.parse(tricky));
    expect(js).toContain(`fetch("${base}/v1/watchlist"`);
    expect(js).toContain('"Content-Type": "application/json"');
  });
  it("streaming uses EventSource", () => {
    expect(javascriptSnippet({ method: "get", baseUrl: base, path: "/v1/stream", streaming: true })).toContain(`new EventSource("${base}/v1/stream")`);
  });
  it("reads the bearer token from HYDRA_API_TOKEN and never embeds a real token", () => {
    for (const f of [curlSnippet, pythonSnippet, javascriptSnippet]) {
      const s = f({ method: "post", baseUrl: base, path: "/x", body: "{}" });
      expect(s).toContain("Authorization");
      expect(s).toContain("HYDRA_API_TOKEN");
      expect(s).not.toMatch(/hnx_[A-Za-z0-9]/);
      expect(f({ method: "post", baseUrl: base, path: "/x", body: "{}", auth: false }).toLowerCase()).not.toContain("authorization");
    }
  });
});
