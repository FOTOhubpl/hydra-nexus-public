import { Link, useSearchParams } from "react-router";
import { useSearch } from "../../api/task";
import { FamChip } from "../../components/evidence";
import { Panel } from "../../components/Panel";
import { CampaignLink, IncidentLink, Page, QueryState } from "./common";

export function SearchPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const res = useSearch(q);
  return (
    <Page title="Wyszukiwanie" sub="dowolna wartość genu → wszystkie incydenty, które ją mają">
      <input defaultValue={q} key={q} onKeyDown={(e) => { if (e.key === "Enter") setParams({ q: (e.target as HTMLInputElement).value }); }} placeholder="domena, fraza, hash, identyfikator piksela… (Enter)" aria-label="Zapytanie" className="h-8 max-w-xl rounded-[4px] border border-line bg-panel px-3 text-sm focus:border-accent focus:outline-none" />
      {q.trim().length < 2 ? <div className="text-sm text-subtle">Wpisz co najmniej 2 znaki.</div> : !res.data ? <QueryState query={res} what="wyniki" endpoint="/v1/search" /> : (
        <div className="grid gap-3 xl:grid-cols-[1fr_320px]">
          <Panel title={`Geny (${res.data.genes.length})`} flush>
            {res.data.genes.length === 0 ? <div className="p-3 text-sm text-subtle">Brak genów z tą wartością.</div> : (
              <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs">
                <thead className="bg-raised text-2xs tracking-wider text-subtle uppercase"><tr><th className="px-2 py-1.5 text-left">Rodzina</th><th className="px-2 py-1.5 text-left">Rodzaj</th><th className="px-2 py-1.5 text-left">Wartość</th><th className="px-2 py-1.5 text-right">DF</th><th className="px-2 py-1.5 text-left">Incydenty</th></tr></thead>
                <tbody>
                  {res.data.genes.map((g, i) => (
                    <tr key={i} className="border-t border-line align-top">
                      <td className="px-2 py-1"><FamChip family={g.family} /></td>
                      <td className="px-2 py-1 text-muted">{g.kind}</td>
                      <td className="max-w-[360px] px-2 py-1 font-mono break-all">{g.value}</td>
                      <td className="px-2 py-1 text-right font-mono">{g.doc_freq}</td>
                      <td className="px-2 py-1"><span className="flex flex-wrap gap-x-2">{g.incidents.slice(0, 30).map((id) => <IncidentLink key={id} id={id} />)}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
          </Panel>
          <div className="flex flex-col gap-3">
            <Panel title={`Kampanie (${res.data.campaigns.length})`}>
              <ul className="space-y-1 text-sm">{res.data.campaigns.map((c) => <li key={c.id}><CampaignLink id={c.id} label={c.label} /></li>)}</ul>
            </Panel>
            <Panel title={`Incydenty (${res.data.incidents.length})`}>
              <ul className="space-y-1 text-sm">{res.data.incidents.map((i) => <li key={i.id}><IncidentLink id={i.id} /> <span className="text-muted">{i.title}</span></li>)}</ul>
            </Panel>
            <Link to="/graph" className="text-sm text-accent hover:underline">Pokaż graf →</Link>
          </div>
        </div>
      )}
    </Page>
  );
}
