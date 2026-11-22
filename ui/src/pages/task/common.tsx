import type { ReactNode } from "react";
import { Link } from "react-router";
import { ApiErrorState } from "../../components/ApiErrorState";
import { EmptyState } from "../../components/EmptyState";
import { Skeleton } from "../../components/Skeleton";
import { cx } from "../../lib/cx";

export function Page({ title, sub, actions, children, className }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={cx("flex flex-col gap-3 p-4", className)}>
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-xl font-semibold text-fg">{title}</h2>
        {sub && <div className="text-sm text-muted">{sub}</div>}
        {actions && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {children}
    </div>
  );
}

export function NoRun({ what }: { what: string }) {
  return (
    <EmptyState
      title="Brak przebiegu analizy"
      description={
        <>
          {what} pojawią się po pierwszym przebiegu potoku na zbiorze incydentów (CLI <span className="font-mono">hydra analyse</span>).
          Interfejs pokazuje wyłącznie wyniki z backendu, bez danych przykładowych.
        </>
      }
      action={<Link className="text-sm text-accent hover:underline" to="/analyse?tab=files">Nowa analiza →</Link>}
    />
  );
}

/** Clean-state onboarding (JURY-READY): how to bring one's own dataset in, shown on the Pulpit before any run. */
export function StartImport() {
  const step = "rounded-lg border border-line bg-raised p-3";
  return (
    <section data-testid="start-import" aria-labelledby="start-import-title" className="rounded-xl border border-accent/40 bg-panel p-4 shadow-card">
      <h2 id="start-import-title" className="text-base font-semibold text-fg">Pusta baza: zaimportuj własny zbiór incydentów</h2>
      <p className="mt-1 text-sm text-muted">
        System działa offline i nie zawiera danych przykładowych. Kampanie, pary i graf pojawią się po pierwszej analizie Twojego materiału.
      </p>
      <div className="mt-3 grid gap-3 md:grid-cols-3">
        <div className={step}>
          <div className="text-sm font-semibold text-fg">1. Przeciągnij i upuść</div>
          <p className="mt-1 text-xs text-muted">
            Folder zbioru (jeden podfolder na incydent), jeden ZIP całego zbioru albo pliki jednego incydentu.
            Analiza rusza sama: import, geny, pary, kampanie.
          </p>
          <Link to="/analyse?tab=files" className="mt-2 inline-block text-sm font-medium text-accent hover:underline">Nowa analiza: plik, folder lub ZIP →</Link>
        </div>
        <div className={step}>
          <div className="text-sm font-semibold text-fg">2. Folder na serwerze</div>
          <p className="mt-1 text-xs text-muted">
            Duży zbiór skopiuj do katalogu danych (Docker: <span className="font-mono">~/HydraNexus/data</span> widoczny jako <span className="font-mono">/hydra/data</span>) i podaj ścieżkę.
          </p>
          <Link to="/analyse?tab=path" className="mt-2 inline-block text-sm font-medium text-accent hover:underline">Analiza folderu →</Link>
        </div>
        <div className={step}>
          <div className="text-sm font-semibold text-fg">3. Wiersz poleceń</div>
          <p className="mt-1 text-xs text-muted">
            <span className="font-mono">hydra analyse &lt;folder|zip&gt;</span> (Docker: <span className="font-mono">docker compose --profile cli run --rm cli analyse /hydra/data/&lt;zbiór&gt;</span>). Wyniki są od razu widoczne tutaj.
          </p>
        </div>
      </div>
      <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-muted">
        <li>Formaty: zrzuty i obrazy (PNG, JPG, WebP, HEIC), zapisane strony (HTML, MHTML, HAR), nagrania wideo, pliki tekstowe (UTF-8 lub cp1250), PDF, archiwa ZIP (także ZIP w ZIP-ie). Typ pliku jest rozpoznawany po zawartości, nie po rozszerzeniu.</li>
        <li>Każdy plik dostaje status. Pliki puste, ZIP-y z hasłem, bomby ZIP, ścieżki wychodzące poza archiwum i dowiązania symboliczne są pomijane z kodem przyczyny, a pozostałe incydenty są analizowane dalej. Lista pominiętych plików z kodami jest w wyniku analizy („Nowa analiza”) i w wydruku polecenia hydra analyse.</li>
        <li>Panel „Import danych” tylko zapisuje pliki w magazynie (ze statusem każdego pliku) bez grupowania w kampanie; do analizy użyj „Nowa analiza”.</li>
      </ul>
    </section>
  );
}

/** Skeleton lines instead of a blank area; after ~1.5 s a hint that a large run takes longer on first open. */
export function Loading({ what }: { what: string }) {
  return <Skeleton label={`Ładowanie: ${what}…`} />;
}

export function QueryState({ query, what, endpoint }: { query: { isPending: boolean; isError: boolean; error: unknown }; what: string; endpoint?: string }) {
  if (query.isPending) return <Loading what={what} />;
  if (query.isError) return <ApiErrorState error={query.error} resource={what} endpoint={endpoint} />;
  return null;
}

export function IncidentLink({ id, title }: { id: string; title?: string | null }) {
  return (
    <Link to={`/incident/${encodeURIComponent(id)}`} className="font-mono whitespace-nowrap text-accent hover:underline" title={title ?? undefined}>
      {id}
    </Link>
  );
}

export function CampaignLink({ id, label }: { id: string | null | undefined; label?: string | null }) {
  if (!id) return <span className="text-subtle">—</span>;
  return (
    <Link to={`/campaigns/${encodeURIComponent(id)}`} className="text-accent hover:underline">
      <span className="font-mono">{id}</span>
      {label && <span className="ml-1 text-fg">{label}</span>}
    </Link>
  );
}

export function pairPath(a: string, b: string): string {
  return `/pair/${encodeURIComponent(a)}/${encodeURIComponent(b)}`;
}

export function Kv({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 gap-2 text-sm">
      <span className="w-32 shrink-0 text-subtle">{k}</span>
      <span className="min-w-0 break-words text-fg">{children}</span>
    </div>
  );
}

/** DATAFLOW: material added since the analysis (docs/ui/STREAM_API.md, label "nowe od analizy"). */
export function NewBadge({ className }: { className?: string }) {
  return <span className={cx("inline-flex shrink-0 items-center rounded border border-accent px-1 text-2xs whitespace-nowrap text-accent", className)}>nowe od analizy</span>;
}

/** "+N nowe" next to a campaign size when members were assigned since the analysis; nothing otherwise. */
export function NewCount({ n, className }: { n: number | null | undefined; className?: string }) {
  if (!n || n <= 0) return null;
  return <span className={cx("shrink-0 text-2xs whitespace-nowrap text-accent", className)} title="nowe od analizy">+{n} nowe</span>;
}
