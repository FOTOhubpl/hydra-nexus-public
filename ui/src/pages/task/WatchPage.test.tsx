import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { watchRtBus, type RtItem, type RtMatch, type RtSource, type RtStatus } from "../../api/watchRt";
import { WatchPage } from "./WatchPage";
import { countdown } from "../../components/watch/parts";
import { itemMatchesFilters } from "../../components/watch/Feed";

const minute = (i: number) => `2026-11-22T0${Math.floor(i / 60) + 1}:${String(i % 60).padStart(2, "0")}`;
const src = (over: Partial<RtSource> = {}): RtSource => ({
  id: "urlhaus", name_pl: "URLhaus (abuse.ch)", enabled: true, state: "ok", state_pl: "działa", interval_s: 300, min_interval_s: 60,
  next_due: null, next_due_in_s: 42, last_poll: "2026-11-22T02:00:00Z", last_ok: "2026-11-22T02:00:00Z", last_latency_ms: 183,
  last_error_pl: null, consecutive_errors: 0, total_polls: 12, total_items: 140, rate_limit: { limit_per_min: 10, used_60s: 3 },
  sends_pl: "Tylko zapytanie GET o publiczny plik z ostatnimi adresami; nie wysyłamy żadnych danych sprawy.", connector_id: "urlhaus", ...over,
});
const status = (over: Partial<RtStatus["engine"]> = {}, sources: RtSource[] = [src(), src({ id: "crtsh", name_pl: "crt.sh", state: "backoff", state_pl: "opóźnienie po błędach", consecutive_errors: 3, last_error_pl: "Przekroczono czas odpowiedzi (10 s)." })]): RtStatus => ({
  engine: { state: "offline", state_pl: "offline", message_pl: "Tryb offline (HYDRA_OFFLINE=1): źródła nie są odpytywane.", started_at: null, now: null, replay: null, ...over },
  sources,
  totals: { items: 140, items_24h: 31, matches: 2, matches_new: 1, watchlist: 57 },
  sparkline: Array.from({ length: 60 }, (_, i) => ({ minute: minute(i), fetched: i % 3, new: i % 2, matched: i === 59 ? 1 : 0 })),
  timeline: Array.from({ length: 48 }, (_, i) => ({ hour: `2026-11-2${i < 24 ? 0 : 1}T${String(i % 24).padStart(2, "0")}`, new: i, matched: i % 5 === 0 ? 1 : 0 })),
});
const item = (id: number, over: Partial<RtItem> = {}): RtItem => ({
  id, source: "urlhaus", external_id: `u${id}`, kind: "url", value_defanged: `hxxps://example-${id}[.]top/login`, first_seen: "2026-11-22T02:00:00Z",
  last_seen: null, seen_count: 1, indicators: [{ kind: "domain", value_display: `example-${id}[.]top` }], match_count: 0, replay: false, ...over,
});
const match = (over: Partial<RtMatch> = {}): RtMatch => ({
  id: 5, item_id: 2, source: "urlhaus", indicator_kind: "domain", indicator_display: "example-2[.]top", watch_id: 9, origin: "campaign",
  campaign_id: "C-7", campaign_label: "Inwestycja PKO Bonus", run_id: "r1", incident_ids: ["nask-a1"], strength: "strong", bits: 7.25, df: 2,
  n_incidents: 120, status: "new", created_at: "2026-11-22T02:00:00Z", updated_at: null, evidence: { rule: "domain_exact", watch_value: "example-2.top" },
  assign: null, item: { value_defanged: "hxxps://example-2[.]top/login", first_seen: "2026-11-22T02:00:00Z" }, replay: false, ...over,
});

interface Call { url: string; method: string; body: unknown }

function setup(opts: { status?: RtStatus; poll409?: boolean; add422?: boolean; pollRun?: Record<string, unknown> } = {}) {
  const calls: Call[] = [];
  let st = opts.status ?? status();
  const fn = mockFetch(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, method, body });
    const path = url.replace(/^\/api/, "").split("?")[0]!;
    if (path === "/v1/watch/rt/status") return jsonResponse(st);
    if (path === "/v1/watch/rt/items") {
      const matched = new URL(url, "http://x").searchParams.get("matched");
      const all = [item(3), item(2, { match_count: 1 }), item(1, { source: "crtsh", kind: "cert", value_defanged: "pko-bonus[.]top", replay: true })];
      return jsonResponse({ items: matched === "1" ? all.filter((i) => i.match_count > 0) : all, next_before: null });
    }
    if (path === "/v1/watch/rt/matches") return jsonResponse({ matches: [match()] });
    if (path === "/v1/watch/rt/matches/5" && method === "POST") return jsonResponse(match({ status: (body as { status: string }).status }));
    if (path === "/v1/watch/rt/matches/5/incident") return jsonResponse({ match: match(), assign: { decision: "new_campaign", campaign_id: null, new_pattern_id: "P-0003", batch_run: "b1", message_pl: "Utworzono incydent i uruchomiono przypisanie." } });
    if (path === "/v1/watch/rt/watchlist" && method === "POST") {
      if (opts.add422) return jsonResponse({ detail: { message_pl: "Niepoprawny IBAN (suma kontrolna)." } }, { status: 422 });
      return jsonResponse({ id: 99, kind: (body as { kind: string }).kind, value_display: "nowa[.]pl", origin: "manual", label: null, hits: 0 });
    }
    if (path === "/v1/watch/rt/watchlist/rebuild") return jsonResponse({ added: 4, removed: 1, total: 60 });
    if (path.startsWith("/v1/watch/rt/watchlist/") && method === "DELETE") return new Response(null, { status: 204 });
    if (path === "/v1/watch/rt/watchlist") {
      return jsonResponse({
        entries: [
          { id: 1, kind: "domain", value_display: "pko-bonus[.]top", origin: "campaign", campaign_id: "C-7", campaign_label: "Inwestycja PKO Bonus", label: null, hits: 2 },
          { id: 2, kind: "iban", value_display: "PL61 1090 1014 0000 0712 1981 2874", origin: "manual", campaign_id: null, label: "zgłoszenie 112", hits: 0 },
          { id: 3, kind: "keyword", value_display: "pko-bonus", origin: "replay", label: null, hits: 0 },
        ],
        counts: { domain: 1, iban: 1, keyword: 1 },
      });
    }
    if (path === "/v1/watch/rt/replay/fixtures") return jsonResponse({ fixtures: [{ name: "demo", description_pl: "Syntetyczny zestaw demonstracyjny.", ticks: 40, synthetic: true }] });
    if (path === "/v1/watch/rt/replay") {
      st = status({ state: "replay", replay: { fixture: "demo", speed: (body as { speed: number }).speed, synthetic: true, label_pl: "dane syntetyczne", ticks_total: 40, ticks_done: 0, done: false } });
      return jsonResponse(st);
    }
    if (path.startsWith("/v1/watch/rt/sources/") && path.endsWith("/poll")) {
      if (opts.poll409) return jsonResponse({ detail: { status: "offline", message_pl: "Tryb offline: odpytywanie źródeł jest wyłączone." } }, { status: 409 });
      return jsonResponse({ run: opts.pollRun ?? { id: 1, source: "urlhaus", started_at: "2026-11-22T02:00:00Z", fetched: 10, new: 2, matched: 0, replay: false } });
    }
    if (path.startsWith("/v1/watch/rt/sources/")) return jsonResponse(src({ enabled: (body as { enabled: boolean }).enabled, state: "disabled", state_pl: "wyłączone" }));
    if (path === "/v1/watch/rt/engine") return jsonResponse(status({ state: "paused", state_pl: "wstrzymany", message_pl: "Wstrzymano." }));
    // classic view
    if (path === "/v1/watch/status") return jsonResponse({ mode: "idle", replay: null, clock: null, events: 0, high: 0, share_high: null, a_cut_bits: 6, in_live_campaign: 0, assigned_known: 0, family_link_known: 0, campaign_gene_hits: 0, enriched: 0, pending: 0, live_campaigns: 0, live_enrichment: false, corpus: "nask", seeded_history: 0, campaign_index: { campaigns: 0, run_id: null, error: null }, top_families: [], top_live_campaigns: [], sparkline: [] });
    if (path === "/v1/watch/recordings") return jsonResponse({ recordings: [] });
    if (path === "/v1/watch/events") return jsonResponse({ replay: false, events: [] });
    return jsonResponse({ detail: "not found" }, { status: 404 });
  });
  return { calls, fn };
}

afterEach(() => { document.body.style.width = ""; });

describe("WatchPage (realtime)", () => {
  it("shows the engine state pill, the offline message, KPIs and one health card per source", async () => {
    setup();
    renderWithProviders(<WatchPage />, { route: "/watch" });
    const pill = await screen.findByTestId("engine-pill");
    expect(pill).toHaveTextContent("Offline");
    expect(screen.getByTestId("engine-message")).toHaveTextContent("HYDRA_OFFLINE=1");
    expect(screen.queryByTestId("replay-banner")).toBeNull();
    expect(screen.getByRole("tab", { name: "Na żywo" })).toHaveAttribute("aria-selected", "true");
    const card = screen.getByTestId("source-urlhaus");
    expect(within(card).getByText("183 ms")).toBeInTheDocument();
    expect(within(card).getByText(/3 \/ 10 na min/)).toBeInTheDocument();
    expect(within(card).getByText("co 5 min")).toBeInTheDocument();
    expect(within(card).getByTestId("source-next-urlhaus")).toHaveTextContent(/za 4\d s/);
    const crt = screen.getByTestId("source-crtsh");
    expect(within(crt).getByTestId("source-error-crtsh")).toHaveTextContent("Przekroczono czas odpowiedzi");
    expect(within(crt).getByText("3")).toHaveClass("text-err");
    expect(screen.getByText("Watchlista", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Ostatnie 48 godzin/ })).toBeInTheDocument();
    // the definition of what is sent appears on keyboard focus
    act(() => within(card).getByRole("button", { name: "Co jest wysyłane" }).focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("nie wysyłamy żadnych danych sprawy");
  });

  it("replay state shows the non-dismissable SYNTETYCZNE / ODTWORZENIE banner and the Odtworzenie pill", async () => {
    setup({ status: status({ state: "replay", state_pl: "odtworzenie", replay: { fixture: "demo", speed: 600, synthetic: true, label_pl: null, ticks_total: 40, ticks_done: 12, done: false } }) });
    renderWithProviders(<WatchPage />, { route: "/watch" });
    const banner = await screen.findByTestId("replay-banner");
    expect(banner).toHaveTextContent("SYNTETYCZNE / ODTWORZENIE — dane nie pochodzą z sieci na żywo");
    expect(banner).toHaveTextContent("prędkość 600×");
    expect(banner).toHaveTextContent("postęp 12/40");
    expect(within(banner).queryByRole("button")).toBeNull();
    expect(screen.getByTestId("engine-pill")).toHaveTextContent("Odtworzenie");
  });

  it("replay of a live recording (synthetic=false) is still labelled ODTWORZENIE, without SYNTETYCZNE", async () => {
    setup({ status: status({ state: "replay", replay: { fixture: "live-2026-11-22", speed: 60, synthetic: false, ticks_total: 5, ticks_done: 1, done: false } }) });
    renderWithProviders(<WatchPage />, { route: "/watch" });
    const banner = await screen.findByTestId("replay-banner");
    expect(banner).toHaveTextContent("ODTWORZENIE — nagranie, nie dane na żywo");
    expect(banner).not.toHaveTextContent("SYNTETYCZNE");
  });

  it("errorPl prefers detail.message_pl, then detail.message, and names a 403", async () => {
    const { errorPl } = await import("../../api/watchRt");
    const { ApiError } = await import("../../api/client");
    const e = (status: number, body: unknown) => new ApiError({ kind: "http", message: "x", path: "/p", status, body });
    expect(errorPl(e(409, { detail: { status: "offline", message_pl: "Tryb offline." } }))).toBe("Tryb offline.");
    expect(errorPl(e(403, { detail: { code: "forbidden", message: "Wymagana rola admin." } }))).toBe("Wymagana rola admin.");
    expect(errorPl(e(403, {}))).toBe("Brak uprawnień do tej operacji (wymagana wyższa rola).");
  });

  it.each([
    ["live", "Na żywo"], ["paused", "Wstrzymany"], ["stopped", "Wyłączony"],
  ] as const)("engine state %s is labelled %s", async (state, label) => {
    setup({ status: status({ state }) });
    renderWithProviders(<WatchPage />, { route: "/watch" });
    expect(await screen.findByTestId("engine-pill")).toHaveTextContent(label);
    expect(screen.getByRole("button", { name: "Wznów" })).toHaveProperty("disabled", state !== "paused");
    expect(screen.getByRole("button", { name: "Uruchom" })).toHaveProperty("disabled", state !== "stopped");
  });

  it("source card: enable toggle posts enabled=false, poll 409 shows the Polish message", async () => {
    const { calls } = setup({ poll409: true });
    renderWithProviders(<WatchPage />, { route: "/watch" });
    const card = await screen.findByTestId("source-urlhaus");
    const sw = within(card).getByRole("switch");
    expect(sw).toHaveAttribute("aria-checked", "true");
    fireEvent.click(sw);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/v1/watch/rt/sources/urlhaus") && c.method === "POST" && (c.body as { enabled: boolean }).enabled === false)).toBe(true));
    await waitFor(() => expect(within(screen.getByTestId("source-urlhaus")).getByRole("switch")).toHaveAttribute("aria-checked", "false"));
    fireEvent.click(within(screen.getByTestId("source-urlhaus")).getByRole("button", { name: "Odpytaj teraz" }));
    expect(await within(screen.getByTestId("source-urlhaus")).findByText("Tryb offline: odpytywanie źródeł jest wyłączone.")).toBeInTheDocument();
  });

  it("CERT card: unchanged list reads as 'Brak nowych wpisów' with file facts, hourly sparkline and the poll answer", async () => {
    const cert = src({
      id: "cert-pl", name_pl: "CERT Polska — lista ostrzeżeń", state: "no_new", state_pl: "Brak nowych wpisów", interval_s: 60,
      file_size: 31602124, server_last_modified: "Sun, 22 Nov 2026 03:00:31 GMT", newest_entry: "2026-11-22T02:01:55+00:00",
      last_new_at: "2026-11-22T01:47:00+00:00", new_24h: 111,
      hourly_new: Array.from({ length: 24 }, (_, i) => ({ hour: `2026-11-22T${String(i).padStart(2, "0")}`, new: i === 1 ? 111 : 0 })),
      summary_pl: "Lista CERT bez nowych wpisów od 02:47 (ostatni wpis 03:01:55 czasu PL). Plik sprawdzany co minutę.",
    });
    setup({
      status: status({ state: "live" }, [cert]),
      pollRun: { id: 9, source: "cert-pl", started_at: "2026-11-22T03:05:00Z", fetched: 0, new: 0, matched: 0, not_modified: true, replay: false,
        result_pl: "Sprawdzono: brak nowych wpisów (plik bez zmian, 31,6 MB)." },
    });
    renderWithProviders(<WatchPage />, { route: "/watch" });
    const card = await screen.findByTestId("source-cert-pl");
    expect(within(card).getByTestId("source-state-cert-pl")).toHaveTextContent("Brak nowych wpisów");
    expect(within(card).getByTestId("source-summary-cert-pl")).toHaveTextContent("Lista CERT bez nowych wpisów od 02:47 (ostatni wpis 03:01:55 czasu PL). Plik sprawdzany co minutę.");
    expect(within(card).getByText("31,6 MB")).toBeInTheDocument();
    expect(within(card).getByText("111")).toBeInTheDocument();
    expect(within(card).getByRole("img", { name: /nowe pozycje na godzinę/ })).toBeInTheDocument();
    fireEvent.click(within(card).getByRole("button", { name: "Odpytaj teraz" }));
    expect(await within(screen.getByTestId("source-cert-pl")).findByText("Sprawdzono: brak nowych wpisów (plik bez zmian, 31,6 MB).")).toBeInTheDocument();
  });

  it("engine button posts the action and shows the new state", async () => {
    const { calls } = setup({ status: status({ state: "live" }) });
    renderWithProviders(<WatchPage />, { route: "/watch" });
    fireEvent.click(await screen.findByRole("button", { name: "Wstrzymaj" }));
    await waitFor(() => expect(calls.find((c) => c.url.endsWith("/v1/watch/rt/engine"))?.body).toEqual({ action: "pause" }));
  });

  it("feed: rows are defanged, never links, filter by matched / source / text, stream items and matches merge in", async () => {
    const { calls } = setup();
    renderWithProviders(<WatchPage />, { route: "/watch" });
    expect(await screen.findByText("hxxps://example-3[.]top/login")).toBeInTheDocument();
    expect(screen.getAllByTestId("feed-row")).toHaveLength(3);
    expect(document.querySelector("a[href*='example']")).toBeNull();
    const replayRow = screen.getAllByTestId("feed-row")[2]!;
    expect(within(replayRow).getByText("odtworzenie")).toBeInTheDocument();
    expect(within(screen.getAllByTestId("feed-row")[1]!).getByText("dopasowanie")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("feed-matched"), { target: { value: "1" } });
    await waitFor(() => expect(screen.getAllByTestId("feed-row")).toHaveLength(1));
    expect(calls.some((c) => c.url.includes("/v1/watch/rt/items?") && c.url.includes("matched=1"))).toBe(true);
    fireEvent.change(screen.getByTestId("feed-matched"), { target: { value: "" } });
    await waitFor(() => expect(screen.getAllByTestId("feed-row")).toHaveLength(3));

    fireEvent.change(screen.getByTestId("feed-source"), { target: { value: "crtsh" } });
    await waitFor(() => expect(screen.getAllByTestId("feed-row")).toHaveLength(1));
    fireEvent.change(screen.getByTestId("feed-source"), { target: { value: "" } });

    act(() => {
      watchRtBus.emit({ type: "item", item: item(10, { value_defanged: "hxxps://nowa-domena[.]top/" }) });
      watchRtBus.emit({ type: "match", match: match({ id: 6, item_id: 10, indicator_display: "nowa-domena[.]top" }) });
    });
    await waitFor(() => expect(screen.getAllByTestId("feed-row")).toHaveLength(4));
    const first = screen.getAllByTestId("feed-row")[0]!;
    expect(first).toHaveTextContent("hxxps://nowa-domena[.]top/");
    expect(within(first).getByText("dopasowanie")).toBeInTheDocument();
    expect(screen.getByTestId("feed-count")).toHaveTextContent("+1 ze strumienia");
    expect(within(screen.getByTestId("match-list")).getByText("nowa-domena[.]top")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("feed-search"), { target: { value: "nowa-domena.top" } });
    await waitFor(() => expect(screen.getAllByTestId("feed-row")).toHaveLength(1));
  });

  it("feed windowing renders only the visible rows of a long list", async () => {
    setup();
    renderWithProviders(<WatchPage />, { route: "/watch" });
    await screen.findByText("hxxps://example-3[.]top/login");
    act(() => {
      for (let i = 100; i < 600; i++) watchRtBus.emit({ type: "item", item: item(i) });
    });
    await waitFor(() => expect(screen.getByTestId("feed-count")).toHaveTextContent("503 poz."));
    expect(screen.getAllByTestId("feed-row").length).toBeLessThan(30);
  });

  it("match panel: selecting a matched feed row opens the match; confirm, rarity wording, create incident result", async () => {
    const { calls } = setup();
    renderWithProviders(<WatchPage />, { route: "/watch" });
    await screen.findByText("hxxps://example-2[.]top/login");
    await screen.findAllByTestId("match-row");
    fireEvent.click(screen.getAllByTestId("feed-row")[1]!);
    const detail = await screen.findByTestId("match-detail");
    expect(within(detail).getByText("domena")).toBeInTheDocument();
    expect(within(detail).getByTestId("match-rarity")).toHaveTextContent("rzadkość w korpusie (informacyjnie, to nie prawdopodobieństwo): 7,3 bit · df 2 z 120 incydentów");
    expect(within(detail).getByText("domain_exact")).toBeInTheDocument();
    expect(within(detail).getByRole("link", { name: "Otwórz kampanię" })).toHaveAttribute("href", "/campaigns/C-7");
    fireEvent.click(within(detail).getByRole("button", { name: "Potwierdź" }));
    await waitFor(() => expect(calls.find((c) => c.url.endsWith("/v1/watch/rt/matches/5") && c.method === "POST")?.body).toEqual({ status: "acknowledged" }));
    fireEvent.click(within(screen.getByTestId("match-detail")).getByRole("button", { name: "Utwórz incydent" }));
    const res = await screen.findByTestId("match-result");
    expect(res).toHaveTextContent("nowy wzorzec P-0003");
    expect(res).toHaveTextContent("nowa kampania");
    expect(res).toHaveTextContent("Utworzono incydent i uruchomiono przypisanie.");
  });

  it("watchlist: groups by Polish kind labels, origins, delete only manual, add posts the form, 422 shows the message", async () => {
    const { calls } = setup();
    renderWithProviders(<WatchPage />, { route: "/watch" });
    const wl = await screen.findByTestId("watchlist");
    await within(wl).findByText("pko-bonus[.]top");
    expect(within(wl).getByTestId("watch-group-iban")).toHaveTextContent("IBAN");
    expect(within(wl).getByTestId("watch-group-keyword")).toHaveTextContent("słowo kluczowe (CT)");
    expect(within(wl).getAllByText("z kampanii").length).toBeGreaterThan(0);
    expect(within(wl).getByText("ręczny", { selector: "span.truncate" })).toBeInTheDocument();
    expect(within(wl).getAllByRole("button", { name: /^Usuń wpis/ })).toHaveLength(1);
    fireEvent.change(within(wl).getByLabelText("Rodzaj wskaźnika"), { target: { value: "phone" } });
    fireEvent.change(within(wl).getByLabelText("Wartość"), { target: { value: "+48 600 100 200" } });
    fireEvent.change(within(wl).getByLabelText("Etykieta (opcjonalnie)"), { target: { value: "infolinia" } });
    fireEvent.click(within(wl).getByRole("button", { name: "Dodaj" }));
    await waitFor(() => expect(calls.find((c) => c.url.endsWith("/v1/watch/rt/watchlist") && c.method === "POST")?.body).toEqual({ kind: "phone", value: "+48 600 100 200", label: "infolinia" }));
    expect(await within(wl).findByTestId("watchlist-msg")).toHaveTextContent("Dodano: telefon");
    fireEvent.click(within(wl).getByRole("button", { name: "Przebuduj z kampanii" }));
    await waitFor(() => expect(within(wl).getByTestId("watchlist-msg")).toHaveTextContent("dodano 4, usunięto 1, razem 60"));
    fireEvent.click(within(wl).getByRole("button", { name: /^Usuń wpis PL61/ }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url.endsWith("/v1/watch/rt/watchlist/2"))).toBe(true));
  });

  it("watchlist add: 422 detail.message_pl is shown", async () => {
    setup({ add422: true });
    renderWithProviders(<WatchPage />, { route: "/watch" });
    const wl = await screen.findByTestId("watchlist");
    fireEvent.change(within(wl).getByLabelText("Rodzaj wskaźnika"), { target: { value: "iban" } });
    fireEvent.change(within(wl).getByLabelText("Wartość"), { target: { value: "PL00" } });
    fireEvent.click(within(wl).getByRole("button", { name: "Dodaj" }));
    await waitFor(() => expect(within(wl).getByTestId("watchlist-msg")).toHaveTextContent("Niepoprawny IBAN (suma kontrolna)."));
  });

  it("demo mode: Start posts fixture + speed and the page switches to the replay banner", async () => {
    const { calls } = setup();
    renderWithProviders(<WatchPage />, { route: "/watch" });
    const fx = await screen.findByTestId("demo-fixture");
    await waitFor(() => expect((fx as HTMLSelectElement).value).toBe("demo"));
    fireEvent.change(screen.getByTestId("demo-speed"), { target: { value: "600" } });
    fireEvent.click(screen.getByTestId("demo-start"));
    await waitFor(() => expect(calls.find((c) => c.url.endsWith("/v1/watch/rt/replay"))?.body).toEqual({ fixture: "demo", speed: 600 }));
    expect(await screen.findByTestId("replay-banner")).toHaveTextContent("SYNTETYCZNE / ODTWORZENIE");
  });

  it("the classic view stays reachable as the second tab", async () => {
    setup();
    renderWithProviders(<WatchPage />, { route: "/watch" });
    fireEvent.click(await screen.findByRole("tab", { name: "Odkrywanie domen (nagrania)" }));
    expect(await screen.findByRole("heading", { name: "Ticker" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Odkrywanie domen (nagrania)" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("watch helpers", () => {
  it("countdown ticks from next_due_in_s at fetch time and falls back to next_due", () => {
    expect(countdown(src({ next_due_in_s: 30 }), 1000, 11_000)).toBe(20);
    expect(countdown(src({ next_due_in_s: null, next_due: new Date(60_000).toISOString() }), 0, 30_000)).toBe(30);
    expect(countdown(src({ next_due_in_s: null, next_due: null }), 0, 0)).toBeNull();
  });
  it("client-side filter accepts the defanged and the plain form of a search", () => {
    const it0 = item(1);
    expect(itemMatchesFilters(it0, { q: "example-1.top" }, 0)).toBe(true);
    expect(itemMatchesFilters(it0, { q: "example-1[.]top" }, 0)).toBe(true);
    expect(itemMatchesFilters(it0, { matched: "1" }, 0)).toBe(false);
    expect(itemMatchesFilters(it0, { kind: "cert" }, 0)).toBe(false);
  });
});
