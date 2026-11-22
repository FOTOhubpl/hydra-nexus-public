/**
 * "Na żywo" tab of /watch: the realtime watch engine (WATCH-RT, /v1/watch/rt/*). Engine state and controls, source
 * health, the realtime feed (stream events + paging), watchlist matches with analyst actions, the watchlist manager,
 * the 48-hour timeline and the demo (replay) mode. Replay is always labelled; it is never shown as live data.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "../../components/Button";
import { Panel } from "../../components/Panel";
import { StatTile } from "../../components/StatTile";
import { Sparkline } from "../../components/charts";
import { Tooltip, InfoTip } from "../../components/Tooltip";
import { EnginePill, ReplayBanner, SourceCard, TimelineChart, useNow } from "../../components/watch/parts";
import { Feed } from "../../components/watch/Feed";
import { MatchPanel } from "../../components/watch/MatchPanel";
import { Watchlist } from "../../components/watch/Watchlist";
import { errorPl, rtApi, rtKeys, useRtFixtures, useRtMatches, useRtStatus, watchRtBus, type RtItem, type RtStatus } from "../../api/watchRt";
import { Page, QueryState } from "./common";

const SPEEDS = [1, 10, 60, 600];

function Kpi({ tip, children, className }: { tip: string; children: ReactNode; className?: string }) {
  return <Tooltip focusable content={tip} className={`block min-w-0 ${className ?? ""}`}>{children}</Tooltip>;
}

function EngineControls({ s }: { s: RtStatus }) {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<string | null>(null);
  const act = useMutation({
    mutationFn: (a: "start" | "pause" | "resume") => rtApi.engine(a),
    onSuccess: (r) => { if (r?.engine) qc.setQueryData(rtKeys.status, r); setMsg(null); void qc.invalidateQueries({ queryKey: rtKeys.status }); },
    onError: (e) => setMsg(errorPl(e)),
  });
  const st = s.engine.state;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button variant="primary" disabled={act.isPending || st !== "stopped"} onClick={() => act.mutate("start")}>Uruchom</Button>
      <Button disabled={act.isPending || st === "paused" || st === "stopped" || st === "replay"} onClick={() => act.mutate("pause")}>Wstrzymaj</Button>
      <Button disabled={act.isPending || st !== "paused"} onClick={() => act.mutate("resume")}>Wznów</Button>
      {msg && <span role="alert" className="text-xs text-err">{msg}</span>}
    </div>
  );
}

function DemoControl({ s }: { s: RtStatus }) {
  const qc = useQueryClient();
  const fx = useRtFixtures();
  const fixtures = fx.data?.fixtures ?? [];
  const [fixture, setFixture] = useState("");
  const [speed, setSpeed] = useState(60);
  const [msg, setMsg] = useState<string | null>(null);
  const chosen = fixture || fixtures[0]?.name || "";
  const onStatus = (r: RtStatus) => { if (r?.engine) qc.setQueryData(rtKeys.status, r); setMsg(null); void qc.invalidateQueries({ queryKey: rtKeys.all }); };
  const start = useMutation({ mutationFn: () => rtApi.replay(chosen, speed), onSuccess: onStatus, onError: (e) => setMsg(errorPl(e)) });
  const stop = useMutation({ mutationFn: () => rtApi.replayStop(), onSuccess: onStatus, onError: (e) => setMsg(errorPl(e)) });
  const running = s.engine.state === "replay" && !s.engine.replay?.done;
  const desc = fixtures.find((f) => f.name === chosen);
  return (
    <Panel title={<span className="inline-flex items-center gap-2">Tryb demonstracyjny (odtworzenie) <InfoTip content="Odtwarza zapisany zestaw zdarzeń z wybraną prędkością przez ten sam silnik, ze strumieniem i dopasowaniami. Każda pozycja z odtworzenia jest oznaczona i nie jest danymi z sieci na żywo." /></span>}>
      <div className="flex flex-wrap items-center gap-2" data-testid="demo-control">
        <label className="sr-only" htmlFor="rt-fixture">Zestaw do odtworzenia</label>
        <select id="rt-fixture" value={chosen} onChange={(e) => setFixture(e.target.value)} className="h-7 min-w-0 rounded-[4px] border border-line bg-panel px-2 text-sm" data-testid="demo-fixture">
          {fixtures.length === 0 && <option value="">{fx.isPending ? "wczytywanie…" : "brak zestawów"}</option>}
          {fixtures.map((f) => <option key={f.name} value={f.name}>{f.name}{f.synthetic ? " (syntetyczny)" : " (ODTWORZENIE, nagranie z sieci)"}{f.ticks ? ` · ${f.ticks} kroków` : ""}</option>)}
        </select>
        <label className="sr-only" htmlFor="rt-speed">Prędkość odtwarzania</label>
        <select id="rt-speed" value={speed} onChange={(e) => setSpeed(Number(e.target.value))} className="h-7 rounded-[4px] border border-line bg-panel px-2 text-sm" data-testid="demo-speed">
          {SPEEDS.map((v) => <option key={v} value={v}>{v}×</option>)}
        </select>
        <Button variant="primary" disabled={!chosen || start.isPending} onClick={() => start.mutate()} data-testid="demo-start">Start</Button>
        <Button disabled={stop.isPending || (!running && s.engine.state !== "replay")} onClick={() => stop.mutate()} data-testid="demo-stop">Stop</Button>
      </div>
      {desc?.description_pl && <p className="mt-2 text-xs text-muted">{desc.description_pl}</p>}
      {msg && <p role="alert" className="mt-2 text-xs text-err">{msg}</p>}
    </Panel>
  );
}

export function WatchLive() {
  const status = useRtStatus();
  const qc = useQueryClient();
  const now = useNow(1000);
  const [selectedItem, setSelectedItem] = useState<number | null>(null);
  const [selectedMatch, setSelectedMatch] = useState<number | null>(null);
  const allMatches = useRtMatches("");

  // poll results change totals and sparkline: refetch the status, at most every 1.5 s
  const lastRefetch = useRef(0);
  useEffect(() => watchRtBus.subscribe((e) => {
    if (e.type !== "poll" && e.type !== "match") return;
    const t = Date.now();
    if (t - lastRefetch.current < 1500) return;
    lastRefetch.current = t;
    void qc.invalidateQueries({ queryKey: rtKeys.status });
  }), [qc]);

  const onSelectItem = (it: RtItem) => {
    setSelectedItem(it.id);
    const m = (allMatches.data?.matches ?? []).find((x) => x.item_id === it.id);
    if (m) setSelectedMatch(m.id);
  };
  // a stream match for the selected item (arrived after the click) selects itself
  useEffect(() => watchRtBus.subscribe((e) => {
    if (e.type === "match" && e.match.item_id === selectedItem) setSelectedMatch((cur) => cur ?? e.match.id);
  }), [selectedItem]);

  const s = status.data;
  if (!s) {
    return (
      <Page title="Monitoring na żywo">
        <QueryState query={status} what="stan silnika monitoringu" endpoint="/v1/watch/rt/status" />
      </Page>
    );
  }
  const spark = s.sparkline.map((p) => p.new);
  const fetched60 = s.sparkline.reduce((a, p) => a + p.fetched, 0);
  const new60 = s.sparkline.reduce((a, p) => a + p.new, 0);
  const matched60 = s.sparkline.reduce((a, p) => a + p.matched, 0);

  return (
    <Page
      title={<span className="flex flex-wrap items-center gap-2">Monitoring na żywo <EnginePill engine={s.engine} /></span>}
      sub={s.engine.message_pl ? <span data-testid="engine-message">{s.engine.message_pl}</span> : undefined}
      actions={<EngineControls s={s} />}
    >
      <ReplayBanner engine={s.engine} />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <Kpi tip="Wszystkie pozycje zapisane przez silnik. Wykres: nowe pozycje na minutę w ostatnich 60 minutach (z zapisanych przebiegów).">
          <StatTile className="h-full" label="Pozycje" value={s.totals.items} hint={`60 min: ${new60} nowych (pobrano ${fetched60})`} aside={<Sparkline values={spark} width={84} label="nowe pozycje na minutę, ostatnie 60 min" />} />
        </Kpi>
        <Kpi tip="Nowe pozycje z ostatnich 24 godzin.">
          <StatTile className="h-full" label="Pozycje (24 h)" value={s.totals.items_24h} />
        </Kpi>
        <Kpi tip="Wszystkie dopasowania pozycji do watchlisty (kandydaci do weryfikacji, nie potwierdzone przypisania).">
          <StatTile className="h-full" label="Dopasowania" value={s.totals.matches} hint={`60 min: ${matched60}`} />
        </Kpi>
        <Kpi tip="Dopasowania, których analityk jeszcze nie potwierdził ani nie odrzucił.">
          <StatTile className="h-full" label="Nowe dopasowania" value={s.totals.matches_new} />
        </Kpi>
        <Kpi className="col-span-2 md:col-span-1" tip="Liczba wskaźników na watchliście (z kampanii, ręcznych i z odtworzenia).">
          <StatTile label="Watchlista" value={s.totals.watchlist} className="h-full" />
        </Kpi>
      </div>

      <section aria-labelledby="rt-sources-title" className="space-y-2">
        <h2 id="rt-sources-title" className="text-sm font-semibold text-fg">Źródła</h2>
        {s.sources.length === 0 ? <p className="text-sm text-subtle">Silnik nie zgłosił żadnego źródła.</p> : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-5">
            {s.sources.map((src) => <SourceCard key={src.id} src={src} fetchedAt={status.dataUpdatedAt} now={now} />)}
          </div>
        )}
      </section>

      <div className="grid items-start gap-3 xl:grid-cols-[minmax(0,1fr)_26rem]">
        <Feed sources={s.sources} selectedItemId={selectedItem} onSelect={onSelectItem} />
        <MatchPanel selectedId={selectedMatch} onSelect={setSelectedMatch} />
      </div>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_26rem]">
        <Panel title={<span className="inline-flex items-center gap-2">Ostatnie 48 godzin <InfoTip content="Nowe pozycje i pozycje dopasowane do watchlisty w każdej godzinie, z zapisanych przebiegów silnika." /></span>}>
          <TimelineChart points={s.timeline} />
        </Panel>
        <DemoControl s={s} />
      </div>

      <Watchlist />
    </Page>
  );
}
