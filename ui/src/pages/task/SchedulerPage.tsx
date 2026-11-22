import { useState } from "react";
import { useSchedulerRun, useSchedulerStatus, useWatchlist, useWatchlistChange, type WatchItem } from "../../api/task";
import { Button } from "../../components/Button";
import { Panel } from "../../components/Panel";
import { PropertyList } from "../../components/PropertyList";
import { Page, QueryState } from "./common";

const KIND_LABEL: Record<WatchItem["kind"], string> = { keyword: "słowo kluczowe", domain: "domena" };

export function SchedulerPage() {
  const list = useWatchlist();
  const status = useSchedulerStatus();
  const change = useWatchlistChange();
  const runNow = useSchedulerRun();
  const [kind, setKind] = useState<WatchItem["kind"]>("keyword");
  const [value, setValue] = useState("");
  const s = status.data;
  return (
    <Page title="Harmonogram i obserwowane" sub="skan bibliotek reklam i ponowne wzbogacanie znanych domen (moduł bonusowy)">
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Lista obserwowanych">
          <form className="mb-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); if (value.trim().length >= 2) change.mutate({ add: [{ kind, value: value.trim() }] }, { onSuccess: () => setValue("") }); }}>
            <select value={kind} onChange={(e) => setKind(e.target.value as WatchItem["kind"])} aria-label="Rodzaj" className="h-7 rounded-[4px] border border-line bg-panel px-2 text-sm">
              {Object.entries(KIND_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
            <input value={value} onChange={(e) => setValue(e.target.value)} placeholder="wartość" aria-label="Wartość" className="h-7 min-w-0 flex-1 rounded-[4px] border border-line bg-raised px-2 text-sm" />
            <Button type="submit" disabled={change.isPending}>Dodaj</Button>
          </form>
          {!list.data ? <QueryState query={list} what="listę obserwowanych" endpoint="/v1/watchlist" /> : list.data.items.length === 0 ? <div className="text-xs text-subtle">Lista jest pusta.</div> : (
            <ul className="space-y-1 text-sm">
              {list.data.items.map((i) => (
                <li key={`${i.kind}|${i.value}`} className="flex items-center gap-2">
                  <span className="w-28 text-xs text-subtle">{KIND_LABEL[i.kind]}</span>
                  <span className="font-mono">{i.value}</span>
                  <button type="button" className="ml-auto text-xs text-err hover:underline" onClick={() => change.mutate({ remove: [{ kind: i.kind, value: i.value }] })}>usuń</button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel title="Stan harmonogramu">
          {!s ? <QueryState query={status} what="stan harmonogramu" endpoint="/v1/scheduler/status" /> : !s.available ? (
            <div className="text-sm text-subtle">Moduł harmonogramu nie jest dostępny w tej wersji backendu.</div>
          ) : (
            <div className="space-y-3 text-sm">
              <div className="flex flex-wrap gap-3 text-xs">
                <span>Token Meta Ad Library: <b>{s.meta_token_configured ? "skonfigurowany" : "brak (tylko import ręczny przez skrzynkę)"}</b></span>
                <span>Wzbogacanie na żywo: <b>{s.live_enrichment ? "tak" : "nie (replay)"}</b></span>
                <span>Skrzynka do analizy: <b>{s.inbox_pending ?? 0}</b> plików</span>
                <Button type="button" className="ml-auto" disabled={runNow.isPending || Boolean(s.running)} onClick={() => runNow.mutate()}>
                  {s.running ? "Trwa uruchomienie…" : "Uruchom teraz"}
                </Button>
              </div>
              {runNow.data && !runNow.data.started && <div className="text-xs text-muted">{runNow.data.note ?? "Nie uruchomiono."}</div>}
              {Object.keys(s.jobs ?? {}).length === 0 ? <div className="text-xs text-subtle">Żadne zadanie harmonogramu jeszcze się nie wykonało (CLI <span className="font-mono">hydra scheduler</span>).</div> : (
                Object.entries(s.jobs ?? {}).map(([name, job]) => (
                  <div key={name} className="rounded-md border border-line">
                    <div className="border-b border-line bg-raised px-2 py-1 text-xs font-semibold">{name}</div>
                    <PropertyList data={job} />
                  </div>
                ))
              )}
            </div>
          )}
        </Panel>
      </div>
    </Page>
  );
}
