/** Evidence primitives (DESIGN.md §5): provenance pill, family chip, waterfall, probability bar, thumbnails. */
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { thumbUrl } from "../../api/artifacts";
import type { GeneMatch, Provenance } from "../../api/task";
import { cx } from "../../lib/cx";
import { clearThumbFailure, markThumbFailed, thumbFailed } from "../../lib/thumbFailures";
import { openGene } from "../../lib/geneExplain";
import {
  DECISION_LABEL,
  FAMILY_VAR,
  familyLabel,
  formatBits,
  formatProbability,
  reasonKindLabel,
  GENE_STATUS_LABEL,
  HYPOTHESIS_LABEL,
  waterfallSteps,
  type WaterfallStep,
} from "../../lib/evidence";

const PROV_TITLE: Record<string, string> = {
  OBS: "OBS: zaobserwowane w materiale",
  INF: "INF: wyliczone przez model lub obliczenie",
  HYP: "HYP: hipoteza warstwy oceny",
};

export function Prov({ value }: { value: Provenance | string | null | undefined }) {
  const v = value ?? "INF";
  return (
    <span
      title={PROV_TITLE[v] ?? v}
      className={cx(
        "inline-flex h-4 shrink-0 items-center rounded-[3px] border px-1 font-mono text-2xs leading-none font-semibold",
        v === "OBS" && "border-line-strong bg-panel text-fg",
        v === "INF" && "border-accent/40 bg-for-soft text-accent",
        v === "HYP" && "border-dashed border-subtle bg-panel text-muted",
      )}
    >
      {v}
    </span>
  );
}

export function FamChip({ family, withName = false, className }: { family: string; withName?: boolean; className?: string }) {
  return (
    <span className={cx("inline-flex items-center gap-1.5 whitespace-nowrap", className)} title={`${family} · ${familyLabel(family)}`}>
      <span aria-hidden="true" className="size-2.5 shrink-0 rounded-[2px]" style={{ backgroundColor: FAMILY_VAR[family] ?? "var(--color-unknown)" }} />
      <span className="font-mono text-xs font-semibold text-fg">{family}</span>
      {withName && <span className="text-xs text-muted">{familyLabel(family)}</span>}
    </span>
  );
}

export function GeneStatusChip({ status }: { status: string }) {
  const tone =
    status === "observed"
      ? "border-line-strong text-fg"
      : status === "failed"
        ? "border-err/40 text-err"
        : "border-line text-subtle italic";
  return (
    <span className={cx("inline-flex h-[18px] items-center rounded-[3px] border px-1.5 text-2xs whitespace-nowrap", tone)}>
      {GENE_STATUS_LABEL[status] ?? status}
    </span>
  );
}

export function HypothesisBadge({ value }: { value: string }) {
  const abstain = value === "abstain";
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <Prov value="HYP" />
      <span
        className={cx(
          "inline-flex h-[18px] items-center rounded-[3px] border px-1.5 text-xs",
          value === "same_campaign" && "border-for/40 bg-for-soft text-fg",
          abstain && "border-abstain bg-abstain-soft text-abstain-ink",
          (value === "independent" || value === "shared_template" || value === "shared_asset") && "border-line-strong bg-raised text-muted",
        )}
      >
        {abstain && <span aria-hidden="true" className="mr-1 font-semibold">?</span>}
        {HYPOTHESIS_LABEL[value] ?? value}
      </span>
    </span>
  );
}

export function DecisionBadge({ value, large = false }: { value: string; large?: boolean }) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 rounded-full border font-semibold whitespace-nowrap",
        large ? "h-8 px-3.5 text-base" : "h-[20px] px-2 text-xs",
        value === "assign" && "border-for bg-for-soft text-fg",
        value === "new_campaign" && "border-line-strong bg-raised text-fg",
        value === "abstain" && "hatch-abstain border-abstain text-abstain-ink",
      )}
    >
      <span aria-hidden="true">{value === "assign" ? "✓" : value === "abstain" ? "?" : "＋"}</span>
      <span className={value === "abstain" ? "rounded-full bg-abstain-soft px-1" : undefined}>{DECISION_LABEL[value] ?? value}</span>
    </span>
  );
}

export function ReviewBadge({ value }: { value: "accepted" | "rejected" | "unsure" | null | undefined }) {
  if (!value) return null;
  return (
    <span
      className={cx(
        "inline-flex h-[18px] items-center gap-1 rounded-full border px-2 text-2xs font-semibold uppercase",
        value === "accepted" ? "border-for bg-for-soft text-fg" : value === "rejected" ? "border-against bg-against-soft text-fg" : "border-abstain bg-abstain-soft text-abstain-ink",
      )}
      title={value === "unsure" ? "Analityk oznaczył jako niepewne (dziennik audytu); kampanie bez zmian" : "Decyzja analityka (zapisana w dzienniku audytu)"}
    >
      {value === "accepted" ? "✓ potwierdzone" : value === "rejected" ? "✕ odrzucone" : "? niepewne"}
    </span>
  );
}

/** Probability on a 0..1 track with the abstain band hatched. Not calibrated → says so. */
export function ProbBar({ p, calibrated, band = [0.2, 0.8] }: { p: number; calibrated: boolean; band?: [number, number] }) {
  const pct = Math.max(0, Math.min(1, p)) * 100;
  return (
    <div className="space-y-1" data-testid="prob-bar">
      <div className="text-2xs font-medium text-muted">{calibrated ? "prawdopodobieństwo (skalibrowane)" : "wynik nieskalibrowany (nie prawdopodobieństwo)"}</div>
      <div className="relative h-2.5 w-full rounded-full bg-hover">
        <div className="absolute inset-y-0 rounded-full bg-unknown/50" style={{ left: 0, width: `${band[0] * 100}%` }} />
        <div className="hatch-abstain absolute inset-y-0 opacity-60" style={{ left: `${band[0] * 100}%`, width: `${(band[1] - band[0]) * 100}%` }} />
        <div className="absolute inset-y-0 rounded-full bg-for/60" style={{ left: `${band[1] * 100}%`, right: 0 }} />
        <div className="absolute top-1/2 size-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-panel bg-fg" style={{ left: `${pct}%` }} />
      </div>
      <div className="flex justify-between text-2xs text-subtle">
        <span>odrzuć</span>
        <span>kolejka analityka</span>
        <span>połącz</span>
      </div>
      {!calibrated && <div className="text-2xs text-subtle">Wynik nieskalibrowany: to ranking z bitów, nie prawdopodobieństwo.</div>}
    </div>
  );
}

export function Probability({ p, calibrated }: { p: number; calibrated?: boolean }) {
  return (
    <span className="font-mono tabular-nums" title={calibrated ? "prawdopodobieństwo (skalibrowane)" : "wynik nieskalibrowany (ranking z bitów, nie prawdopodobieństwo)"} aria-label={`${calibrated ? "prawdopodobieństwo (skalibrowane)" : "wynik nieskalibrowany"} ${formatProbability(p)}`}>
      {formatProbability(p)}
      {calibrated === false && <span className="ml-0.5 text-subtle">*</span>}
    </span>
  );
}

/** Per-family bits mini bars (for lists). */
export function FamilyBars({ bits, families = ["VIS", "TXT", "INF", "ID", "NAR", "META"] }: { bits: Record<string, number>; families?: readonly string[] }) {
  const max = Math.max(1, ...Object.values(bits).map((v) => Math.abs(v)));
  return (
    <span className="inline-flex items-end gap-0.5" aria-label="bity per rodzina">
      {families.map((f) => {
        const v = bits[f];
        const h = v === undefined ? 2 : 2 + (14 * Math.abs(v)) / max;
        return (
          <span
            key={f}
            title={`${f}: ${v === undefined ? "nieznane" : formatBits(v)}`}
            className="inline-block w-1.5 rounded-t-[1px]"
            style={{ height: h, backgroundColor: v === undefined ? "var(--color-line)" : v < 0 ? "var(--color-against)" : FAMILY_VAR[f] }}
          />
        );
      })}
    </span>
  );
}

const STEP_COLOR: Record<string, string> = {
  cap: "var(--color-muted)",
  for: "var(--color-for)",
  against: "var(--color-against)",
  unknown: "var(--color-unknown)",
  total: "var(--color-fg)",
};

/**
 * Bits waterfall (HTML/CSS, so text stays crisp): one row per family, residual, total.
 * Rows with reasons are expandable to the gene table.
 */
export function Waterfall({
  familyBits,
  totalBits,
  missing,
  reasons,
  contradictions,
  families = ["VIS", "TXT", "INF", "ID", "NAR", "META"],
  reveal,
}: {
  familyBits: Record<string, number>;
  totalBits: number;
  missing: readonly string[];
  reasons: GeneMatch[];
  contradictions: GeneMatch[];
  families?: readonly string[];
  /** Number of rows shown (simulation builds the waterfall bar by bar); default all. */
  reveal?: number;
}) {
  const cap = reasons.find((r) => r.kind === "cap_correction");
  const steps = waterfallSteps(familyBits, families, totalBits, missing).map((st) =>
    st.key === "residual" && cap ? { ...st, label: "korekta limitu rodziny", kind: "cap" as WaterfallStep["kind"] } : st,
  );
  reasons = reasons.filter((r) => r.kind !== "cap_correction");
  const lo = Math.min(0, ...steps.map((s) => Math.min(s.start, s.end)));
  const hi = Math.max(1, ...steps.map((s) => Math.max(s.start, s.end)));
  const span = hi - lo || 1;
  const x = (v: number) => ((v - lo) / span) * 100;
  const [open, setOpen] = useState<string | null>(null);
  const byFamily = (fam: string) => reasons.filter((r) => r.family === fam);

  return (
    <div className="text-sm" data-testid="waterfall">
      <div className="grid grid-cols-[160px_minmax(0,1fr)_72px] items-center gap-x-3">
        {steps.map((step, stepIndex) => {
          const hidden = reveal !== undefined && stepIndex >= reveal;
          const rows = step.family ? byFamily(step.family) : step.key === "residual" ? contradictions : [];
          const expandable = rows.length > 0;
          const left = Math.min(x(step.start), x(step.end));
          const width = Math.max(0.6, Math.abs(x(step.end) - x(step.start)));
          const isOpen = open === step.key;
          return (
            <div key={step.key} data-testid="waterfall-step" data-family={step.family} data-step={step.key} className={cx("contents", hidden && "[&>*]:invisible")}>
              <button
                type="button"
                disabled={!expandable}
                onClick={() => setOpen(isOpen ? null : step.key)}
                aria-expanded={expandable ? isOpen : undefined}
                title={step.kind === ("cap" as WaterfallStep["kind"]) ? "Korekta limitu rodziny: bity rodzin powyżej limitu nie są liczone do sumy (scorer CORE)" : undefined}
                className={cx(
                  "flex h-7 items-center gap-1.5 truncate text-left",
                  step.kind === "total" ? "font-semibold text-fg" : "text-muted",
                  expandable && "hover:text-fg",
                )}
              >
                <span aria-hidden="true" className="w-3 text-2xs text-subtle">{expandable ? (isOpen ? "▾" : "▸") : ""}</span>
                {step.label}
              </button>
              <div className="relative h-7">
                <div className="absolute inset-y-0 w-px bg-line-strong" style={{ left: `${x(0)}%` }} />
                <div
                  className={cx("absolute top-1.5 h-4 rounded-[3px]", step.kind === "unknown" && "border border-dashed border-unknown bg-transparent")}
                  style={{ left: `${left}%`, width: `${width}%`, backgroundColor: step.kind === "unknown" ? undefined : STEP_COLOR[step.kind] }}
                />
              </div>
              <div className={cx("text-right font-mono text-xs tabular-nums", step.kind === "total" ? "font-semibold text-fg" : "text-muted")}>
                {step.kind === "unknown" ? <span className="text-subtle italic" title="Brak obserwacji: nieznane, nie liczy się przeciw">nieznane</span> : formatBits(step.bits)}
              </div>
              {isOpen && (
                <div className="col-span-3 mb-2">
                  <GeneMatchTable rows={rows} />
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="mt-2 flex flex-wrap gap-3 text-2xs text-subtle">
        <Legend color="var(--color-for)" label="za powiązaniem" />
        <Legend color="var(--color-against)" label="przeciw (sprzeczność)" />
        <Legend color="var(--color-unknown)" label="nieznane (brak obserwacji, 0 b)" dashed />
        {cap && <Legend color="var(--color-muted)" label="korekta limitu rodziny: bity ponad limit rodziny nie wchodzą do sumy" />}
      </div>
    </div>
  );
}

function Legend({ color, label, dashed }: { color: string; label: string; dashed?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span aria-hidden="true" className={cx("inline-block h-2 w-3 rounded-[2px]", dashed && "border border-dashed")} style={dashed ? { borderColor: color } : { backgroundColor: color }} />
      {label}
    </span>
  );
}

export function GeneMatchTable({ rows, empty = "Brak genów", limit = 25, idPrefix }: { rows: GeneMatch[]; empty?: ReactNode; limit?: number; idPrefix?: string }) {
  const [all, setAll] = useState(false);
  if (rows.length === 0) return <div className="px-2 py-3 text-xs text-subtle">{empty}</div>;
  const hasB = rows.some((r) => r.value_b !== null && r.value_b !== undefined);
  const shown = all ? rows : rows.slice(0, limit);
  return (
    <div className="overflow-x-auto rounded-md border border-line">
      <table className="w-full text-xs">
        <thead className="bg-raised text-2xs tracking-wider text-subtle uppercase">
          <tr>
            <th className="px-2 py-1.5 text-left">Poch.</th>
            <th className="px-2 py-1.5 text-left">Gen</th>
            <th className="px-2 py-1.5 text-left">{hasB ? "Wartość A" : "Wartość"}</th>
            {hasB && <th className="px-2 py-1.5 text-left">Wartość B</th>}
            <th className="px-2 py-1.5 text-right" title="liczba incydentów z tą wartością w korpusie">DF</th>
            <th className="px-2 py-1.5 text-right">Bity</th>
            <th className="px-2 py-1.5 text-left">Dlaczego</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={`${r.kind}-${i}`} id={idPrefix ? `${idPrefix}-${i}` : undefined} className="scroll-mt-20 border-t border-line">
              <td className="px-2 py-1.5"><Prov value={r.provenance} /></td>
              <td className="px-2 py-1.5 whitespace-nowrap"><FamChip family={r.family} /> <span className="text-muted" title={r.kind === "cap_correction" ? "bity ponad limit rodziny nie wchodzą do sumy" : reasonKindLabel(r.kind) !== r.kind ? `kod: ${r.kind}` : undefined}>{reasonKindLabel(r.kind)}</span></td>
              <td className="max-w-[240px] truncate px-2 py-1.5">{r.value_a ? <button type="button" className="max-w-full truncate text-left hover:underline" title="dlaczego ten gen się liczy" onClick={() => openGene(r.kind, r.value_a!)}><GeneValue value={r.value_a} max={60} /></button> : <GeneValue value={r.value_a} max={60} />}</td>
              {hasB && <td className="max-w-[240px] truncate px-2 py-1.5"><GeneValue value={r.value_b} max={60} /></td>}
              <td className="px-2 py-1.5 text-right font-mono">{r.doc_freq ?? "—"}</td>
              <td className={cx("px-2 py-1.5 text-right font-mono font-semibold whitespace-nowrap", r.bits < 0 ? "text-err" : "text-fg")}>{formatBits(r.bits)}</td>
              <td className="min-w-[220px] px-2 py-1.5 text-muted">{r.explanation_pl}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > shown.length && (
        <button type="button" onClick={() => setAll(true)} className="w-full border-t border-line py-1.5 text-xs text-accent hover:bg-hover">pokaż wszystkie ({rows.length})</button>
      )}
    </div>
  );
}

/** Artifact thumbnail from the safe content endpoint; images only. */
export function Thumb({ artifactId, alt, size = 64, to, className }: { artifactId: string | null | undefined; alt: string; size?: number; to?: string; className?: string }) {
  const box = { width: size, height: size };
  const [, setBump] = useState(0);
  const broken = artifactId ? thumbFailed(artifactId) : false;
  const rerender = () => setBump((n) => n + 1);
  // CR-ASTRA-RT-01: an <img> error does not tell a corrupt file from an expired session or a network blip, so the
  // label stays neutral and the failure is only remembered briefly (lib/thumbFailures); retry is always possible.
  const failText = "nie udało się wczytać podglądu";
  const failTitle = `${failText} (przyczyna nieznana: sesja, sieć lub plik); ponowna próba przy następnej wizycie`;
  const img = artifactId && broken ? (
    to ? (
      <span className="flex h-full w-full items-center justify-center text-center text-2xs leading-3 text-subtle" title={failTitle} data-testid="thumb-broken">{size >= 64 ? failText : "—"}</span>
    ) : (
      <button type="button" className="flex h-full w-full items-center justify-center text-center text-2xs leading-3 text-subtle hover:text-fg" title={`${failTitle}; kliknij, aby spróbować ponownie`} data-testid="thumb-broken" onClick={() => { clearThumbFailure(artifactId); rerender(); }}>{size >= 64 ? failText : "—"}</button>
    )
  ) : artifactId ? (
    <img src={thumbUrl(artifactId, size > 160 ? 320 : 160)} alt={alt} loading="lazy" onError={() => { markThumbFailed(artifactId); rerender(); }} className="h-full w-full object-cover" />
  ) : (
    <span className="flex h-full w-full items-center justify-center text-center text-2xs leading-3 text-subtle" title="brak obrazu">{size >= 64 ? "brak obrazu" : "—"}</span>
  );
  const cls = cx("block shrink-0 overflow-hidden rounded-[4px] border border-line bg-raised", className);
  return to ? (
    <Link to={to} className={cls} style={box}>{img}</Link>
  ) : (
    <span className={cls} style={box}>{img}</span>
  );
}

/** Polish gene kind name; the raw code stays in the tooltip for experts (B9). */
export function KindLabel({ kind, className }: { kind: string; className?: string }) {
  const pl = reasonKindLabel(kind);
  return <span className={className} title={pl !== kind ? `kod: ${kind}` : undefined} data-kind={kind}>{pl}</span>;
}

export function SectionNote({ children }: { children: ReactNode }) {
  return <p className="text-xs leading-5 text-subtle">{children}</p>;
}

export const REDACTED = "[REDACTED]";

/** Gene value: mono, truncated with a tooltip; redacted secrets shown as such (contracts REDACTED). */
export function GeneValue({ value, max = 120, className }: { value: string | null | undefined; max?: number; className?: string }) {
  if (value === null || value === undefined || value === "") return <span className="text-subtle">—</span>;
  if (value.includes(REDACTED)) {
    return (
      <span className={cx("font-mono", className)} title="Wartość zawierała sekret (token, klucz API, nazwa użytkownika) i została ukryta przy ekstrakcji">
        {value.replaceAll(REDACTED, "")}
        <span className="ml-0.5 rounded-[3px] border border-line-strong bg-raised px-1 font-sans text-2xs text-subtle italic">ukryto dane wrażliwe</span>
      </span>
    );
  }
  const short = value.length > max ? `${value.slice(0, max)}…` : value;
  return <span className={cx("font-mono", className)} title={value}>{short}</span>;
}
