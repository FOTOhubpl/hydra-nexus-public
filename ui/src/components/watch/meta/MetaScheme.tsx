/**
 * P1 "Nowa reklama, ten sam schemat": the versions of one ad (text / domain / media changes, identifiers kept), the
 * ads the analysis linked to the same scheme with their shared rare identifiers, and side by side the look-alike ads
 * the system deliberately did not merge, with the reason. Values come from GET /ads/{id}/scheme; every entry is a
 * hypothesis to verify, never an operator attribution. Hosts are defanged text, never links.
 */
import { errorPl, timePl } from "../../../api/watchRt";
import { defang, useMetaScheme, type MetaMode, type SchemeShared } from "../../../api/metaAds";
import { ModeBadge, numPl, short } from "./common";

function Shared({ s }: { s: SchemeShared }) {
  const hosts = s.hosts ?? [];
  const phones = s.phones ?? [];
  const media = s.media_sha256 ?? [];
  if (!hosts.length && !phones.length && !media.length) return <span className="text-muted">brak wspólnych identyfikatorów</span>;
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-0.5">
      {hosts.length > 0 && <span>hosty: <span className="font-mono">{hosts.map(defang).join(", ")}</span></span>}
      {phones.length > 0 && <span>telefony: <span className="font-mono">{phones.join(", ")}</span></span>}
      {media.length > 0 && <span>materiał (SHA-256): <span className="font-mono">{media.map((m) => short(m)).join(", ")}</span></span>}
    </span>
  );
}

export function MetaScheme({ mode, adId }: { mode: MetaMode; adId: string }) {
  const q = useMetaScheme(mode, adId);
  if (q.isPending) return <p className="text-xs text-muted">Wczytywanie schematu…</p>;
  if (q.isError) return <p role="alert" className="text-xs text-err">{errorPl(q.error, "Nie udało się wczytać schematu.")}</p>;
  const d = q.data;
  return (
    <section data-testid="meta-scheme" className="rounded-lg border border-line p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h4 className="text-sm font-semibold text-fg">Nowa reklama, ten sam schemat</h4>
        <ModeBadge mode={d.mode} />
        {d.decision && <span className="font-mono text-xs text-muted">{d.decision}{d.campaign_id ? ` · ${d.campaign_id}` : ""}</span>}
      </div>
      {d.note_pl && <p className="mb-2 text-2xs text-muted" data-testid="scheme-note">{d.note_pl}</p>}

      <div className="mb-3">
        <div className="text-xs font-semibold text-fg">Wersje tej reklamy</div>
        {d.versions.length === 0 ? (
          <p className="text-2xs text-muted">Jedna rewizja: brak zmian do pokazania.</p>
        ) : (
          <ol className="mt-1 space-y-1 text-xs" data-testid="scheme-versions">
            {d.versions.map((v) => (
              <li key={`${v.from_rev}-${v.to_rev}`} className="rounded border border-line p-2">
                <div className="flex flex-wrap gap-2">
                  <span className="font-semibold text-fg">rewizja {v.from_rev} → {v.to_rev}</span>
                  <span className="text-muted">{timePl(v.observed_at)}</span>
                  <span className="text-muted">tekst: {v.text_changed ? "zmieniony" : "bez zmian"}</span>
                  <span className="text-muted">materiał: {v.media_changed ? "zmieniony" : "bez zmian"}</span>
                </div>
                {v.domain_change && (v.domain_change.removed.length > 0 || v.domain_change.added.length > 0) && (
                  <div className="mt-1" data-testid="domain-change">
                    zmiana domeny: <span className="font-mono text-err">{v.domain_change.removed.map(defang).join(", ") || "—"}</span>
                    {" → "}<span className="font-mono text-ok">{v.domain_change.added.map(defang).join(", ") || "—"}</span>
                  </div>
                )}
                {v.kept.phones.length > 0 && <div className="mt-1 text-muted">zachowane telefony: <span className="font-mono">{v.kept.phones.join(", ")}</span></div>}
              </li>
            ))}
          </ol>
        )}
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <div data-testid="scheme-same">
          <div className="text-xs font-semibold text-fg">Ten sam schemat (powiązane przez analizę)</div>
          {d.same_scheme.length === 0 ? (
            <p className="text-2xs text-muted">Brak innych reklam powiązanych z tym schematem.</p>
          ) : (
            <ul className="mt-1 space-y-1 text-xs">
              {d.same_scheme.map((x) => (
                <li key={x.ad_id} className="rounded border border-line p-2">
                  <div className="flex flex-wrap gap-2">
                    <span className="font-mono text-fg">{x.ad_id}</span>
                    {x.decision && <span className="text-muted">{x.decision}{x.campaign_id ? ` · ${x.campaign_id}` : ""}</span>}
                    {x.analysis_id != null && <span className="text-muted">analiza #{x.analysis_id}</span>}
                  </div>
                  <div className="mt-1 text-2xs"><Shared s={x.shared} /></div>
                  {x.evidence.length > 0 && (
                    <ul className="mt-1 text-2xs text-muted">
                      {x.evidence.map((e, i) => (
                        <li key={`${e.kind}-${i}`}><span className="font-mono">{e.kind}</span> {e.kind.includes("host") || e.kind.includes("domain") ? defang(e.value) : e.value}{e.bits != null ? ` · ${numPl(e.bits)} bit` : ""}{e.level ? ` · ${e.level}` : ""}</li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div data-testid="scheme-lookalike">
          <div className="text-xs font-semibold text-fg">Podobne, ale nie scalone</div>
          {d.lookalike_not_merged.length === 0 ? (
            <p className="text-2xs text-muted">Brak podobnych reklam odrzuconych przez reguły korelacji.</p>
          ) : (
            <ul className="mt-1 space-y-1 text-xs">
              {d.lookalike_not_merged.map((x) => (
                <li key={x.ad_id} className="rounded border border-line p-2">
                  <div className="flex flex-wrap gap-2">
                    <span className="font-mono text-fg">{x.ad_id}</span>
                    {x.decision && <span className="text-muted">{x.decision}{x.campaign_id ? ` · ${x.campaign_id}` : ""}</span>}
                  </div>
                  <div className="mt-1 text-2xs"><Shared s={x.shared} /></div>
                  <p className="mt-1 text-fg">{x.why_not_merged_pl}</p>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}
