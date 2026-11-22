import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { ConsoleTab } from "./ConsoleTab";
import { MINI_OPENAPI } from "./console/__fixtures__/openapi";
import { HISTORY_KEY } from "./console/history";
import { STORAGE_PREFIX } from "../../lib/storage";

function setup(route = "/integrations?tab=console", handler?: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fetchMock = mockFetch(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/api/openapi.json")) return jsonResponse(MINI_OPENAPI);
    if (handler) return handler(url, init);
    return jsonResponse({ ok: true, status: "zdrowy" }, { headers: { "x-test": "1" } });
  });
  renderWithProviders(<ConsoleTab />, { route });
  return fetchMock;
}

describe("ConsoleTab", () => {
  it("lists endpoints grouped and filters by search", async () => {
    setup();
    const list = await screen.findByTestId("console-op-list");
    expect(within(list).getByText("/v1/campaigns")).toBeInTheDocument();
    expect(within(list).getByText("/v1/stream")).toBeInTheDocument();
    await userEvent.type(screen.getByTestId("console-search"), "health");
    expect(within(list).queryByText("/v1/campaigns")).not.toBeInTheDocument();
    expect(within(list).getByText("/health")).toBeInTheDocument();
    expect(screen.getByText("1 z 6 endpointów")).toBeInTheDocument();
  });

  it("'/' focuses the search", async () => {
    setup();
    await screen.findByTestId("console-op-list");
    fireEvent.keyDown(window, { key: "/" });
    expect(screen.getByTestId("console-search")).toHaveFocus();
  });

  it("sends GET and renders status, time and size; history keeps no body or headers", async () => {
    const fetchMock = setup();
    await screen.findByTestId("console-operation");
    expect(screen.getByRole("heading", { name: "/health" })).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("console-send"));
    expect(await screen.findByTestId("response-status")).toHaveTextContent("200");
    expect(screen.getByTestId("response-time")).toHaveTextContent(/\d+ ms/);
    const size = JSON.stringify({ ok: true, status: "zdrowy" }).length;
    expect(screen.getByTestId("response-size")).toHaveTextContent(`${size} B`);
    expect(screen.getByTestId("response-body")).toHaveTextContent('"status": "zdrowy"');
    const call = fetchMock.mock.calls.find(([u]) => String(u) === "/api/health")!;
    expect(call[1]?.method).toBe("GET");
    const stored = window.localStorage.getItem(STORAGE_PREFIX + HISTORY_KEY)!;
    expect(JSON.parse(stored)[0]).toMatchObject({ method: "GET", path: "/health", status: 200 });
    expect(stored).not.toMatch(/accept|content-type/i);
  });

  it("selects the operation from &op=, builds the query, and Ctrl+Enter sends", async () => {
    const fetchMock = setup("/integrations?tab=console&op=list_campaigns_v1_campaigns_get");
    await screen.findByRole("heading", { name: "/v1/campaigns" });
    expect(screen.getByTestId("console-url")).toHaveTextContent("/v1/campaigns?limit=50");
    fireEvent.change(screen.getByLabelText(/order/), { target: { value: "size" } });
    expect(screen.getByTestId("console-url")).toHaveTextContent("/v1/campaigns?limit=50&order=size");
    fireEvent.keyDown(window, { key: "Enter", ctrlKey: true });
    await screen.findByTestId("response-status");
    expect(fetchMock.mock.calls.some(([u]) => String(u) === "/api/v1/campaigns?limit=50&order=size")).toBe(true);
    expect(screen.getByTestId("console-snippet")).toHaveTextContent("curl -sS");
  });

  it("asks for confirmation before POST and sends the prefilled JSON body", async () => {
    const fetchMock = setup("/integrations?tab=console&op=change_watchlist_v1_watchlist_post");
    await screen.findByRole("heading", { name: "/v1/watchlist" });
    expect((screen.getByTestId("console-body") as HTMLTextAreaElement).value).toContain('"kind": "keyword"');
    await userEvent.click(screen.getByTestId("console-send"));
    expect(screen.getByTestId("console-confirm")).toHaveTextContent("lokalnej instancji");
    expect(fetchMock.mock.calls.some(([u]) => String(u) === "/api/v1/watchlist")).toBe(false);
    await userEvent.click(screen.getByTestId("console-confirm-send"));
    await screen.findByTestId("response-status");
    const call = fetchMock.mock.calls.find(([u]) => String(u) === "/api/v1/watchlist")!;
    expect(call[1]?.method).toBe("POST");
    expect(JSON.parse(String(call[1]?.body))).toEqual({ add: [{ kind: "keyword", value: "xx" }], remove: [{ kind: "keyword", value: "xx" }], note: "" });
  });

  it("blocks invalid input with Polish messages", async () => {
    setup("/integrations?tab=console&op=campaign_stix");
    await screen.findByTestId("console-operation");
    await userEvent.click(screen.getByTestId("console-send"));
    expect(screen.getByRole("alert")).toHaveTextContent("Brak wymaganego parametru „campaign_id”.");
  });

  it("streaming endpoints show curl -N instead of Send", async () => {
    setup("/integrations?tab=console&op=stream_v1_stream_get");
    await screen.findByTestId("console-streaming-note");
    expect(screen.queryByTestId("console-send")).not.toBeInTheDocument();
    expect(screen.getByTestId("console-snippet")).toHaveTextContent("curl -N");
  });

  it("renders HTTP errors and network failures", async () => {
    setup("/integrations?tab=console", () => jsonResponse({ detail: "nie ma" }, { status: 404 }));
    await screen.findByTestId("console-operation");
    await userEvent.click(screen.getByTestId("console-send"));
    expect(await screen.findByTestId("response-status")).toHaveTextContent("404");
  });

  it("network failure shows a Polish error", async () => {
    setup("/integrations?tab=console", () => {
      throw new TypeError("fetch failed");
    });
    await screen.findByTestId("console-operation");
    await userEvent.click(screen.getByTestId("console-send"));
    await waitFor(() => expect(screen.getByTestId("response-error")).toHaveTextContent("Brak połączenia"));
  });

  it("an example fills the form", async () => {
    setup();
    await screen.findByTestId("console-examples");
    await userEvent.click(within(screen.getByTestId("console-examples")).getByText("Lista kampanii"));
    expect(await screen.findByRole("heading", { name: "/v1/campaigns" })).toBeInTheDocument();
    // examples whose endpoint is not in this schema are disabled
    expect(within(screen.getByTestId("console-examples")).getByText("Lista konektorów").closest("button")).toBeDisabled();
  });
});
