/**
 * Integrations & API: types and fetchers for `/v1/integrations` and `/v1/integrations/webhooks`
 * (contract: hydra/api_integrations.py, hydra/api_webhooks.py).
 *
 * Secrets: a key typed by the operator is sent once in a mutation body and never kept in a query key, a query
 * result, localStorage or a log. Mutations here never put the variables into the cache (TanStack keeps mutation
 * variables only on the mutation object, which the components reset right after submit).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, apiUrl } from "./client";

export type ConnectorCategory = "enrichment" | "threat_intel" | "ad_transparency" | "outbound_sharing" | "database_sink" | "notification";
export type ConnectorDirection = "inbound" | "outbound" | "both";
export type ConnectorAuth = "none" | "api_key" | "oauth_token" | "basic" | "hmac";
export type ConnectorLevel = "live" | "export-format" | "planned";
export type ConnectorStatus = "configured" | "not_configured" | "offline" | "disabled" | "planned";

export interface SecretStatus {
  name: string;
  configured: boolean;
  source: "env" | "file" | null;
  last4: string | null;
}

export interface LastResult {
  at?: string | null;
  ok?: boolean;
  status?: string;
  message_pl?: string;
  ms?: number | null;
  [key: string]: unknown;
}

export interface Usage {
  requests_60m: number;
  limit_per_min: number | null;
}

export interface Connector {
  id: string;
  name: string;
  category: ConnectorCategory | string;
  direction: ConnectorDirection;
  auth: ConnectorAuth | string;
  level: ConnectorLevel;
  formats: string[];
  description_pl: string;
  setup_pl: string[];
  notes_pl: string;
  docs_url: string | null;
  licence_pl: string;
  rate_limit_pl: string;
  rate_limit_per_min: number | null;
  data_out: string[];
  data_out_pl: string;
  never_sent_pl: string;
  secrets: SecretStatus[];
  status: ConnectorStatus;
  enabled: boolean;
  can_connect: boolean;
  can_test: boolean;
  can_run: boolean;
  can_export: boolean;
  last_test: LastResult | null;
  last_run: LastResult | null;
  usage: Usage | null;
}

export interface AuditEntry {
  seq: number;
  ts: string;
  type: string;
  payload: Record<string, unknown>;
}

export interface ConnectorDetail extends Connector {
  audit: AuditEntry[];
  runs: LastResult[];
}

export interface Labelled {
  id: string;
  label_pl: string;
  description_pl: string;
}

export interface Preset {
  id: string;
  name_pl: string;
  description_pl: string;
  connectors: string[];
}

export interface IntegrationsIndex {
  offline: boolean;
  enrich: boolean;
  generated_at: string;
  categories: Labelled[];
  levels: Labelled[];
  presets: Preset[];
  connectors: Connector[];
}

export interface TestResult {
  ok: boolean;
  status: "ok" | "offline" | "not_configured" | "error" | "disabled" | string;
  message_pl: string;
  ms?: number | null;
  at?: string | null;
}

export interface ConnectStep {
  id: "secret" | "test" | "enable" | "export" | string;
  ok: boolean;
  message_pl: string;
  status?: string;
}

export interface ConnectResult {
  ok: boolean;
  steps: ConnectStep[];
  connector: Connector;
}

export interface PreviewResult {
  connector: string;
  format: string;
  method: string;
  destination: string;
  headers: Record<string, string>;
  content_type: string;
  bytes: number;
  sha256: string;
  fields_sent: string[];
  redactions: string[];
  payload: unknown;
  truncated?: boolean;
}

export interface PresetApplyResult {
  preset: Preset | string;
  results: { connector: string; ok: boolean; status: string; message_pl: string; steps_pl?: string[] }[];
}

// --- webhooks ------------------------------------------------------------------------------------------------

export interface WebhookEventType {
  id: string;
  label_pl: string;
  description_pl: string;
  example?: unknown;
}

export interface Delivery {
  id: string;
  endpoint_id: string;
  event: string;
  status: "pending" | "delivered" | "retrying" | "dead" | "blocked_offline";
  attempts: number;
  max_attempts: number;
  last_status_code: number | null;
  last_error: string | null;
  next_retry_at: string | null;
  payload_sha256: string | null;
  created_at: string;
  delivered_at: string | null;
}

export interface WebhookEndpoint {
  id: string;
  name: string;
  url_redacted: string;
  events: string[];
  enabled: boolean;
  loopback: boolean;
  secret: { configured: boolean; last4: string | null };
  created_at: string;
  last_delivery: Partial<Delivery> | null;
}

export interface WebhooksIndex {
  delivery_possible: boolean;
  offline: boolean;
  event_types: WebhookEventType[];
  endpoints: WebhookEndpoint[];
  signature: { header: string; algo: string; scheme_pl: string };
}

export interface SinkReceipt {
  at: string;
  event: string;
  delivery_id: string;
  signature_ok: boolean;
  bytes: number;
}

export interface NewEndpoint {
  name: string;
  url: string;
  events: string[];
  enabled: boolean;
  secret: string;
}

// --- fetchers ------------------------------------------------------------------------------------------------

const BASE = "/v1/integrations";
const WH = `${BASE}/webhooks`;
const enc = encodeURIComponent;

export const integrationKeys = {
  all: ["integrations"] as const,
  index: ["integrations", "index"] as const,
  detail: (id: string) => ["integrations", "detail", id] as const,
  webhooks: ["integrations", "webhooks"] as const,
  deliveries: ["integrations", "webhooks", "deliveries"] as const,
  sink: ["integrations", "webhooks", "sink"] as const,
  scheduler: ["integrations", "scheduler"] as const,
};

export const getIntegrations = (signal?: AbortSignal) => apiFetch<IntegrationsIndex>(BASE, { signal });
export const getConnector = (id: string, signal?: AbortSignal) => apiFetch<ConnectorDetail>(`${BASE}/${enc(id)}`, { signal });
export const putSecret = (id: string, name: string, value: string) =>
  apiFetch<SecretStatus>(`${BASE}/${enc(id)}/secret`, { method: "PUT", json: { name, value } });
export const deleteSecret = (id: string, name: string) =>
  apiFetch<SecretStatus>(`${BASE}/${enc(id)}/secret?name=${enc(name)}`, { method: "DELETE" });
export const setEnabled = (id: string, enabled: boolean) =>
  apiFetch<Connector>(`${BASE}/${enc(id)}/enable`, { method: "POST", json: { enabled } });
export const testConnector = (id: string) => apiFetch<TestResult>(`${BASE}/${enc(id)}/test`, { method: "POST", json: {} });
export const connectConnector = (id: string, secrets: Record<string, string>) =>
  apiFetch<ConnectResult>(`${BASE}/${enc(id)}/connect`, { method: "POST", json: Object.keys(secrets).length ? { secrets } : {} });
export const previewConnector = (id: string, body: { run_id?: string | null; format?: string; limit?: number }) =>
  apiFetch<PreviewResult>(`${BASE}/${enc(id)}/preview`, { method: "POST", json: stripNull(body) });
export const applyPreset = (presetId: string) =>
  apiFetch<PresetApplyResult>(`${BASE}/presets/${enc(presetId)}/apply`, { method: "POST", json: {} });

/** Same-origin download URL of a provider-format file (GET export, audited server side). */
export function exportUrl(id: string, format?: string, runId?: string | null, campaignId?: string | null): string {
  const qs = new URLSearchParams();
  if (format) qs.set("format", format);
  if (runId) qs.set("run_id", runId);
  if (campaignId) qs.set("campaign_id", campaignId);
  const q = qs.toString();
  return apiUrl(`${BASE}/${enc(id)}/export${q ? `?${q}` : ""}`);
}

export const getWebhooks = (signal?: AbortSignal) => apiFetch<WebhooksIndex>(WH, { signal });
export const createEndpoint = (body: NewEndpoint) => apiFetch<WebhookEndpoint>(WH, { method: "POST", json: body });
export const patchEndpoint = (id: string, body: Partial<NewEndpoint>) =>
  apiFetch<WebhookEndpoint>(`${WH}/${enc(id)}`, { method: "PATCH", json: body });
export const deleteEndpoint = (id: string) => apiFetch<{ deleted: boolean }>(`${WH}/${enc(id)}`, { method: "DELETE" });
export const testEndpoint = (id: string) => apiFetch<Delivery>(`${WH}/${enc(id)}/test`, { method: "POST", json: {} });
export const getDeliveries = (signal?: AbortSignal) => apiFetch<{ deliveries: Delivery[] }>(`${WH}/deliveries?limit=50`, { signal });
export const retryDelivery = (id: string) => apiFetch<Delivery>(`${WH}/deliveries/${enc(id)}/retry`, { method: "POST", json: {} });
export const getSink = (signal?: AbortSignal) => apiFetch<{ receipts: SinkReceipt[] }>(`${WH}/sink`, { signal });

/** URL of the built-in loopback receiver for an endpoint (same origin as the UI, `/api` proxied to the API). */
export function sinkUrl(origin: string, endpointId = ""): string {
  return `${origin}${apiUrl(`${WH}/sink`)}${endpointId ? `?endpoint_id=${enc(endpointId)}` : ""}`;
}

function stripNull<T extends Record<string, unknown>>(body: T): Partial<T> {
  return Object.fromEntries(Object.entries(body).filter(([, v]) => v !== null && v !== undefined && v !== "")) as Partial<T>;
}

/** 32 random bytes as 64 lowercase hex characters (crypto.getRandomValues, never Math.random). */
export function generateSecret(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// --- hooks ---------------------------------------------------------------------------------------------------

export function useIntegrations() {
  return useQuery({ queryKey: integrationKeys.index, queryFn: ({ signal }) => getIntegrations(signal), staleTime: 5_000 });
}

export function useConnector(id: string | null) {
  return useQuery({
    queryKey: integrationKeys.detail(id ?? ""),
    queryFn: ({ signal }) => getConnector(id as string, signal),
    enabled: Boolean(id),
  });
}

export function useWebhooks() {
  return useQuery({ queryKey: integrationKeys.webhooks, queryFn: ({ signal }) => getWebhooks(signal) });
}

export function useDeliveries(poll = false) {
  return useQuery({
    queryKey: integrationKeys.deliveries,
    queryFn: ({ signal }) => getDeliveries(signal),
    refetchInterval: poll ? 2_000 : false,
  });
}

export function useSink(poll = false) {
  return useQuery({ queryKey: integrationKeys.sink, queryFn: ({ signal }) => getSink(signal), refetchInterval: poll ? 2_000 : false });
}

/** Scheduler status for the Meta Ad Library card (optional; `available:false` without the module). */
export function useSchedulerJobs(enabled: boolean) {
  return useQuery({
    queryKey: integrationKeys.scheduler,
    queryFn: ({ signal }) => apiFetch<{ available: boolean; jobs?: Record<string, Record<string, unknown>>; meta_token_configured?: boolean }>("/v1/scheduler/status", { signal }),
    enabled,
    retry: false,
  });
}

/** Invalidate every integrations query after a mutation (the index and the open drawer). */
export function useInvalidateIntegrations() {
  const client = useQueryClient();
  return () => client.invalidateQueries({ queryKey: integrationKeys.all });
}

export function useIntegrationMutation<TVars, TData>(fn: (vars: TVars) => Promise<TData>) {
  const invalidate = useInvalidateIntegrations();
  return useMutation({ mutationFn: fn, onSettled: () => void invalidate() });
}
