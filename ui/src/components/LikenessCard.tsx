import { Link } from "react-router";
import { useLikeness, type LikenessCard as Card, type LikenessItem, type LikenessSignal, type LikenessState } from "../api/likeness";
import { isApiError } from "../api/client";
import { Badge, type BadgeVariant } from "./Badge";
import { Panel } from "./Panel";
import { isWholeFile, WHOLE_FILE_TEXT } from "../lib/displayPl";

const STATE: Record<LikenessState, { label: string; variant: BadgeVariant }> = {
  present: { label: "obecny", variant: "warning" },
  absent: { label: "nieobecny", variant: "neutral" },
  unknown: { label: "nie dało się sprawdzić", variant: "neutral" },
};

const NO_MARK_NOTE = "brak znaku nie świadczy o autentyczności";

function StateBadge({ state, label }: { state: LikenessState; label?: string | null }) {
  const s = STATE[state] ?? STATE.unknown;
  return <Badge variant={s.variant} dot className={state === "unknown" ? "opacity-70" : undefined}>{label || s.label}</Badge>;
}

const ROLE_PL: Record<string, string> = {
  tool_mark: "znak narzędzia, nie rodzaj treści; niewliczane do siły",
  hint: "słaba wskazówka; niewliczane do siły",
};

function evidenceLabel(it: LikenessItem): string {
  const e = it.evidence ?? {};
  const parts: string[] = [];
  if (e.artifact_id) parts.push(`artefakt ${e.artifact_id.slice(0, 10)}…`);
  const whole = isWholeFile(e, it.kind);
  if (whole) parts.push(WHOLE_FILE_TEXT);
  if (e.bbox && !whole) parts.push(`ramka ${e.bbox.map((v) => Math.round(v)).join(",")}`);
  if (e.char_range) parts.push(`znaki ${e.char_range[0]}–${e.char_range[1]}`);
  if (e.timecode_s !== null && e.timecode_s !== undefined && !whole) parts.push(`t=${e.timecode_s.toFixed(1).replace(".", ",")} s`);
  if (e.dom_path) parts.push(`DOM ${e.dom_path}`);
  if (e.note) parts.push(e.note);
  return parts.join(" · ");
}

function Item({ it }: { it: LikenessItem }) {
  const ev = evidenceLabel(it);
  return (
    <li className="text-xs">
      <span className="font-medium">{it.label_pl ?? it.value ?? "—"}</span>
      <span className="ml-1 font-mono text-2xs text-subtle">{it.family}/{it.kind}</span>
      {it.quality !== null && it.quality !== undefined && (
        <span className="ml-1 font-mono text-2xs text-subtle">jakość {it.quality.toFixed(2).replace(".", ",")}</span>
      )}
      {it.other_incident && (
        <span className="ml-1">
          także w{" "}
          <Link to={`/incident/${encodeURIComponent(it.other_incident)}`} className="font-mono text-accent hover:underline">{it.other_incident}</Link>
        </span>
      )}
      {ev && (
        <div className="truncate text-2xs text-muted" title={ev}>
          {it.evidence?.artifact_id ? <Link to={`/item/${it.evidence.artifact_id}`} className="text-accent hover:underline">{ev}</Link> : ev}
        </div>
      )}
    </li>
  );
}

function Signal({ s }: { s: LikenessSignal }) {
  return (
    <li className="rounded-md border border-line p-2" data-signal={s.key}>
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold">{s.label_pl}</span>
        <span className="ml-auto"><StateBadge state={s.state} label={s.state_label_pl} /></span>
      </div>
      <div className="mt-1 text-xs text-muted">{s.summary_pl}</div>
      {s.items.length > 0 && <ul className="mt-1 space-y-1">{s.items.slice(0, 8).map((it, i) => <Item key={i} it={it} />)}</ul>}
      {s.items.length > 8 && <div className="text-2xs text-subtle">… oraz {s.items.length - 8} kolejnych</div>}
      {s.checks.length > 0 && (
        <ul className="mt-2 space-y-1 border-t border-line pt-1">
          {s.checks.map((c) => (
            <li key={c.key} className="flex flex-wrap items-center gap-2 text-xs" data-check={c.key}>
              <span>{c.label_pl}</span>
              <StateBadge state={c.state} label={c.state_label_pl} />
              <span className="text-2xs text-subtle">{c.summary_pl}</span>
              {c.counted === false && c.role && ROLE_PL[c.role] && (
                <span className="text-2xs text-subtle italic">({ROLE_PL[c.role]})</span>
              )}
              {c.state === "absent" && (c.key === "c2pa" || c.key === "watermark" || c.key === "audio_watermark") && (
                <span className="text-2xs text-subtle italic" title={NO_MARK_NOTE}>({NO_MARK_NOTE})</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {s.hint_pl && <div className="mt-1 text-2xs text-subtle">{s.hint_pl}</div>}
    </li>
  );
}

export function LikenessCardView({ card }: { card: Card }) {
  return (
    <div className="space-y-2">
      <div className="rounded-md border border-warn/35 bg-warn/10 p-2 text-xs">{card.disclaimer_pl}</div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-subtle">Siła wskazania:</span>
        <span className="font-semibold">{card.strength}</span>
        <span className="font-mono text-muted">({card.present}/{card.total} sygnałów obecnych)</span>
        <Badge variant={card.triggered ? "warning" : "neutral"}>
          {card.triggered ? "osoba publiczna + obietnica finansowa" : "warunek karty niespełniony"}
        </Badge>
      </div>
      <ul className="space-y-2">{card.signals.map((s) => <Signal key={s.key} s={s} />)}</ul>
      {card.limits_pl.length > 0 && (
        <div className="text-2xs text-subtle">
          <div className="font-semibold">Ograniczenia:</div>
          <ul className="list-disc pl-4">{card.limits_pl.map((l, i) => <li key={i}>{l}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

/** Non-blocking panel for the incident page: a failing or missing endpoint never breaks the page. */
export function LikenessCard({ incidentId, runId }: { incidentId: string; runId?: string | null }) {
  const q = useLikeness(incidentId, runId);
  const title = q.data?.title_pl ?? "Nadużycie wizerunku — karta triażu analityka";
  let body;
  if (q.data) body = <LikenessCardView card={q.data} />;
  else if (q.isError)
    body = (
      <div className="text-xs text-subtle">
        {isApiError(q.error) && q.error.status === 404 ? "Karta niedostępna dla tego incydentu." : "Nie udało się pobrać karty (pozostała część strony działa)."}
      </div>
    );
  else body = <div className="text-xs text-subtle">Wczytywanie…</div>;
  return <Panel title={title}>{body}</Panel>;
}
