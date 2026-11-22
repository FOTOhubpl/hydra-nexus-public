/**
 * "Konsola API" (/integrations?tab=console[&op=<operationId>]): an API explorer generated from the local FastAPI
 * schema (GET /api/openapi.json, same origin). Requests go only to this instance (same-origin fetch to /api + path);
 * unsafe methods need an explicit confirm step; SSE endpoints show a curl -N / EventSource snippet instead of a
 * "Wyślij" that would hang. History (localStorage) keeps method/path/status/time only — see console/history.ts.
 * Keyboard: "/" focuses the search, Ctrl/Cmd+Enter sends (or confirms).
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router";
import { ApiErrorState } from "../../components/ApiErrorState";
import { Button } from "../../components/Button";
import { EmptyState } from "../../components/EmptyState";
import { Skeleton } from "../../components/Skeleton";
import { apiUrl } from "../../api/client";
import { cx } from "../../lib/cx";
import {
  UNSAFE_METHODS,
  bodySkeleton,
  buildPath,
  buildQuery,
  defaultParamValues,
  filterOperations,
  findOperation,
  groupByTag,
  validateJsonBody,
  validateParams,
  type OpenApiDoc,
  type Operation,
  type ParamInfo,
  type ParamValues,
} from "../../lib/openapi";
import { SNIPPET_LABELS, buildSnippet, type SnippetLang } from "../../lib/snippets";
import { CodeBlock, CopyButton, MethodPill, StatusPill } from "./console/CodeBlock";
import { CONSOLE_EXAMPLES, type ConsoleExample } from "./console/examples";
import { clearHistory, loadHistory, recordHistory, type HistoryEntry } from "./console/history";
import { snippetBase, useOpenApi } from "./console/useOpenApi";
import { consoleAuth, consoleRequestAuth, useConsoleAuth } from "./console/consoleAuth";
import { notifyAuthStatus } from "../../api/auth";

const TIMEOUTS = [10, 30, 120] as const;
const DISPLAY_CAP = 512 * 1024;

export interface ConsoleResponse {
  status: number | null;
  statusText: string;
  ms: number;
  bytes: number;
  headers: [string, string][];
  contentType: string;
  body: string;
  isJson: boolean;
  binary: boolean;
  error?: string;
  truncated: boolean;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toLocaleString("pl-PL", { maximumFractionDigits: 1 })} KB`;
  return `${(n / 1024 / 1024).toLocaleString("pl-PL", { maximumFractionDigits: 1 })} MB`;
}

const TEXTUAL = /json|text|xml|javascript|csv|ndjson|yaml|x-www-form-urlencoded/i;

/** Execute one request against this instance and normalise the result for the viewer. */
export async function executeRequest(
  method: string,
  pathWithQuery: string,
  body: string | undefined,
  contentType: string | undefined,
  signal: AbortSignal,
): Promise<ConsoleResponse> {
  const auth = consoleRequestAuth(method);
  const headers: Record<string, string> = { Accept: "application/json, text/plain, */*", ...auth.headers };
  const sendBody = body && body.trim() && !["GET", "HEAD"].includes(method.toUpperCase()) ? body : undefined;
  if (sendBody) headers["Content-Type"] = contentType ?? "application/json";
  const t0 = performance.now();
  const res = await fetch(apiUrl(pathWithQuery), { method: method.toUpperCase(), headers, body: sendBody, signal, credentials: auth.credentials });
  // a 401 with a pasted token is the token's problem, not the session's
  if (auth.credentials !== "omit") notifyAuthStatus(pathWithQuery, res.status);
  const buf = await res.arrayBuffer();
  const ms = performance.now() - t0;
  const ct = res.headers.get("content-type") ?? "";
  const headerList: [string, string][] = [];
  res.headers.forEach((v, k) => headerList.push([k, v]));
  headerList.sort(([a], [b]) => a.localeCompare(b));
  const binary = buf.byteLength > 0 && ct !== "" && !TEXTUAL.test(ct);
  let text = "";
  let isJson = false;
  let truncated = false;
  if (!binary) {
    text = new TextDecoder("utf-8").decode(buf);
    if (ct.includes("json") || /^\s*[[{]/.test(text)) {
      try {
        text = JSON.stringify(JSON.parse(text), null, 2);
        isJson = true;
      } catch {
        /* keep raw text */
      }
    }
    if (text.length > DISPLAY_CAP) {
      text = `${text.slice(0, DISPLAY_CAP)}\n…`;
      truncated = true;
    }
  }
  return { status: res.status, statusText: res.statusText, ms, bytes: buf.byteLength, headers: headerList, contentType: ct, body: text, isJson, binary, truncated };
}

// ---------------------------------------------------------------------------
// Operation list
// ---------------------------------------------------------------------------

function OperationList({
  ops,
  selected,
  onSelect,
  query,
  setQuery,
  searchRef,
}: {
  ops: Operation[];
  selected: Operation | undefined;
  onSelect: (op: Operation) => void;
  query: string;
  setQuery: (q: string) => void;
  searchRef: React.RefObject<HTMLInputElement | null>;
}) {
  const filtered = useMemo(() => filterOperations(ops, query), [ops, query]);
  const groups = useMemo(() => groupByTag(filtered), [filtered]);
  return (
    <nav aria-label="Lista endpointów" className="surface-card flex min-h-0 min-w-0 flex-col">
      <div className="border-b border-line p-3">
        <label htmlFor="console-search" className="mb-1 block text-xs font-medium text-muted">
          Szukaj endpointu <kbd className="ml-1 rounded border border-line-strong px-1 font-mono text-[10px] text-subtle">/</kbd>
        </label>
        <input
          id="console-search"
          ref={searchRef}
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="ścieżka, opis lub tag, np. health"
          autoComplete="off"
          className="h-8 w-full rounded-md border border-line-strong bg-panel px-2 text-sm text-fg placeholder:text-subtle focus-visible:outline-2 focus-visible:outline-accent"
          data-testid="console-search"
        />
        <p className="mt-1 text-xs text-subtle" aria-live="polite">
          {filtered.length} z {ops.length} endpointów
        </p>
      </div>
      <div className="max-h-[22rem] min-h-0 overflow-y-auto lg:max-h-[calc(100vh-16rem)]" data-testid="console-op-list">
        {groups.length === 0 && <EmptyState compact title="Brak wyników" description="Zmień zapytanie: szukamy w ścieżce, opisie i tagu." />}
        {groups.map((g) => (
          <section key={g.tag} aria-label={g.label} className="border-b border-line last:border-b-0">
            <h3 className="sticky top-0 z-[1] bg-panel px-3 pt-2 pb-1 text-[11px] font-semibold tracking-wide text-subtle uppercase">
              {g.label} <span className="font-normal normal-case">({g.operations.length})</span>
            </h3>
            <ul className="pb-1">
              {g.operations.map((op) => {
                const active = selected?.id === op.id;
                return (
                  <li key={op.id}>
                    <button
                      type="button"
                      onClick={() => onSelect(op)}
                      aria-current={active ? "true" : undefined}
                      data-testid={`op-${op.id}`}
                      className={cx(
                        "flex w-full min-w-0 items-start gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-hover focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent",
                        active && "bg-accent-soft",
                      )}
                    >
                      <MethodPill method={op.method} className="mt-px" />
                      <span className="min-w-0 flex-1">
                        <span className={cx("block font-mono break-all text-fg", op.deprecated && "line-through decoration-subtle")}>{op.path}</span>
                        {op.summary && <span className="block truncate text-subtle">{op.summary}</span>}
                      </span>
                      {op.deprecated && <span className="shrink-0 rounded-full border border-warn/40 px-1.5 text-[10px] text-warn">przestarzały</span>}
                      {op.streaming && <span className="shrink-0 rounded-full border border-info/40 px-1.5 text-[10px] text-info">SSE</span>}
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Form
// ---------------------------------------------------------------------------

function ParamField({ p, value, onChange }: { p: ParamInfo; value: string; onChange: (v: string) => void }) {
  const id = `param-${p.in}-${p.name}`;
  const hint = [p.typeLabel, p.description].filter(Boolean).join(" · ");
  const control =
    p.enum ? (
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)} className="h-8 w-full min-w-0 rounded-md border border-line-strong bg-panel px-2 text-sm text-fg focus-visible:outline-2 focus-visible:outline-accent">
        {!p.required && <option value="">(nie wysyłaj)</option>}
        {p.enum.map((v) => (
          <option key={v} value={v}>
            {v}
          </option>
        ))}
      </select>
    ) : p.kind === "boolean" ? (
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)} className="h-8 w-full min-w-0 rounded-md border border-line-strong bg-panel px-2 text-sm text-fg focus-visible:outline-2 focus-visible:outline-accent">
        {!p.required && <option value="">(nie wysyłaj)</option>}
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    ) : (
      <input
        id={id}
        type="text"
        inputMode={p.kind === "integer" || p.kind === "number" ? "decimal" : undefined}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={p.kind === "array" ? "wartości po przecinku" : p.default !== undefined && p.default !== null ? String(p.default) : ""}
        autoComplete="off"
        spellCheck={false}
        className="h-8 w-full min-w-0 rounded-md border border-line-strong bg-panel px-2 font-mono text-sm text-fg focus-visible:outline-2 focus-visible:outline-accent"
      />
    );
  return (
    <div className="grid min-w-0 gap-1 sm:grid-cols-[12rem_1fr] sm:items-start sm:gap-3">
      <label htmlFor={id} className="min-w-0 pt-1.5 text-xs">
        <span className="font-mono font-semibold break-all text-fg">{p.name}</span>
        {p.required && <span className="ml-1 text-err" aria-label="wymagany">*</span>}
        <span className="ml-1.5 rounded-full border border-line px-1.5 text-[10px] text-subtle">{p.in === "path" ? "ścieżka" : "zapytanie"}</span>
      </label>
      <div className="min-w-0">
        {control}
        {hint && <p className="mt-0.5 text-[11px] break-words text-subtle">{hint}</p>}
      </div>
    </div>
  );
}

function Section({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="min-w-0 space-y-2">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <h3 className="text-xs font-semibold tracking-wide text-subtle uppercase">{title}</h3>
        {actions}
      </div>
      {children}
    </section>
  );
}

type Phase = { kind: "idle" } | { kind: "confirm" } | { kind: "sending"; startedAt: number } | { kind: "done"; res: ConsoleResponse } | { kind: "error"; message: string; ms: number };

function ResponseView({ phase, onAbort }: { phase: Phase; onAbort: () => void }) {
  if (phase.kind === "sending") {
    return (
      <div role="status" className="flex items-center justify-between gap-3 rounded-lg border border-line bg-raised px-3 py-2 text-sm text-muted">
        <span>Wysyłanie żądania do lokalnej instancji…</span>
        <Button size="sm" variant="secondary" onClick={onAbort}>
          Przerwij
        </Button>
      </div>
    );
  }
  if (phase.kind === "error") {
    return (
      <div role="alert" data-testid="response-error" className="space-y-1 rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
        <div className="flex items-center gap-2">
          <StatusPill status={null} />
          <span className="font-mono text-xs">{Math.round(phase.ms)} ms</span>
        </div>
        <p>{phase.message}</p>
      </div>
    );
  }
  if (phase.kind !== "done") return null;
  const r = phase.res;
  return (
    <div className="min-w-0 space-y-2" data-testid="console-response">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <StatusPill status={r.status} />
        <span className="text-fg">{r.statusText}</span>
        <span className="rounded-full border border-line px-2 py-0.5 font-mono" data-testid="response-time">
          {Math.round(r.ms)} ms
        </span>
        <span className="rounded-full border border-line px-2 py-0.5 font-mono" data-testid="response-size">
          {formatBytes(r.bytes)}
        </span>
        {r.contentType && <span className="min-w-0 truncate font-mono text-subtle">{r.contentType}</span>}
      </div>
      <details className="rounded-lg border border-line bg-raised text-xs">
        <summary className="cursor-pointer px-3 py-1.5 font-medium text-muted">Nagłówki odpowiedzi ({r.headers.length})</summary>
        <dl className="grid min-w-0 grid-cols-[minmax(0,10rem)_minmax(0,1fr)] gap-x-3 gap-y-0.5 px-3 pb-2 font-mono">
          {r.headers.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="break-all text-subtle">{k}</dt>
              <dd className="break-all text-fg">{v}</dd>
            </div>
          ))}
        </dl>
      </details>
      {r.binary ? (
        <p className="rounded-lg border border-line bg-raised px-3 py-2 text-sm text-muted">
          Odpowiedź binarna ({formatBytes(r.bytes)}, {r.contentType}). Pobierz ją poleceniem curl z zakładki fragmentów kodu.
        </p>
      ) : r.body ? (
        <CodeBlock code={r.body} label={`Treść odpowiedzi${r.isJson ? " (JSON)" : ""}${r.truncated ? " — przycięto do 512 KB" : ""}`} testId="response-body" />
      ) : (
        <p className="text-sm text-subtle">Pusta treść odpowiedzi.</p>
      )}
    </div>
  );
}

/** Session (default) or a pasted personal access token; the token stays in memory of this tab only. */
function ConsoleAuthPanel() {
  const auth = useConsoleAuth();
  return (
    <section aria-label="Uwierzytelnianie konsoli" className="surface-card flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:gap-4" data-testid="console-auth">
      <div role="radiogroup" aria-label="Uwierzytelnianie żądań" className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
        <label className="flex items-center gap-1.5 text-fg">
          <input type="radio" name="console-auth" checked={auth.mode === "session"} onChange={() => consoleAuth.set({ mode: "session" })} className="accent-[var(--color-accent)]" />
          Sesja przeglądarki
        </label>
        <label className="flex items-center gap-1.5 text-fg">
          <input type="radio" name="console-auth" checked={auth.mode === "token"} onChange={() => consoleAuth.set({ mode: "token" })} className="accent-[var(--color-accent)]" />
          Token API (Bearer)
        </label>
      </div>
      {auth.mode === "token" ? (
        <label className="flex min-w-0 flex-1 items-center gap-2 text-xs text-muted">
          <span className="shrink-0">Token</span>
          <input
            type="password"
            value={auth.token}
            onChange={(e) => consoleAuth.set({ token: e.target.value })}
            placeholder="hnx_…"
            autoComplete="off"
            spellCheck={false}
            className="h-8 min-w-0 flex-1 rounded-md border border-line-strong bg-panel px-2 font-mono text-xs text-fg focus-visible:outline-2 focus-visible:outline-accent"
            data-testid="console-token"
          />
        </label>
      ) : (
        <p className="text-xs text-subtle">Żądania idą z ciasteczkiem sesji i nagłówkiem X-CSRF-Token, z uprawnieniami zalogowanego konta.</p>
      )}
      {auth.mode === "token" && <p className="text-2xs text-subtle sm:max-w-[16rem]">Token jest tylko w pamięci tej karty (nie trafia do historii ani localStorage). Wysyłany jako Authorization: Bearer, bez ciasteczek.</p>}
    </section>
  );
}

function Snippets({ op, path, body }: { op: Operation; path: string; body: string }) {
  const [lang, setLang] = useState<SnippetLang>("curl");
  const code = buildSnippet(lang, {
    method: op.method,
    baseUrl: snippetBase(),
    path,
    body: op.body ? body : undefined,
    contentType: op.body?.contentType,
    streaming: op.streaming,
  });
  const langs: SnippetLang[] = ["curl", "python", "javascript"];
  return (
    <Section title="Fragmenty kodu">
      <div role="group" aria-label="Język fragmentu" className="flex flex-wrap gap-1">
        {langs.map((l) => (
          <button
            key={l}
            type="button"
            aria-pressed={lang === l}
            onClick={() => setLang(l)}
            className={cx(
              "h-6 rounded-full border px-2.5 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-accent",
              lang === l ? "border-accent bg-accent-soft text-fg" : "border-line-strong text-muted hover:bg-hover",
            )}
          >
            {SNIPPET_LABELS[l]}
          </button>
        ))}
      </div>
      <CodeBlock code={code} label={SNIPPET_LABELS[lang]} testId="console-snippet" maxHeight="20rem" />
    </Section>
  );
}

function OperationPane({
  doc,
  op,
  preset,
  onHistory,
}: {
  doc: OpenApiDoc;
  op: Operation;
  preset: { values?: Record<string, string>; body?: string; nonce: number } | null;
  onHistory: (h: HistoryEntry[]) => void;
}) {
  const [values, setValues] = useState<ParamValues>(() => ({ ...defaultParamValues(op), ...(preset?.values ?? {}) }));
  const [body, setBody] = useState<string>(() => preset?.body ?? bodySkeleton(doc, op));
  const [timeoutS, setTimeoutS] = useState<(typeof TIMEOUTS)[number]>(30);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [problems, setProblems] = useState<string[]>([]);
  const ctrl = useRef<AbortController | null>(null);

  useEffect(() => () => ctrl.current?.abort(), []);

  const pathOnly = buildPath(op.path, values);
  const query = buildQuery(op, values);
  const fullPath = `${pathOnly}${query ? `?${query}` : ""}`;
  const unsafe = UNSAFE_METHODS.has(op.method);
  const formParams = op.params.filter((p) => p.in === "path" || p.in === "query");
  const otherParams = op.params.filter((p) => p.in === "header" || p.in === "cookie");
  const jsonBody = Boolean(op.body && op.body.contentType.includes("json"));

  const send = useCallback(async () => {
    const issues = [...validateParams(op, values)];
    if (jsonBody) {
      const err = validateJsonBody(body, Boolean(op.body?.required));
      if (err) issues.push(err);
    }
    setProblems(issues);
    if (issues.length) {
      setPhase({ kind: "idle" });
      return;
    }
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      c.abort();
    }, timeoutS * 1000);
    const t0 = performance.now();
    setPhase({ kind: "sending", startedAt: t0 });
    try {
      const res = await executeRequest(op.method, fullPath, jsonBody ? body : undefined, op.body?.contentType, c.signal);
      setPhase({ kind: "done", res });
      onHistory(recordHistory({ method: op.method, path: pathOnly, query, status: res.status, ms: res.ms, op: op.id, body: jsonBody ? body : undefined }));
    } catch (e) {
      const ms = performance.now() - t0;
      const aborted = e instanceof DOMException && e.name === "AbortError";
      const message = aborted
        ? timedOut
          ? `Przekroczono limit czasu (${timeoutS} s). Żądanie przerwano po stronie przeglądarki; serwer mógł je mimo to wykonać.`
          : "Żądanie przerwane."
        : "Brak połączenia z lokalnym API (127.0.0.1). Sprawdź, czy serwer działa.";
      setPhase({ kind: "error", message, ms });
      onHistory(recordHistory({ method: op.method, path: pathOnly, query, status: null, ms, op: op.id }));
    } finally {
      window.clearTimeout(timer);
      if (ctrl.current === c) ctrl.current = null;
    }
  }, [op, values, body, jsonBody, timeoutS, fullPath, pathOnly, query, onHistory]);

  const request = useCallback(() => {
    if (op.streaming) return;
    if (phase.kind === "sending") return;
    if (unsafe && phase.kind !== "confirm") {
      const issues = validateParams(op, values);
      setProblems(issues);
      if (issues.length === 0) setPhase({ kind: "confirm" });
      return;
    }
    void send();
  }, [op, phase.kind, unsafe, send, values]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        request();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [request]);

  return (
    <article className="surface-card min-w-0 space-y-5 p-4 sm:p-5" aria-labelledby="console-op-title" data-testid="console-operation">
      <header className="min-w-0 space-y-1.5">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <MethodPill method={op.method} />
          <h3 id="console-op-title" className="min-w-0 font-mono text-sm font-semibold break-all text-fg">
            {op.path}
          </h3>
          {op.deprecated && <span className="rounded-full border border-warn/40 bg-warn/10 px-2 text-xs text-warn">przestarzały</span>}
        </div>
        {op.summary && <p className="text-sm font-medium text-fg">{op.summary}</p>}
        {op.description && <p className="text-sm whitespace-pre-line text-muted">{op.description}</p>}
        <p className="font-mono text-[11px] break-all text-subtle">operationId: {op.id}</p>
      </header>

      {op.streaming && (
        <div role="note" className="rounded-lg border border-info/40 bg-info/10 px-3 py-2 text-sm text-fg" data-testid="console-streaming-note">
          To jest strumień zdarzeń (Server-Sent Events): połączenie pozostaje otwarte i serwer dosyła kolejne zdarzenia. Konsola go nie
          wysyła, bo żądanie nigdy by się nie zakończyło. Użyj <code className="font-mono">curl -N</code> lub <code className="font-mono">EventSource</code> z
          fragmentów poniżej; wznowienie po zerwaniu: nagłówek <code className="font-mono">Last-Event-ID</code>. Opis zdarzeń w zakładce Dokumentacja.
        </div>
      )}

      {formParams.length > 0 && (
        <Section title="Parametry">
          <div className="space-y-3">
            {formParams.map((p) => (
              <ParamField key={`${p.in}:${p.name}`} p={p} value={values[`${p.in}:${p.name}`] ?? ""} onChange={(v) => setValues((s) => ({ ...s, [`${p.in}:${p.name}`]: v }))} />
            ))}
          </div>
        </Section>
      )}
      {otherParams.length > 0 && (
        <p className="text-xs text-subtle">Nagłówki/ciasteczka zdefiniowane w schemacie ({otherParams.map((p) => p.name).join(", ")}) nie są wysyłane z konsoli.</p>
      )}

      {op.body && (
        <Section
          title={`Treść żądania (${op.body.contentType}${op.body.required ? ", wymagana" : ""})`}
          actions={
            jsonBody ? (
              <Button size="sm" variant="ghost" onClick={() => setBody(bodySkeleton(doc, op))}>
                Przywróć szkielet
              </Button>
            ) : undefined
          }
        >
          {jsonBody ? (
            <>
              <label htmlFor="console-body" className="sr-only">
                Treść JSON
              </label>
              <textarea
                id="console-body"
                value={body}
                onChange={(e) => setBody(e.target.value)}
                spellCheck={false}
                rows={Math.min(18, Math.max(5, body.split("\n").length + 1))}
                className="w-full min-w-0 rounded-lg border border-line-strong bg-raised p-3 font-mono text-xs text-fg focus-visible:outline-2 focus-visible:outline-accent"
                data-testid="console-body"
              />
              {op.body.schemaName && <p className="text-[11px] text-subtle">Schemat: {op.body.schemaName}</p>}
            </>
          ) : (
            <p className="text-sm text-muted">
              Ten endpoint przyjmuje <code className="font-mono">{op.body.contentType}</code> (np. plik). Konsola nie wysyła plików — użyj fragmentu curl
              (<code className="font-mono">--data-binary @plik</code> lub <code className="font-mono">-F</code>).
            </p>
          )}
        </Section>
      )}

      <Section title="Żądanie">
        <div className="flex min-w-0 items-start gap-2 rounded-lg border border-line bg-raised px-3 py-2">
          <MethodPill method={op.method} className="mt-px" />
          <code className="min-w-0 flex-1 font-mono text-xs break-all text-fg" data-testid="console-url">
            {snippetBase()}
            {fullPath}
          </code>
          <CopyButton text={`${snippetBase()}${fullPath}`} label="Kopiuj URL" />
        </div>
        {problems.length > 0 && (
          <ul role="alert" className="list-disc space-y-0.5 rounded-lg border border-warn/40 bg-warn/10 py-2 pr-3 pl-7 text-sm text-warn">
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        )}
        {!op.streaming && (
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={request} disabled={phase.kind === "sending" || phase.kind === "confirm"} data-testid="console-send">
              Wyślij
            </Button>
            <span className="text-xs text-subtle">
              <kbd className="rounded border border-line-strong px-1 font-mono text-[10px]">Ctrl</kbd>/<kbd className="rounded border border-line-strong px-1 font-mono text-[10px]">⌘</kbd>+
              <kbd className="rounded border border-line-strong px-1 font-mono text-[10px]">Enter</kbd>
            </span>
            <label className="ml-auto flex items-center gap-1.5 text-xs text-muted">
              Limit czasu
              <select
                value={timeoutS}
                onChange={(e) => setTimeoutS(Number(e.target.value) as (typeof TIMEOUTS)[number])}
                className="h-7 rounded-md border border-line-strong bg-panel px-1.5 text-xs text-fg focus-visible:outline-2 focus-visible:outline-accent"
              >
                {TIMEOUTS.map((t) => (
                  <option key={t} value={t}>
                    {t} s
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
        {phase.kind === "confirm" && (
          <div role="alertdialog" aria-labelledby="console-confirm-title" aria-describedby="console-confirm-desc" className="space-y-2 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2" data-testid="console-confirm">
            <p id="console-confirm-title" className="text-sm font-semibold text-fg">
              Potwierdź żądanie {op.method.toUpperCase()}
            </p>
            <p id="console-confirm-desc" className="text-sm break-words text-muted">
              To żądanie może zmienić stan lokalnej instancji HYDRA pod adresem <code className="font-mono break-all">{snippetBase()}</code> (np. zapisać
              recenzję, uruchomić zadanie albo wysłać dane do włączonego konektora). Nie jest to symulacja.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button autoFocus variant="danger" onClick={() => void send()} data-testid="console-confirm-send">
                Wyślij do lokalnej instancji
              </Button>
              <Button variant="ghost" onClick={() => setPhase({ kind: "idle" })}>
                Anuluj
              </Button>
            </div>
          </div>
        )}
        <ResponseView phase={phase} onAbort={() => ctrl.current?.abort()} />
      </Section>

      <Snippets op={op} path={fullPath} body={body} />
    </article>
  );
}

// ---------------------------------------------------------------------------
// Examples + history
// ---------------------------------------------------------------------------

function ExamplesPanel({ ops, onPick }: { ops: Operation[]; onPick: (ex: ConsoleExample, op: Operation) => void }) {
  return (
    <details className="surface-card min-w-0">
      <summary className="cursor-pointer px-4 py-2.5 text-sm font-semibold text-fg">
        Przykłady <span className="font-normal text-subtle">— gotowe żądania wypełniające formularz</span>
      </summary>
      <ul className="grid min-w-0 gap-2 px-3 pb-3 sm:grid-cols-2 xl:grid-cols-3" data-testid="console-examples">
        {CONSOLE_EXAMPLES.map((ex) => {
          const op = findOperation(ops, `${ex.method} ${ex.path}`);
          return (
            <li key={ex.id} className="min-w-0">
              <button
                type="button"
                disabled={!op}
                onClick={() => op && onPick(ex, op)}
                className="flex h-full w-full min-w-0 flex-col gap-1 rounded-lg border border-line bg-raised px-3 py-2 text-left transition-colors hover:bg-hover focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:bg-hover"
              >
                <span className="flex min-w-0 items-center gap-2">
                  <MethodPill method={ex.method} />
                  <span className="min-w-0 truncate text-sm font-medium text-fg">{ex.title}</span>
                </span>
                <span className="font-mono text-[11px] break-all text-subtle">{ex.path}</span>
                <span className="text-xs text-muted">{op ? ex.description : "Ten endpoint nie występuje w schemacie tej wersji API."}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </details>
  );
}

function HistoryPanel({ history, ops, onPick, onClear }: { history: HistoryEntry[]; ops: Operation[]; onPick: (h: HistoryEntry, op: Operation) => void; onClear: () => void }) {
  return (
    <details className="surface-card min-w-0">
      <summary className="cursor-pointer px-4 py-2.5 text-sm font-semibold text-fg">Historia żądań ({history.length})</summary>
      <div className="space-y-2 px-3 pb-3">
        <p className="text-xs text-subtle">
          Tylko w tej przeglądarce (localStorage), maks. 50. Zapisujemy metodę, ścieżkę, status i czas; nigdy nagłówków ani treści z polami typu
          secret/token/key/password.
        </p>
        {history.length === 0 ? (
          <p className="text-sm text-muted">Brak zapisanych żądań.</p>
        ) : (
          <ul className="divide-y divide-line rounded-lg border border-line" data-testid="console-history">
            {history.map((h, i) => {
              const op = (h.op && ops.find((o) => o.id === h.op)) || undefined;
              return (
                <li key={`${h.at}-${i}`} className="min-w-0">
                  <button
                    type="button"
                    disabled={!op}
                    onClick={() => op && onPick(h, op)}
                    className="flex w-full min-w-0 items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-hover focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-default"
                  >
                    <MethodPill method={h.method} />
                    <span className="min-w-0 flex-1 font-mono break-all text-fg">
                      {h.path}
                      {h.query ? `?${h.query}` : ""}
                    </span>
                    <span className="shrink-0 font-mono text-subtle">{h.status ?? "błąd"}</span>
                    <span className="shrink-0 font-mono text-subtle">{h.ms} ms</span>
                    <time className="hidden shrink-0 text-subtle sm:inline" dateTime={h.at}>
                      {new Date(h.at).toLocaleTimeString("pl-PL")}
                    </time>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        <Button size="sm" variant="secondary" onClick={onClear} disabled={history.length === 0}>
          Wyczyść historię
        </Button>
      </div>
    </details>
  );
}

function valuesFromHistory(op: Operation, h: HistoryEntry): Record<string, string> {
  const out: Record<string, string> = {};
  const tpl = op.path.split("/");
  const actual = h.path.split("/");
  tpl.forEach((seg, i) => {
    const m = /^\{(.+)\}$/.exec(seg);
    if (m && actual[i] !== undefined) out[`path:${m[1]}`] = decodeURIComponent(actual[i]!);
  });
  const qs = new URLSearchParams(h.query);
  const arrays: Record<string, string[]> = {};
  for (const [k, v] of qs) (arrays[k] ??= []).push(v);
  for (const [k, vs] of Object.entries(arrays)) if (!vs.includes("•••")) out[`query:${k}`] = vs.join(",");
  return out;
}

// ---------------------------------------------------------------------------
// Tab
// ---------------------------------------------------------------------------

export function ConsoleTab() {
  const { data: doc, ops, isLoading, error } = useOpenApi();
  const [params, setParams] = useSearchParams();
  const opParam = params.get("op");
  const [query, setQuery] = useState("");
  const [history, setHistory] = useState<HistoryEntry[]>(() => loadHistory());
  const [preset, setPreset] = useState<{ values?: Record<string, string>; body?: string; nonce: number } | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const paneRef = useRef<HTMLDivElement | null>(null);

  const selected = useMemo(() => findOperation(ops, opParam) ?? ops.find((o) => o.path === "/health") ?? ops[0], [ops, opParam]);

  const select = useCallback(
    (op: Operation, nextPreset: { values?: Record<string, string>; body?: string } | null = null) => {
      setPreset({ ...(nextPreset ?? {}), nonce: Date.now() });
      const next = new URLSearchParams(params);
      next.set("tab", "console");
      next.set("op", op.id);
      setParams(next, { replace: true });
      if (window.matchMedia?.("(max-width: 1023px)").matches) paneRef.current?.scrollIntoView({ block: "start" });
    },
    [params, setParams],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
      e.preventDefault();
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (isLoading) return <Skeleton label="Wczytywanie schematu OpenAPI…" lines={6} />;
  if (error || !doc) return <ApiErrorState error={error} resource="schemat OpenAPI lokalnego API" endpoint="GET /openapi.json" />;
  if (ops.length === 0) return <EmptyState title="Schemat OpenAPI nie zawiera endpointów" />;

  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="console-tab">
      <p className="max-w-3xl text-sm text-muted">
        Konsola wygenerowana z lokalnego schematu <code className="font-mono">/openapi.json</code> ({doc.info?.title ?? "HYDRA"} {doc.info?.version ?? ""},{" "}
        {ops.length} operacji). Żądania trafiają wyłącznie do tej instancji pod <code className="font-mono break-all">{snippetBase()}</code>; API przyjmuje tylko
        połączenia z pętli zwrotnej i wymaga zalogowania (sesja przeglądarki albo token API).
      </p>
      <ConsoleAuthPanel />
      <ExamplesPanel
        ops={ops}
        onPick={(ex, op) => select(op, { values: ex.values, body: ex.body === undefined ? undefined : JSON.stringify(ex.body, null, 2) })}
      />
      <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(17rem,22rem)_minmax(0,1fr)] lg:items-start">
        <div className="min-w-0 lg:sticky lg:top-4">
          <OperationList ops={ops} selected={selected} onSelect={(op) => select(op)} query={query} setQuery={setQuery} searchRef={searchRef} />
        </div>
        <div ref={paneRef} className="min-w-0 scroll-mt-4 space-y-4">
          {selected && <OperationPane key={`${selected.id}-${preset?.nonce ?? 0}`} doc={doc} op={selected} preset={preset} onHistory={setHistory} />}
          <HistoryPanel
            history={history}
            ops={ops}
            onPick={(h, op) => select(op, { values: valuesFromHistory(op, h), body: h.body })}
            onClear={() => {
              clearHistory();
              setHistory([]);
            }}
          />
        </div>
      </div>
    </div>
  );
}
