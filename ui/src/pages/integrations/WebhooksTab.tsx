/**
 * "Webhooki": signed HTTP notifications to the operator's endpoints. Endpoint list, add form (secret generated in
 * the browser with crypto.getRandomValues and shown once), test sends, delivery log with retries / dead letters,
 * the built-in loopback receiver and the signature scheme. Every value comes from /v1/integrations/webhooks.
 */
import { useState, type FormEvent } from "react";
import {
  createEndpoint,
  deleteEndpoint,
  generateSecret,
  patchEndpoint,
  retryDelivery,
  sinkUrl,
  testEndpoint,
  useDeliveries,
  useIntegrationMutation,
  useInvalidateIntegrations,
  useSink,
  useWebhooks,
  type Delivery,
  type WebhookEndpoint,
  type WebhooksIndex,
} from "../../api/integrations";
import { isApiError } from "../../api/client";
import { ApiErrorState } from "../../components/ApiErrorState";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { DataTable, type Column } from "../../components/DataTable";
import { EmptyState } from "../../components/EmptyState";
import { Panel } from "../../components/Panel";
import { Skeleton } from "../../components/Skeleton";
import { InfoTip, Tooltip } from "../../components/Tooltip";
import { DELIVERY_LABEL, DELIVERY_TIP, DELIVERY_VARIANT, formatWhen } from "./hub/labels";
import { CodeViewer, CopyButton, Switch, TipPill } from "./hub/ui";

const VERIFY_SNIPPET = `import hashlib, hmac, time

def verify(secret: bytes, headers: dict, raw_body: bytes) -> bool:
    ts = headers["X-Hydra-Timestamp"]
    if abs(time.time() - int(ts)) > 300:      # odrzuć stare żądania (replay)
        return False
    mac = hmac.new(secret, f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, headers["X-Hydra-Signature"])`;

export function DeliveryPill({ status }: { status: Delivery["status"] }) {
  return <TipPill variant={DELIVERY_VARIANT[status] ?? "neutral"} tip={DELIVERY_TIP[status] ?? status} testId="delivery-status">{DELIVERY_LABEL[status] ?? status}</TipPill>;
}

function AddEndpoint({ index }: { index: WebhooksIndex }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>(["ping"]);
  const [secret, setSecret] = useState("");
  const [generated, setGenerated] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [loopback, setLoopback] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const invalidate = useInvalidateIntegrations();

  const fillSink = () => {
    setLoopback(true);
    setUrl(sinkUrl(window.location.origin));
    if (!name) setName("Odbiornik testowy (loopback)");
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const body = { name: name.trim(), url: url.trim(), events, enabled, secret };
    setSecret(""); // the secret leaves the UI state right away; the server never echoes it
    setGenerated(false);
    setBusy(true);
    setMsg(null);
    try {
      let ep = await createEndpoint(body);
      // The loopback receiver identifies the endpoint by ?endpoint_id=, known only after creation.
      if (loopback && !/endpoint_id=/.test(body.url)) ep = await patchEndpoint(ep.id, { url: sinkUrl(window.location.origin, ep.id) });
      setMsg({ ok: true, text: `Dodano endpoint „${ep.name}”.` });
      setName("");
      setUrl("");
      setLoopback(false);
      setEvents(["ping"]);
    } catch (err) {
      setMsg({ ok: false, text: isApiError(err) ? err.message : "Nie udało się dodać endpointu." });
    } finally {
      setBusy(false);
      void invalidate();
    }
  };

  return (
    <Panel title="Dodaj endpoint">
      <form onSubmit={submit} className="space-y-3" autoComplete="off" data-testid="webhook-form">
        <div className="grid min-w-0 gap-3 sm:grid-cols-2">
          <label className="block min-w-0 space-y-1">
            <span className="text-xs font-medium text-subtle">Nazwa</span>
            <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} aria-label="Nazwa endpointu" className="h-8 w-full min-w-0 rounded-[4px] border border-line bg-raised px-2 text-sm" />
          </label>
          <label className="block min-w-0 space-y-1">
            <span className="flex items-center gap-1.5 text-xs font-medium text-subtle">Adres URL <InfoTip content="Adres odbiorcy (HTTPS). Adresy spoza tego komputera działają tylko przy HYDRA_OFFLINE=0; adres jest przechowywany lokalnie (0600) i pokazywany w skrócie." /></span>
            <input value={url} onChange={(e) => { setUrl(e.target.value); setLoopback(false); }} required type="url" inputMode="url" spellCheck={false} aria-label="Adres URL endpointu" data-testid="webhook-url" className="h-8 w-full min-w-0 rounded-[4px] border border-line bg-raised px-2 font-mono text-sm" />
          </label>
        </div>
        <Button onClick={fillSink} data-testid="use-sink">Użyj odbiornika testowego (loopback)</Button>

        <fieldset className="min-w-0 space-y-1">
          <legend className="text-xs font-medium text-subtle">Zdarzenia</legend>
          <div className="flex min-w-0 flex-wrap gap-x-4 gap-y-1.5">
            {index.event_types.map((t) => (
              <Tooltip key={t.id} content={t.description_pl} className="inline-flex min-w-0">
                <label className="inline-flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    checked={events.includes(t.id)}
                    onChange={(e) => setEvents((s) => (e.target.checked ? [...s, t.id] : s.filter((x) => x !== t.id)))}
                    data-testid={`event-${t.id}`}
                  />
                  <span>{t.label_pl}</span> <span className="font-mono text-2xs text-subtle">{t.id}</span>
                </label>
              </Tooltip>
            ))}
          </div>
        </fieldset>

        <div className="min-w-0 space-y-1">
          <span className="flex items-center gap-1.5 text-xs font-medium text-subtle">Sekret podpisu <InfoTip content="Wspólny sekret HMAC-SHA256. „Generuj” tworzy 32 losowe bajty w przeglądarce (crypto.getRandomValues). Skopiuj go teraz: po zapisaniu serwer pokazuje tylko 4 ostatnie znaki." /></span>
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              data-lpignore="true"
              data-1p-ignore=""
              value={secret}
              onChange={(e) => { setSecret(e.target.value); setGenerated(false); }}
              required
              minLength={16}
              aria-label="Sekret podpisu"
              data-testid="webhook-secret"
              className="h-8 min-w-0 flex-1 basis-56 rounded-[4px] border border-line bg-raised px-2 font-mono text-sm"
            />
            <Button onClick={() => { setSecret(generateSecret()); setGenerated(true); }} data-testid="generate-secret">Generuj</Button>
            {generated && secret && <CopyButton text={secret} label="Skopiuj" testId="copy-secret" />}
          </div>
          {generated && secret && <p className="text-xs text-warn">Skopiuj sekret teraz i zapisz go u odbiorcy; nie będzie można go później odczytać.</p>}
        </div>

        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <label className="inline-flex items-center gap-2 text-sm">
            <Switch checked={enabled} onChange={setEnabled} label="Endpoint włączony" />
            <span>Włączony</span>
          </label>
          <Button type="submit" variant="primary" disabled={busy || !events.length || secret.length < 16} data-testid="webhook-submit">{busy ? "Zapisywanie…" : "Dodaj endpoint"}</Button>
        </div>
        {msg && <div role={msg.ok ? "status" : "alert"} className={msg.ok ? "text-sm text-ok" : "text-sm break-words text-err"}>{msg.text}</div>}
      </form>
    </Panel>
  );
}

function EndpointRow({ ep, labels, onTested }: { ep: WebhookEndpoint; labels: Map<string, string>; onTested: () => void }) {
  const toggle = useIntegrationMutation((enabled: boolean) => patchEndpoint(ep.id, { enabled }));
  const test = useIntegrationMutation((_: void) => testEndpoint(ep.id));
  const del = useIntegrationMutation((_: void) => deleteEndpoint(ep.id));
  const last = ep.last_delivery;
  return (
    <li className="surface-card flex min-w-0 flex-col gap-2 p-3" data-testid={`endpoint-${ep.id}`}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="min-w-0 truncate text-sm font-semibold text-fg">{ep.name}</span>
        {ep.loopback && <Badge variant="info">loopback</Badge>}
        <span className="ml-auto inline-flex items-center gap-2 text-xs text-muted">
          {ep.enabled ? "włączony" : "wyłączony"}
          <Switch checked={toggle.isPending && toggle.variables !== undefined ? toggle.variables : ep.enabled} disabled={toggle.isPending} onChange={(v) => toggle.mutate(v)} label={`${ep.enabled ? "Wyłącz" : "Włącz"} ${ep.name}`} />
        </span>
      </div>
      <div className="font-mono text-xs break-all text-muted">{ep.url_redacted}</div>
      <div className="flex min-w-0 flex-wrap gap-1">{ep.events.map((e) => <Badge key={e}>{labels.get(e) ?? e}</Badge>)}</div>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <span>Sekret: {ep.secret.configured ? `zapisany${ep.secret.last4 ? ` (…${ep.secret.last4})` : ""}` : "brak"}</span>
        <span className="inline-flex items-center gap-1">Ostatnie doręczenie: {last?.status ? <DeliveryPill status={last.status} /> : "—"} {last?.created_at ? formatWhen(last.created_at) : ""}</span>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={() => test.mutate(undefined, { onSettled: onTested })} disabled={test.isPending} data-testid={`test-${ep.id}`}>{test.isPending ? "Wysyłanie…" : "Wyślij test"}</Button>
        <Button size="sm" variant="danger" onClick={() => del.mutate()} disabled={del.isPending}>Usuń</Button>
      </div>
      {test.data && <div role="status" className="flex items-center gap-2 text-xs">Test: <DeliveryPill status={test.data.status} /> {test.data.last_error && <span className="break-words text-muted">{test.data.last_error}</span>}</div>}
      {(test.isError || toggle.isError || del.isError) && <div role="alert" className="text-xs break-words text-err">{((test.error ?? toggle.error ?? del.error) as Error).message}</div>}
    </li>
  );
}

function DeliveryLog({ endpoints, poll }: { endpoints: WebhookEndpoint[]; poll: boolean }) {
  const q = useDeliveries(poll);
  const retry = useIntegrationMutation((id: string) => retryDelivery(id));
  const names = new Map(endpoints.map((e) => [e.id, e.name]));
  const rows = q.data?.deliveries ?? [];
  const columns: Column<Delivery>[] = [
    { key: "created_at", header: "Czas", render: (d) => formatWhen(d.created_at), width: "140px", mono: true },
    { key: "endpoint_id", header: "Endpoint", render: (d) => <span className="block truncate">{names.get(d.endpoint_id) ?? d.endpoint_id}</span> },
    { key: "event", header: "Zdarzenie", mono: true, width: "90px" },
    { key: "status", header: "Status", render: (d) => <DeliveryPill status={d.status} />, width: "170px" },
    { key: "attempts", header: "Próby", render: (d) => `${d.attempts}/${d.max_attempts}`, mono: true, width: "70px", title: "Wykonane próby / maksimum (backoff wykładniczy)" },
    { key: "last_status_code", header: "Kod", mono: true, width: "60px", render: (d) => d.last_status_code ?? "—" },
    { key: "last_error", header: "Błąd", render: (d) => <span className="block truncate" title={d.last_error ?? undefined}>{d.last_error ?? "—"}</span> },
    { key: "next_retry_at", header: "Następna próba", render: (d) => (d.next_retry_at ? formatWhen(d.next_retry_at) : "—"), width: "140px", mono: true },
    {
      key: "actions", header: <span className="sr-only">Akcje</span>, width: "80px",
      render: (d) => d.status === "dead" ? <Button size="sm" onClick={(e) => { e.stopPropagation(); retry.mutate(d.id); }} disabled={retry.isPending}>Ponów</Button> : null,
    },
  ];
  return (
    <Panel title="Dziennik doręczeń" actions={<InfoTip content="Do 5 prób; ponowienie po błędzie sieci, 429 lub 5xx z opóźnieniem 2, 4, 8, 16 s. Inne 4xx od razu trafiają do martwej kolejki." />} flush>
      {q.isError ? <ApiErrorState error={q.error} resource="dziennik doręczeń" compact /> : (
        <div data-testid="delivery-log">
          <DataTable columns={columns} rows={rows} rowKey={(d) => d.id} loading={q.isLoading} caption="Dziennik doręczeń webhooków" empty={<EmptyState title="Brak doręczeń" description="Użyj „Wyślij test” przy endpoincie." compact />} />
        </div>
      )}
      {retry.isError && <div role="alert" className="p-3 text-xs text-err">{(retry.error as Error).message}</div>}
    </Panel>
  );
}

function SinkReceipts({ poll }: { poll: boolean }) {
  const q = useSink(poll);
  const rows = q.data?.receipts ?? [];
  return (
    <Panel title="Odbiornik testowy (loopback)" actions={<InfoTip content="Wbudowany odbiornik pod /api/v1/integrations/webhooks/sink: sprawdza podpis HMAC i zapamiętuje 50 ostatnich odbiorów. Dane nie opuszczają komputera." />}>
      {q.isError ? <ApiErrorState error={q.error} resource="odbiory" compact /> : rows.length === 0 ? <div className="text-xs text-subtle">Brak odbiorów.</div> : (
        <ul className="space-y-1.5" data-testid="sink-receipts">
          {rows.slice(0, 20).map((r) => (
            <li key={`${r.delivery_id}-${r.at}`} className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
              <span className="font-mono text-muted">{formatWhen(r.at)}</span>
              <span className="font-mono">{r.event}</span>
              <TipPill variant={r.signature_ok ? "success" : "danger"} tip={r.signature_ok ? "Podpis HMAC zgodny z sekretem endpointu." : "Podpis niezgodny albo przeterminowany znacznik czasu."} testId="signature-pill">
                {r.signature_ok ? "Podpis poprawny" : "Podpis błędny"}
              </TipPill>
              <span className="text-subtle">{r.bytes} B</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

export function WebhooksTab() {
  const q = useWebhooks();
  const [burst, setBurst] = useState(0);
  const deliveries = useDeliveries(false);
  const active = (deliveries.data?.deliveries ?? []).some((d) => d.status === "pending" || d.status === "retrying");
  const poll = active || burst > Date.now();
  const d = q.data;
  if (!d) return q.isError ? <ApiErrorState error={q.error} resource="webhooki" endpoint="/v1/integrations/webhooks" /> : <Skeleton label="Wczytywanie webhooków…" lines={4} />;
  const labels = new Map(d.event_types.map((t) => [t.id, t.label_pl]));
  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="webhooks-tab">
      {!d.delivery_possible && (
        <div role="status" className="rounded-lg border border-warn/35 bg-warn/10 px-3 py-2 text-sm text-warn">
          Doręczenia poza ten komputer są zablokowane (tryb offline). Odbiornik testowy na 127.0.0.1 działa nadal.
        </div>
      )}
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        <AddEndpoint index={d} />
        <Panel title="Endpointy" actions={<span className="text-xs text-subtle">{d.endpoints.length}</span>}>
          {d.endpoints.length === 0 ? <EmptyState title="Brak endpointów" description="Dodaj endpoint albo użyj odbiornika testowego." compact /> : (
            <ul className="space-y-2" data-testid="endpoint-list">
              {d.endpoints.map((ep) => <EndpointRow key={ep.id} ep={ep} labels={labels} onTested={() => setBurst(Date.now() + 30_000)} />)}
            </ul>
          )}
        </Panel>
      </div>
      <DeliveryLog endpoints={d.endpoints} poll={poll} />
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        <SinkReceipts poll={poll} />
        <Panel title="Podpis żądań">
          <div className="space-y-2 text-sm text-muted">
            <p>{d.signature.scheme_pl}</p>
            <p className="text-xs">
              Nagłówki: <span className="font-mono">X-Hydra-Event</span>, <span className="font-mono">X-Hydra-Delivery</span>,{" "}
              <span className="font-mono">X-Hydra-Timestamp</span>, <span className="font-mono">{d.signature.header}</span> ={" "}
              <span className="font-mono">sha256=hex({d.signature.algo}(sekret, „znacznik.treść”))</span>. Odrzucaj żądania starsze niż 300 s.
            </p>
            <CodeViewer value={VERIFY_SNIPPET} label="Weryfikacja (Python)" maxHeight="14rem" />
          </div>
        </Panel>
      </div>
    </div>
  );
}
