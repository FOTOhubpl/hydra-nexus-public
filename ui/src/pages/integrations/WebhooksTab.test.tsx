import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { generateSecret, type Delivery } from "../../api/integrations";
import { jsonResponse, mockFetch, renderWithProviders } from "../../test/utils";
import { WebhooksTab } from "./WebhooksTab";
import { webhooks } from "./hub/testData";

afterEach(() => vi.unstubAllGlobals());

const delivery = (id: string, status: Delivery["status"]): Delivery => ({
  id, endpoint_id: "e1", event: "ping", status, attempts: status === "dead" ? 5 : 1, max_attempts: 5, last_status_code: status === "delivered" ? 200 : 500,
  last_error: null, next_retry_at: null, payload_sha256: null, created_at: "2026-11-22T02:00:00Z", delivered_at: null,
});

describe("WebhooksTab", () => {
  it("generates a 32-byte hex secret with crypto.getRandomValues", () => {
    const spy = vi.spyOn(crypto, "getRandomValues");
    const s = generateSecret();
    expect(s).toMatch(/^[0-9a-f]{64}$/);
    expect(spy).toHaveBeenCalled();
    expect(generateSecret()).not.toBe(s);
  });

  it("adds a loopback endpoint with a generated secret and clears the secret field", async () => {
    const posts: { url: string; body: Record<string, unknown> }[] = [];
    mockFetch(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (method !== "GET") posts.push({ url, body: JSON.parse(String(init?.body ?? "{}")) });
      if (url === "/api/v1/integrations/webhooks" && method === "GET") return jsonResponse(webhooks());
      if (url === "/api/v1/integrations/webhooks" && method === "POST") return jsonResponse({ id: "e1", name: "x", url_redacted: "http://127.0.0.1/…sink", events: ["ping"], enabled: true, loopback: true, secret: { configured: true, last4: "abcd" }, created_at: "", last_delivery: null });
      if (url.startsWith("/api/v1/integrations/webhooks/e1") && method === "PATCH") return jsonResponse({ id: "e1", name: "x", url_redacted: "", events: ["ping"], enabled: true, loopback: true, secret: { configured: true, last4: "abcd" }, created_at: "", last_delivery: null });
      if (url.startsWith("/api/v1/integrations/webhooks/deliveries")) return jsonResponse({ deliveries: [] });
      if (url.startsWith("/api/v1/integrations/webhooks/sink")) return jsonResponse({ receipts: [] });
      return jsonResponse({ detail: "x" }, { status: 404 });
    });
    renderWithProviders(<WebhooksTab />);
    fireEvent.click(await screen.findByTestId("use-sink"));
    expect((screen.getByTestId("webhook-url") as HTMLInputElement).value).toBe(`${window.location.origin}/api/v1/integrations/webhooks/sink`);
    fireEvent.click(screen.getByTestId("generate-secret"));
    const secret = screen.getByTestId("webhook-secret") as HTMLInputElement;
    expect(secret.type).toBe("password");
    expect(secret.value).toMatch(/^[0-9a-f]{64}$/);
    const value = secret.value;
    expect(screen.getByTestId("copy-secret")).toHaveTextContent("Skopiuj");
    fireEvent.click(screen.getByTestId("event-delta"));
    fireEvent.click(screen.getByTestId("webhook-submit"));
    await screen.findByText(/Dodano endpoint/);
    expect(posts[0]!.body).toMatchObject({ secret: value, events: ["ping", "delta"], enabled: true });
    expect(posts[1]!.url).toBe("/api/v1/integrations/webhooks/e1");
    expect(String(posts[1]!.body.url)).toContain("sink?endpoint_id=e1");
    expect(secret.value).toBe("");
    expect(screen.queryByTestId("copy-secret")).toBeNull();
    for (const k of Object.keys(window.localStorage)) expect(window.localStorage.getItem(k)).not.toContain(value);
  });

  it("shows delivery status pills and a retry button only for dead letters", async () => {
    const retried: string[] = [];
    mockFetch(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/integrations/webhooks") return jsonResponse(webhooks({ endpoints: [{ id: "e1", name: "Sink", url_redacted: "http://127.0.0.1/…sink", events: ["ping"], enabled: true, loopback: true, secret: { configured: true, last4: "abcd" }, created_at: "", last_delivery: null }] }));
      if (url.includes("/retry")) { retried.push(url); return jsonResponse(delivery("d4", "pending")); }
      if (url.startsWith("/api/v1/integrations/webhooks/deliveries")) return jsonResponse({ deliveries: [delivery("d1", "delivered"), delivery("d2", "dead"), delivery("d3", "blocked_offline"), delivery("d4", "retrying")] });
      if (url.startsWith("/api/v1/integrations/webhooks/sink")) return jsonResponse({ receipts: [{ at: "2026-11-22T02:00:00Z", event: "ping", delivery_id: "d1", signature_ok: true, bytes: 120 }] });
      void init;
      return jsonResponse({}, { status: 404 });
    });
    renderWithProviders(<WebhooksTab />);
    const log = await screen.findByTestId("delivery-log");
    await waitFor(() => expect(within(log).getAllByTestId("delivery-status")).toHaveLength(4));
    const text = within(log).getAllByTestId("delivery-status").map((n) => n.textContent);
    expect(text).toEqual(expect.arrayContaining(["Dostarczono", "Martwa kolejka", "Zablokowano offline", "Ponawianie"]));
    const retry = within(log).getAllByRole("button", { name: "Ponów" });
    expect(retry).toHaveLength(1);
    fireEvent.click(retry[0]!);
    await waitFor(() => expect(retried[0]).toContain("/deliveries/d2/retry"));
    expect(await screen.findByTestId("signature-pill")).toHaveTextContent("Podpis poprawny");
  });
});
