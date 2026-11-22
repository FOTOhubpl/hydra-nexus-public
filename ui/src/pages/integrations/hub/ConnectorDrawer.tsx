/** Detail drawer of one connector: what it does, what leaves the machine, secrets, test, dry-run preview, audit. */
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import {
  previewConnector,
  testConnector,
  useConnector,
  useIntegrationMutation,
  useSchedulerJobs,
  type Connector,
  type ConnectorDetail,
} from "../../../api/integrations";
import { ApiErrorState } from "../../../components/ApiErrorState";
import { Badge } from "../../../components/Badge";
import { Button } from "../../../components/Button";
import { Skeleton } from "../../../components/Skeleton";
import { formatBytes } from "../../../lib/format";
import { useSelectedRun } from "../../../lib/runSelection";
import { CampaignPicker, useExportDownload } from "./ConnectorCard";
import { AUTH_LABEL, DIRECTION_LABEL, formatWhen, LEVEL_LABEL, LEVEL_TIP, LIVE_NOTE, LEVEL_VARIANT, STATUS_LABEL, STATUS_TIP, STATUS_VARIANT } from "./labels";
import { KeyForm } from "./KeyForm";
import { CodeViewer, Sheet, Term, TipPill } from "./ui";

function Section({ title, tip, children }: { title: string; tip: ReactNode; children: ReactNode }) {
  return (
    <section className="min-w-0 space-y-1.5">
      <h3 className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-subtle uppercase">
        <Term tip={tip}>{title}</Term>
      </h3>
      <div className="min-w-0 text-sm text-fg">{children}</div>
    </section>
  );
}

function Preview({ c }: { c: Connector }) {
  const run = useSelectedRun();
  const [format, setFormat] = useState(c.formats[0] ?? "");
  const preview = useIntegrationMutation((_: void) => previewConnector(c.id, { run_id: run, format: format || undefined, limit: 50 }));
  const p = preview.data;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {c.formats.length > 1 && (
          <select value={format} onChange={(e) => setFormat(e.target.value)} aria-label="Format podglądu" className="h-7 rounded-[4px] border border-line bg-panel px-2 text-sm">
            {c.formats.map((f) => <option key={f} value={f}>{f}</option>)}
          </select>
        )}
        <Button onClick={() => preview.mutate()} disabled={preview.isPending} data-testid="preview-button">
          {preview.isPending ? "Przygotowanie…" : "Podgląd ładunku (na sucho)"}
        </Button>
      </div>
      {preview.isError && <div role="alert" className="text-xs break-words text-err">{(preview.error as Error).message}</div>}
      {p && (
        <div className="space-y-2" data-testid="preview-result">
          <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
            <dt className="text-subtle">Metoda</dt><dd className="font-mono">{p.method}</dd>
            <dt className="text-subtle">Cel</dt><dd className="font-mono break-all">{p.destination}</dd>
            <dt className="text-subtle">Format</dt><dd className="font-mono">{p.format} · {p.content_type}</dd>
            <dt className="text-subtle">Rozmiar</dt><dd className="font-mono">{formatBytes(p.bytes)}{p.truncated ? " (podgląd obcięty do 200 KB)" : ""}</dd>
            <dt className="text-subtle">SHA-256</dt><dd className="font-mono break-all">{p.sha256}</dd>
          </dl>
          {Object.keys(p.headers ?? {}).length > 0 && (
            <div className="min-w-0">
              <div className="mb-1 text-xs text-subtle">Nagłówki (sekrety zamaskowane)</div>
              <ul className="space-y-0.5 font-mono text-xs">
                {Object.entries(p.headers).map(([k, v]) => <li key={k} className="break-all"><span className="text-muted">{k}:</span> {v}</li>)}
              </ul>
            </div>
          )}
          <div className="flex min-w-0 flex-wrap gap-1">
            <span className="text-xs text-subtle">Wysyłane pola:</span>
            {p.fields_sent.map((f) => <Badge key={f} variant="info">{f}</Badge>)}
          </div>
          {p.redactions.length > 0 && (
            <div className="flex min-w-0 flex-wrap gap-1">
              <span className="text-xs text-subtle">Redakcje:</span>
              {p.redactions.map((r) => <Badge key={r} variant="warning">{r}</Badge>)}
            </div>
          )}
          <CodeViewer value={p.payload} label="Ładunek (nic nie zostało wysłane)" testId="preview-payload" />
        </div>
      )}
    </div>
  );
}

function MetaScheduler() {
  const q = useSchedulerJobs(true);
  const s = q.data;
  const job = s?.jobs ? Object.entries(s.jobs).find(([k]) => /adlib|meta/i.test(k)) : undefined;
  return (
    <div className="space-y-1 rounded-lg border border-line bg-raised p-3 text-xs" data-testid="meta-scheduler">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium text-fg">Harmonogram skanów biblioteki reklam</span>
        <Link to="/scheduler" className="font-medium text-accent hover:underline">Otwórz Harmonogram</Link>
      </div>
      {q.isLoading ? <div className="text-subtle">Wczytywanie stanu…</div> : !s || !s.available ? (
        <div className="text-subtle">Moduł harmonogramu nie jest dostępny w tym backendzie.</div>
      ) : job ? (
        <ul className="space-y-0.5">
          <li className="font-mono">{job[0]}</li>
          {Object.entries(job[1]).slice(0, 6).map(([k, v]) => <li key={k} className="break-all"><span className="text-subtle">{k}:</span> {typeof v === "object" ? JSON.stringify(v) : String(v)}</li>)}
        </ul>
      ) : (
        <div className="text-subtle">Zadanie biblioteki reklam jeszcze się nie wykonało.</div>
      )}
    </div>
  );
}

export function ConnectorDrawer({ id, onClose, offline }: { id: string | null; onClose: () => void; offline: boolean }) {
  const q = useConnector(id);
  const c = q.data;
  return (
    <Sheet open={Boolean(id)} onClose={onClose} title={c?.name ?? id ?? ""} testId="connector-drawer" wide>
      {!c ? (
        q.isError ? <ApiErrorState error={q.error} resource="szczegóły konektora" compact /> : <Skeleton label="Wczytywanie konektora…" lines={6} />
      ) : (
        <DrawerBody c={c} offline={offline} />
      )}
    </Sheet>
  );
}

function DrawerBody({ c, offline }: { c: ConnectorDetail; offline: boolean }) {
  const test = useIntegrationMutation((_: void) => testConnector(c.id));
  const planned = c.level === "planned";
  const exp = useExportDownload(c);
  const dir = DIRECTION_LABEL[c.direction] ?? DIRECTION_LABEL.both!;
  return (
    <div className="space-y-5 p-4">
      <div className="flex flex-wrap gap-1.5">
        <TipPill variant={LEVEL_VARIANT[c.level]} tip={LEVEL_TIP[c.level]}>{LEVEL_LABEL[c.level]}</TipPill>
        <TipPill variant={STATUS_VARIANT[c.status]} tip={STATUS_TIP[c.status]}>{STATUS_LABEL[c.status]}</TipPill>
        <Badge>{dir.arrow} {dir.label}</Badge>
      </div>
      {c.level === "live" && <p className="rounded-lg border border-line bg-hover px-3 py-2 text-xs text-muted" data-testid="drawer-live-note">Poziom „Na żywo (live)”: {LIVE_NOTE}. Fikstury testowe są przygotowane ręcznie z publicznej dokumentacji dostawcy.</p>}
      <p className="text-sm text-muted">{c.description_pl}</p>
      {c.notes_pl && <p className="text-xs text-subtle">{c.notes_pl}</p>}

      {!planned && (
        <div className="flex flex-wrap gap-2">
          {c.can_test && (
            <Button onClick={() => test.mutate()} disabled={test.isPending} data-testid="test-button">
              {test.isPending ? "Testowanie…" : "Testuj połączenie"}
            </Button>
          )}
          {c.level === "export-format" && (
            <>
              <CampaignPicker exp={exp} idPrefix={`drawer-${c.id}`} />
              <Button variant="primary" onClick={exp.download} disabled={exp.busy || !c.can_export || (exp.perCampaign && !exp.campaignId)}>{exp.busy ? "Pobieranie…" : "Pobierz pakiet"}</Button>
            </>
          )}
        </div>
      )}
      {test.data && (
        <div role="status" data-testid="test-result" className={test.data.ok ? "text-sm text-ok" : test.data.status === "offline" ? "text-sm text-warn" : "text-sm text-err"}>
          {test.data.ok ? "✓" : "✕"} {test.data.message_pl}{test.data.ms != null ? ` (${test.data.ms} ms)` : ""}
        </div>
      )}
      {test.isError && <div role="alert" className="text-sm text-err">{(test.error as Error).message}</div>}
      {exp.error && <div role="alert" className="text-xs text-err">{exp.error}</div>}
      {exp.done && <div role="status" className="text-xs text-ok">Pobrano {exp.done}</div>}

      {c.setup_pl.length > 0 && (
        <Section title="Konfiguracja krok po kroku" tip="Co trzeba zrobić u dostawcy i w HYDRA, aby konektor działał.">
          <ol className="list-decimal space-y-1 pl-5 text-sm">{c.setup_pl.map((s, i) => <li key={i}>{s}</li>)}</ol>
        </Section>
      )}

      <Section title="Jakie dane wychodzą" tip="Jedyne pola, jakie konektor może przekazać dostawcy. Wszystko inne zostaje na tym komputerze.">
        <div className="mb-1 flex flex-wrap gap-1">{c.data_out.length ? c.data_out.map((f) => <Badge key={f} variant="info">{f}</Badge>) : <span className="text-xs text-subtle">żadne</span>}</div>
        <p className="text-xs text-muted">{c.data_out_pl}</p>
      </Section>
      <Section title="Czego nigdy nie wysyłamy" tip="Twarda granica: tych danych żaden konektor nie wysyła (artefakty, OCR, pliki organizatora, nazwy plików, sekrety).">
        <p className="text-xs text-muted">{c.never_sent_pl}</p>
      </Section>

      <Section title="Uwierzytelnianie i klucze" tip="Klucze są w pliku ~/.config/hydra/<konektor>.env (0600) albo w zmiennej środowiskowej; API zwraca tylko informację o zapisaniu i 4 ostatnie znaki.">
        <div className="mb-1 text-xs text-muted">Typ: {AUTH_LABEL[c.auth] ?? c.auth}</div>
        {c.can_connect && (c.secrets.length > 0 || c.level === "live") ? (
          <KeyForm connector={c} offline={offline} idPrefix="drawer" />
        ) : c.secrets.length === 0 ? <div className="text-xs text-subtle">Brak wymaganych kluczy.</div> : (
          <ul className="space-y-1.5">
            {c.secrets.map((s) => (
              <li key={s.name} className="flex min-w-0 flex-wrap items-center gap-2 text-xs" data-testid={`secret-${s.name}`}>
                <span className="font-mono">{s.name}</span>
                <Badge variant={s.configured ? "success" : "neutral"} dot>{s.configured ? "zapisany" : "brak"}</Badge>
                {s.configured && <span className="text-muted">{s.source === "env" ? "zmienna środowiskowa" : "plik"}{s.last4 ? ` · …${s.last4}` : ""}</span>}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <div className="grid min-w-0 gap-4 sm:grid-cols-2">
        <Section title="Limit zapytań" tip="Limit dostawcy; HYDRA wstrzymuje zapytania ponad limit (licznik z 60 minut).">
          <p className="text-xs text-muted">{c.rate_limit_pl || "—"}</p>
          {c.usage && <p className="font-mono text-xs text-muted">{c.usage.requests_60m} zapytań w 60 min</p>}
        </Section>
        <Section title="Licencja i regulamin" tip="Warunki korzystania z danych dostawcy; sprawdź przed użyciem produkcyjnym.">
          <p className="text-xs text-muted">{c.licence_pl || "—"}</p>
        </Section>
      </div>
      {c.docs_url && (
        <Section title="Dokumentacja dostawcy" tip="Zwykły link do dokumentacji; otwiera się w nowej karcie, HYDRA nic nie pobiera.">
          <a href={c.docs_url} target="_blank" rel="noreferrer noopener" className="text-xs break-all text-accent hover:underline">{c.docs_url}</a>
        </Section>
      )}

      {c.id === "meta-adlibrary" && <MetaScheduler />}

      {!planned && (
        <Section title="Podgląd ładunku" tip="Próba na sucho: dokładnie to, co zostałoby wysłane lub zapisane, z maskowaniem sekretów. Nic nie wychodzi.">
          <Preview c={c} />
        </Section>
      )}

      {!planned && (
        <Section title="Ostatnie uruchomienia" tip="Testy, zapytania i eksporty tego konektora.">
          {c.runs?.length ? (
            <ul className="space-y-1 text-xs">
              {c.runs.slice(0, 10).map((r, i) => (
                <li key={i} className="flex min-w-0 flex-wrap gap-2">
                  <span className={r.ok ? "text-ok" : "text-err"}>{r.ok ? "✓" : "✕"}</span>
                  <span className="font-mono text-muted">{formatWhen(r.at)}</span>
                  <span className="min-w-0 break-words">{r.message_pl ?? r.status}</span>
                </li>
              ))}
            </ul>
          ) : <div className="text-xs text-subtle">Brak uruchomień.</div>}
        </Section>
      )}
      <Section title="Dziennik audytu" tip="Ostatnie wpisy INTEGRATION_* z łańcucha audytu (bez sekretów i bez ścieżek z parametrami).">
        {c.audit?.length ? (
          <ul className="space-y-1 text-xs" data-testid="audit-list">
            {c.audit.map((a) => (
              <li key={a.seq} className="min-w-0">
                <span className="font-mono text-subtle">#{a.seq} {formatWhen(a.ts)}</span> <span className="font-medium">{a.type}</span>
                <div className="font-mono break-all text-muted">{JSON.stringify(a.payload)}</div>
              </li>
            ))}
          </ul>
        ) : <div className="text-xs text-subtle">Brak wpisów.</div>}
      </Section>
    </div>
  );
}
