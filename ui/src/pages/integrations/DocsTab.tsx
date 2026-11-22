/**
 * "Dokumentacja" (/integrations?tab=docs): searchable Polish documentation for integrations and the local API.
 * Static text lives in docs/docsContent.ts (exported as data for docs/INTEGRATIONS.md, docs/API.md); the API
 * reference is generated from /openapi.json, connector guides from GET /v1/integrations and the webhook event
 * catalogue from GET /v1/integrations/webhooks (static contract fallback when that router is not available).
 * Table of contents: sticky on desktop, collapsible on phones. Every code block has a copy button.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import { apiFetch } from "../../api/client";
import { Skeleton } from "../../components/Skeleton";
import { cx } from "../../lib/cx";
import {
  exampleFromSchema,
  filterOperations,
  groupByTag,
  normalizeText,
  schemaFields,
  schemaTypeLabel,
  type OpenApiDoc,
  type Operation,
} from "../../lib/openapi";
import { curlSnippet } from "../../lib/snippets";
import { CodeBlock, MethodPill } from "./console/CodeBlock";
import { snippetBase, useOpenApi } from "./console/useOpenApi";
import { DOC_SECTIONS, WEBHOOK_EVENTS_FALLBACK, sectionText, type DocBlock, type DocSection } from "./docs/docsContent";

// Minimal shapes this tab reads (full types: ui/src/api/integrations.ts, owned by the hub).
interface ConnectorDoc {
  id: string;
  name: string;
  category?: string;
  level?: string;
  auth?: string;
  description_pl?: string;
  setup_pl?: string[];
  notes_pl?: string;
  docs_url?: string;
  licence_pl?: string;
  rate_limit_pl?: string;
  data_out?: string[];
  data_out_pl?: string;
  never_sent_pl?: string;
  formats?: string[];
  secrets?: { name: string }[];
}
interface IntegrationsIndexDoc {
  connectors?: ConnectorDoc[];
  categories?: { id: string; label_pl: string }[];
}
interface WebhooksDoc {
  event_types?: { id: string; label_pl?: string; description_pl?: string; example?: unknown }[];
}

const LEVEL_PL: Record<string, string> = { live: "Na żywo (live)", "export-format": "Format eksportu", planned: "Planowane" };
const LEVEL_TONE: Record<string, string> = {
  live: "border-ok/40 bg-ok/10 text-ok",
  "export-format": "border-info/40 bg-info/10 text-info",
  planned: "border-line-strong bg-hover text-muted",
};

/** `inline code` → <code>. */
export function Inline({ text }: { text: string }) {
  const parts = text.split(/(`[^`]+`)/g);
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith("`") && p.endsWith("`") && p.length > 1 ? (
          <code key={i} className="rounded bg-hover px-1 py-px font-mono text-[0.85em] break-words text-fg">
            {p.slice(1, -1)}
          </code>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
  );
}

function Table({ head, rows }: { head: ReactNode[]; rows: ReactNode[][] }) {
  return (
    <div className="min-w-0 overflow-x-auto rounded-lg border border-line" tabIndex={0} role="region" aria-label="Tabela">
      <table className="w-full min-w-[32rem] border-collapse text-left text-xs">
        <thead className="bg-raised text-subtle">
          <tr>
            {head.map((h, i) => (
              <th key={i} scope="col" className="border-b border-line px-3 py-1.5 font-semibold">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-line last:border-b-0 align-top">
              {r.map((c, j) => (
                <td key={j} className="px-3 py-1.5 break-words text-fg">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Block({ b, dynamic }: { b: DocBlock; dynamic: (kind: Extract<DocBlock, { t: "dynamic" }>["kind"]) => ReactNode }) {
  switch (b.t) {
    case "p":
      return (
        <p className="text-sm leading-relaxed text-fg">
          <Inline text={b.text} />
        </p>
      );
    case "h":
      return (
        <h4 id={b.id ? `doc-${b.id}` : undefined} className="scroll-mt-4 pt-2 text-sm font-semibold text-fg">
          {b.text}
        </h4>
      );
    case "list": {
      const Tag = b.ordered ? "ol" : "ul";
      return (
        <Tag className={cx("space-y-1 pl-5 text-sm leading-relaxed text-fg", b.ordered ? "list-decimal" : "list-disc")}>
          {b.items.map((it, i) => (
            <li key={i}>
              <Inline text={it} />
            </li>
          ))}
        </Tag>
      );
    }
    case "code":
      return <CodeBlock code={b.code} label={b.label ?? b.lang} maxHeight="24rem" testId="doc-code" />;
    case "table":
      return <Table head={b.head} rows={b.rows.map((r) => r.map((c, i) => <Inline key={i} text={c} />))} />;
    case "note":
      return (
        <div role="note" className={cx("rounded-lg border px-3 py-2 text-sm", b.tone === "warn" ? "border-warn/40 bg-warn/10 text-fg" : "border-info/40 bg-info/10 text-fg")}>
          <Inline text={b.text} />
        </div>
      );
    case "dynamic":
      return <>{dynamic(b.kind)}</>;
  }
}

// ---------------------------------------------------------------------------
// API reference (generated)
// ---------------------------------------------------------------------------

function EndpointDoc({ doc, op, onOpen, open }: { doc: OpenApiDoc; op: Operation; onOpen: (op: Operation) => void; open: boolean }) {
  const fields = op.body ? schemaFields(doc, op.body.schema) : [];
  const example = op.body?.contentType.includes("json") ? JSON.stringify(exampleFromSchema(doc, op.body.schema)) : undefined;
  const ok = op.responses.find((r) => r.status.startsWith("2"));
  const okFields = ok ? schemaFields(doc, ok.schema) : [];
  const curl = curlSnippet({ method: op.method, baseUrl: snippetBase(), path: op.path, body: example, contentType: op.body?.contentType, streaming: op.streaming });
  return (
    <details id={`api-${op.id}`} open={open} className="group scroll-mt-4 rounded-lg border border-line bg-panel" data-testid="api-endpoint">
      <summary className="flex min-w-0 cursor-pointer items-start gap-2 px-3 py-2 text-xs hover:bg-hover">
        <MethodPill method={op.method} className="mt-px" />
        <span className="min-w-0 flex-1">
          <span className={cx("block font-mono break-all text-fg", op.deprecated && "line-through")}>{op.path}</span>
          {op.summary && <span className="block text-subtle">{op.summary}</span>}
        </span>
        {op.streaming && <span className="shrink-0 rounded-full border border-info/40 px-1.5 text-[10px] text-info">SSE</span>}
      </summary>
      <div className="min-w-0 space-y-3 border-t border-line px-3 py-3">
        {op.description && <p className="text-sm whitespace-pre-line text-fg">{op.description}</p>}
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => onOpen(op)}
            className="inline-flex h-6 items-center rounded-full border border-accent/50 bg-accent-soft px-2.5 text-xs font-medium text-fg hover:bg-hover focus-visible:outline-2 focus-visible:outline-accent"
          >
            Otwórz w konsoli
          </button>
          <a href={`#api-${op.id}`} className="font-mono text-[11px] break-all text-subtle hover:text-fg">
            #{op.id}
          </a>
        </div>
        {op.params.length > 0 && (
          <Table
            head={["Parametr", "Gdzie", "Typ", "Wymagany", "Opis"]}
            rows={op.params.map((p) => [
              <code key="n" className="font-mono">{p.name}</code>,
              p.in === "path" ? "ścieżka" : p.in === "query" ? "zapytanie" : p.in,
              <span key="t" className="font-mono">{p.typeLabel}</span>,
              p.required ? "tak" : "nie",
              [p.description, p.default !== undefined && p.default !== null ? `domyślnie ${JSON.stringify(p.default)}` : ""].filter(Boolean).join(" · "),
            ])}
          />
        )}
        {op.body && (
          <div className="space-y-1">
            <h5 className="text-xs font-semibold text-subtle">
              Treść żądania: <span className="font-mono">{op.body.contentType}</span>
              {op.body.schemaName ? ` (${op.body.schemaName})` : ""}
              {op.body.required ? ", wymagana" : ""}
            </h5>
            {fields.length > 0 && (
              <Table head={["Pole", "Typ", "Wymagane", "Opis"]} rows={fields.map((f) => [<code key="n" className="font-mono">{f.name}</code>, <span key="t" className="font-mono">{f.type}</span>, f.required ? "tak" : "nie", f.description])} />
            )}
          </div>
        )}
        <div className="space-y-1">
          <h5 className="text-xs font-semibold text-subtle">Odpowiedzi</h5>
          <ul className="space-y-0.5 text-xs text-fg">
            {op.responses.map((r) => (
              <li key={r.status} className="break-words">
                <span className="font-mono font-semibold">{r.status}</span> {r.description}
                {r.contentType && <span className="text-subtle"> · {r.contentType}</span>}
                {r.schema && <span className="font-mono text-subtle"> · {schemaTypeLabel(doc, r.schema)}</span>}
              </li>
            ))}
          </ul>
          {okFields.length > 0 && <p className="text-xs break-words text-subtle">Pola odpowiedzi: {okFields.map((f) => f.name).join(", ")}</p>}
        </div>
        <CodeBlock code={curl} label="Przykład (curl)" maxHeight="14rem" />
      </div>
    </details>
  );
}

function ApiReference({ query, onOpen }: { query: string; onOpen: (op: Operation) => void }) {
  const { data: doc, ops, isLoading, error } = useOpenApi();
  const filtered = useMemo(() => filterOperations(ops, query), [ops, query]);
  const groups = useMemo(() => groupByTag(filtered), [filtered]);
  if (isLoading) return <Skeleton label="Wczytywanie schematu OpenAPI…" lines={3} />;
  if (error || !doc) return <p className="text-sm text-warn">Nie udało się wczytać `/openapi.json` z lokalnego API — referencja niedostępna.</p>;
  const searching = query.trim().length > 0;
  return (
    <div className="min-w-0 space-y-2" data-testid="api-reference">
      <p className="text-xs text-subtle">
        {doc.info?.title} {doc.info?.version} · {searching ? `${filtered.length} z ${ops.length}` : ops.length} endpointów
      </p>
      {groups.map((g) => (
        <details key={g.tag} id={`api-tag-${g.tag}`} open={searching} className="min-w-0 scroll-mt-4 rounded-lg border border-line bg-raised">
          <summary className="cursor-pointer px-3 py-2 text-sm font-semibold text-fg">
            {g.label} <span className="font-normal text-subtle">({g.operations.length})</span>
          </summary>
          <div className="min-w-0 space-y-1.5 px-2 pb-2">
            {g.operations.map((op) => (
              <EndpointDoc key={op.id} doc={doc} op={op} onOpen={onOpen} open={searching && filtered.length <= 3} />
            ))}
          </div>
        </details>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Connectors + webhook events (live)
// ---------------------------------------------------------------------------

function useIntegrationsDoc() {
  return useQuery({
    queryKey: ["integrations", "docs", "index"],
    queryFn: ({ signal }) => apiFetch<IntegrationsIndexDoc>("/v1/integrations", { signal }),
    retry: false,
    staleTime: 60_000,
  });
}

function useWebhooksDoc() {
  return useQuery({
    queryKey: ["integrations", "docs", "webhooks"],
    queryFn: ({ signal }) => apiFetch<WebhooksDoc>("/v1/integrations/webhooks", { signal }),
    retry: false,
    staleTime: 60_000,
  });
}

function connectorText(c: ConnectorDoc): string {
  return [c.name, c.id, c.description_pl, c.notes_pl, c.licence_pl, c.rate_limit_pl, c.data_out_pl, ...(c.setup_pl ?? [])].join(" ");
}

function ConnectorGuides({ query }: { query: string }) {
  const q = useIntegrationsDoc();
  if (q.isLoading) return <Skeleton label="Wczytywanie konektorów…" lines={2} />;
  const all = q.data?.connectors ?? [];
  if (q.error || all.length === 0)
    return (
      <p role="note" className="rounded-lg border border-line bg-raised px-3 py-2 text-sm text-muted">
        Lista konektorów niedostępna w tej instancji (<code className="font-mono">GET /v1/integrations</code>). Przewodnik Meta Ad Library poniżej obowiązuje niezależnie.
      </p>
    );
  const terms = normalizeText(query).split(/\s+/).filter(Boolean);
  const list = terms.length ? all.filter((c) => terms.every((t) => normalizeText(connectorText(c)).includes(t))) : all;
  const cats = new Map((q.data?.categories ?? []).map((c) => [c.id, c.label_pl]));
  return (
    <div className="min-w-0 space-y-3" data-testid="connector-guides">
      <Table
        head={["Konektor", "Poziom", "Uwierzytelnianie", "Dane wychodzące", "Limity"]}
        rows={list.map((c) => [
          <a key="n" href={`#conn-${c.id}`} className="font-medium text-accent hover:underline">{c.name}</a>,
          LEVEL_PL[c.level ?? ""] ?? c.level ?? "—",
          c.auth ?? "—",
          c.data_out && c.data_out.length ? c.data_out.join(", ") : "nic",
          c.rate_limit_pl ?? "—",
        ])}
      />
      {list.map((c) => (
        <details key={c.id} id={`conn-${c.id}`} className="min-w-0 scroll-mt-4 rounded-lg border border-line bg-panel">
          <summary className="flex min-w-0 cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-sm">
            <span className="font-semibold text-fg">{c.name}</span>
            {c.level && <span className={cx("rounded-full border px-2 text-[11px]", LEVEL_TONE[c.level] ?? LEVEL_TONE.planned)}>{LEVEL_PL[c.level] ?? c.level}</span>}
            {c.category && <span className="text-xs text-subtle">{cats.get(c.category) ?? c.category}</span>}
          </summary>
          <div className="min-w-0 space-y-2 border-t border-line px-3 py-3 text-sm text-fg">
            {c.description_pl && (
              <p>
                <Inline text={c.description_pl} />
              </p>
            )}
            {c.setup_pl && c.setup_pl.length > 0 && (
              <>
                <h5 className="text-xs font-semibold text-subtle">Konfiguracja</h5>
                <ol className="list-decimal space-y-0.5 pl-5">
                  {c.setup_pl.map((s, i) => (
                    <li key={i}>
                      <Inline text={s} />
                    </li>
                  ))}
                </ol>
              </>
            )}
            <dl className="grid min-w-0 gap-x-3 gap-y-1 text-xs sm:grid-cols-[10rem_minmax(0,1fr)]">
              <dt className="text-subtle">Uwierzytelnianie</dt>
              <dd className="break-words">
                {c.auth ?? "—"}
                {c.secrets && c.secrets.length > 0 && <> ({c.secrets.map((s) => s.name).join(", ")})</>}
              </dd>
              <dt className="text-subtle">Limity</dt>
              <dd className="break-words">{c.rate_limit_pl || "—"}</dd>
              <dt className="text-subtle">Licencja / warunki</dt>
              <dd className="break-words">{c.licence_pl || "—"}</dd>
              <dt className="text-subtle">Co wychodzi</dt>
              <dd className="break-words">{c.data_out_pl || (c.data_out?.length ? c.data_out.join(", ") : "nic")}</dd>
              <dt className="text-subtle">Nigdy nie wychodzi</dt>
              <dd className="break-words">{c.never_sent_pl || "—"}</dd>
              {c.formats && c.formats.length > 0 && (
                <>
                  <dt className="text-subtle">Formaty</dt>
                  <dd className="break-words font-mono">{c.formats.join(", ")}</dd>
                </>
              )}
              {c.notes_pl && (
                <>
                  <dt className="text-subtle">Uwagi</dt>
                  <dd className="break-words">
                    <Inline text={c.notes_pl} />
                  </dd>
                </>
              )}
              {c.docs_url && (
                <>
                  <dt className="text-subtle">Dokumentacja dostawcy</dt>
                  <dd className="break-all">
                    <a href={c.docs_url} target="_blank" rel="noreferrer noopener" className="text-accent hover:underline">
                      {c.docs_url}
                    </a>
                  </dd>
                </>
              )}
            </dl>
          </div>
        </details>
      ))}
    </div>
  );
}

function WebhookEvents() {
  const q = useWebhooksDoc();
  const live = q.data?.event_types && q.data.event_types.length > 0 ? q.data.event_types : null;
  const events = live ?? WEBHOOK_EVENTS_FALLBACK;
  return (
    <div className="min-w-0 space-y-2" data-testid="webhook-events">
      {!live && !q.isLoading && <p className="text-xs text-subtle">Katalog według kontraktu integracji (lista z API niedostępna w tej instancji).</p>}
      <Table
        head={["Zdarzenie", "Nazwa", "Kiedy"]}
        rows={events.map((e) => [<code key="i" className="font-mono">{e.id}</code>, e.label_pl ?? "", e.description_pl ?? ""])}
      />
      {events
        .filter((e) => e.example !== undefined)
        .map((e) => (
          <CodeBlock key={e.id} label={`Przykład: ${e.id}`} maxHeight="14rem" code={JSON.stringify({ id: "dlv-…", event: e.id, created_at: "2026-11-22T03:00:00Z", data: e.example }, null, 2)} />
        ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab
// ---------------------------------------------------------------------------

function sectionMatches(section: DocSection, query: string): boolean {
  const terms = normalizeText(query).split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  const hay = normalizeText(sectionText(section));
  return terms.every((t) => hay.includes(t));
}

function scrollToId(id: string) {
  const el = document.getElementById(id);
  if (!el) return;
  const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  if (el instanceof HTMLDetailsElement) el.open = true;
  try {
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.search}#${id}`);
  } catch {
    /* ignore */
  }
}

export function DocsTab() {
  const [query, setQuery] = useState("");
  const [tocOpen, setTocOpen] = useState(false);
  const [params, setParams] = useSearchParams();
  const { ops } = useOpenApi();
  const integ = useIntegrationsDoc();

  // Deep link from the hub ("Jak zdobyć klucz"): ?doc=<element id> opens that guide (a <details>) and scrolls to it,
  // once the dynamic connector guides have rendered.
  const docTarget = params.get("doc");
  useEffect(() => {
    if (!docTarget || !/^[a-z0-9-]{1,80}$/.test(docTarget)) return;
    const el = document.getElementById(docTarget);
    if (!el) return;
    if (el instanceof HTMLDetailsElement) el.open = true;
    el.scrollIntoView?.({ block: "start" });
    const focusable = el instanceof HTMLDetailsElement ? el.querySelector("summary") : el;
    (focusable as HTMLElement | null)?.focus?.({ preventScroll: true });
  }, [docTarget, integ.data]);

  const apiHits = useMemo(() => (query.trim() ? filterOperations(ops, query).length : ops.length), [ops, query]);
  const connectorHits = useMemo(() => {
    const terms = normalizeText(query).split(/\s+/).filter(Boolean);
    return (integ.data?.connectors ?? []).filter((c) => terms.every((t) => normalizeText(connectorText(c)).includes(t))).length;
  }, [integ.data, query]);

  const visible = DOC_SECTIONS.filter((s) => sectionMatches(s, query) || (s.id === "api" && apiHits > 0 && query.trim() !== "") || (s.id === "konektory" && connectorHits > 0 && query.trim() !== ""));

  const openInConsole = (op: Operation) => {
    const next = new URLSearchParams(params);
    next.set("tab", "console");
    next.set("op", op.id);
    setParams(next);
    if (!navigator.userAgent.includes("jsdom")) window.scrollTo({ top: 0 });
  };

  const dynamic = (kind: Extract<DocBlock, { t: "dynamic" }>["kind"]) => {
    if (kind === "api-reference") return <ApiReference query={query} onOpen={openInConsole} />;
    if (kind === "connectors") return <ConnectorGuides query={query} />;
    return <WebhookEvents />;
  };

  const toc = (
    <ol className="space-y-0.5 text-sm">
      {DOC_SECTIONS.map((s, i) => {
        const shown = visible.includes(s);
        return (
          <li key={s.id}>
            <a
              href={`#doc-${s.id}`}
              onClick={(e) => {
                e.preventDefault();
                setTocOpen(false);
                scrollToId(`doc-${s.id}`);
              }}
              className={cx("block rounded-md px-2 py-1 hover:bg-hover focus-visible:outline-2 focus-visible:outline-accent", shown ? "text-fg" : "text-subtle line-through")}
              data-testid={`toc-${s.id}`}
            >
              {i + 1}. {s.title}
            </a>
          </li>
        );
      })}
    </ol>
  );

  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="docs-tab">
      <div className="surface-card min-w-0 p-3">
        <label htmlFor="docs-search" className="mb-1 block text-xs font-medium text-muted">
          Szukaj w dokumentacji
        </label>
        <input
          id="docs-search"
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="np. HMAC, STIX, token Meta, Last-Event-ID"
          autoComplete="off"
          className="h-8 w-full rounded-md border border-line-strong bg-panel px-2 text-sm text-fg placeholder:text-subtle focus-visible:outline-2 focus-visible:outline-accent"
          data-testid="docs-search"
        />
        <p className="mt-1 text-xs text-subtle" aria-live="polite" data-testid="docs-count">
          {query.trim() ? `Pasujące sekcje: ${visible.length} z ${DOC_SECTIONS.length}` : `${DOC_SECTIONS.length} sekcji`}
        </p>
      </div>

      <div className="grid min-w-0 gap-4 lg:grid-cols-[15rem_minmax(0,1fr)] lg:items-start">
        <nav aria-label="Spis treści" className="surface-card min-w-0 p-2 lg:sticky lg:top-4">
          <button
            type="button"
            aria-expanded={tocOpen}
            aria-controls="docs-toc"
            onClick={() => setTocOpen((v) => !v)}
            className="flex w-full items-center justify-between rounded-md px-2 py-1 text-sm font-semibold text-fg hover:bg-hover focus-visible:outline-2 focus-visible:outline-accent lg:hidden"
          >
            Spis treści <span aria-hidden="true">{tocOpen ? "▴" : "▾"}</span>
          </button>
          <p className="hidden px-2 pb-1 text-xs font-semibold tracking-wide text-subtle uppercase lg:block">Spis treści</p>
          <div id="docs-toc" className={cx("lg:block", tocOpen ? "block pt-1" : "hidden")}>
            {toc}
          </div>
        </nav>

        <div className="min-w-0 space-y-4">
          {visible.length === 0 && (
            <p role="status" className="surface-card p-4 text-sm text-muted">
              Brak wyników dla „{query}”.
            </p>
          )}
          {visible.map((s) => (
            <section key={s.id} id={`doc-${s.id}`} aria-labelledby={`doc-${s.id}-title`} className="surface-card min-w-0 scroll-mt-4 space-y-3 p-4 sm:p-5" data-testid={`doc-section-${s.id}`}>
              <header className="space-y-0.5">
                <h3 id={`doc-${s.id}-title`} className="text-base font-semibold text-fg">
                  <a href={`#doc-${s.id}`} onClick={(e) => { e.preventDefault(); scrollToId(`doc-${s.id}`); }} className="hover:underline">
                    {s.title}
                  </a>
                </h3>
                <p className="text-sm text-muted">
                  <Inline text={s.summary} />
                </p>
              </header>
              {s.blocks.map((b, i) => (
                <Block key={i} b={b} dynamic={dynamic} />
              ))}
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
