import { useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { useQueries, type Query } from "@tanstack/react-query";
import { getJob, isApiError, jobRefetchInterval, queryKeys, useArtifacts, useHealth } from "../api/client";
import type { Artifact, Job } from "../api/types";
import { ApiErrorState } from "../components/ApiErrorState";
import { Badge } from "../components/Badge";
import { Button } from "../components/Button";
import { DataTable, type Column } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { FileDropzone } from "../components/FileDropzone";
import { JobProgress } from "../components/JobProgress";
import { Panel } from "../components/Panel";
import { ReferenceGalleryPanel, useReferenceStatus } from "../components/ReferenceGalleryPanel";
import { StatTile } from "../components/StatTile";
import { formatBytes, formatCount } from "../lib/format";
import { addSessionJob, clearSessionJobs, removeSessionJob, useSessionJobs } from "../lib/sessionJobs";
import { useSettings } from "../lib/settings";
import { statusVariant } from "../lib/status";

const STATUS_LABELS: Record<string, string> = {
  queued: "oczekuje", running: "w toku", done: "zakończone", failed: "błąd",
  imported: "zaimportowany", skipped: "pominięty", duplicate: "duplikat", quarantined: "kwarantanna",
  ok: "działa", healthy: "działa", ready: "gotowy", online: "dostępny", offline: "niedostępny",
  error: "błąd", unknown: "nieznany",
};

function JobsPanel() {
  const jobs = useSessionJobs();
  const [trackId, setTrackId] = useState("");

  const onTrack = (event: FormEvent) => {
    event.preventDefault();
    if (!trackId.trim()) return;
    addSessionJob(trackId, "dodane ręcznie");
    setTrackId("");
  };

  return (
    <Panel
      title="Zadania"
      flush
      className="min-h-[240px]"
      actions={
        jobs.length > 0 && (
          <Button size="sm" variant="ghost" onClick={clearSessionJobs}>
            Wyczyść listę
          </Button>
        )
      }
    >
      <form onSubmit={onTrack} className="flex gap-2 border-b border-line px-3 py-2">
        <input
          value={trackId}
          onChange={(event) => setTrackId(event.target.value)}
          placeholder="Wpisz identyfikator zadania…"
          aria-label="Identyfikator zadania do obserwowania"
          className="h-7 min-w-0 flex-1 rounded-[4px] border border-line bg-canvas px-2 font-mono text-xs text-fg placeholder:font-sans placeholder:text-subtle focus:border-accent focus:outline-none"
        />
        <Button size="sm" type="submit" disabled={!trackId.trim()}>
          Obserwuj
        </Button>
      </form>
      {jobs.length === 0 ? (
        <EmptyState
          compact
          title="Brak zadań"
          description="Tutaj pojawią się zadania uruchomione podczas importu. Ich stan jest odświeżany do zakończenia."
        />
      ) : (
        <ul className="divide-y divide-line">
          {jobs.map((job) => (
            <li key={job.id}>
              <JobProgress jobId={job.id} label={job.label} onRemove={() => removeSessionJob(job.id)} />
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function ArtifactsPanel() {
  const { pageSize } = useSettings();
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const navigate = useNavigate();
  const query = useArtifacts({ limit: pageSize, offset: page * pageSize });
  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const selectedSet = useMemo(() => new Set(selected), [selected]);

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id].slice(-2)));

  const columns = useMemo<Column<Artifact>[]>(
    () => [
      {
        key: "select",
        header: <span className="sr-only">Wybierz</span>,
        sortable: false,
        width: "36px",
        render: (row) => (
          <input
            type="checkbox"
            className="accent-accent"
            aria-label={`Wybierz ${row.id}`}
            checked={selectedSet.has(row.id)}
            onClick={(event) => event.stopPropagation()}
            onChange={() => toggle(row.id)}
          />
        ),
      },
      { key: "id", header: "ID", mono: true, width: "24%" },
      { key: "mime", header: "MIME", width: "16%" },
      { key: "size", header: "Rozmiar", align: "right", width: "10%", render: (row) => formatBytes(row.size) },
      { key: "source_ref", header: "Źródło", mono: true },
      {
        key: "status",
        header: "Status",
        width: "120px",
        render: (row) => (
          <Badge variant={statusVariant(row.status)} dot>
            {STATUS_LABELS[row.status || "unknown"] ?? row.status ?? "nieznany"}
          </Badge>
        ),
      },
    ],
    [selectedSet],
  );

  let body;
  if (query.isError && !query.data) {
    body = <ApiErrorState error={query.error} resource="artefakty" endpoint="GET /v1/artifacts?limit&offset" compact />;
  } else {
    body = (
      <DataTable
        columns={columns}
        rows={items}
        rowKey={(row) => row.id}
        loading={query.isPending}
        selectedKeys={selectedSet}
        onRowClick={(row) => navigate(`/item/${encodeURIComponent(row.id)}`)}
        caption="Artefakty"
        className="h-full"
        empty={
          <EmptyState compact title="Brak artefaktów" description="Zaimportuj pliki, aby wypełnić listę. Dane pojawią się po potwierdzeniu przez serwer." />
        }
      />
    );
  }

  return (
    <Panel
      title={
        <>
          Artefakty{" "}
          {query.data && <span className="ml-1 font-mono text-subtle normal-case">{formatCount(total)}</span>}
        </>
      }
      flush
      className="min-h-[320px]"
      bodyClassName="flex flex-col"
      actions={
        <>
          {selected.length > 0 && (
            <Button size="sm" variant="ghost" onClick={() => setSelected([])}>
              Wyczyść zaznaczenie
            </Button>
          )}
          <Button
            size="sm"
            variant={selected.length === 2 ? "primary" : "secondary"}
            disabled={selected.length !== 2}
            title="Wybierz dwa wiersze, aby porównać je obok siebie"
            onClick={() => navigate(`/detail/${encodeURIComponent(selected[0]!)}/${encodeURIComponent(selected[1]!)}`)}
          >
            Porównaj {selected.length}/2
          </Button>
        </>
      }
    >
      <div className="min-h-0 flex-1">{body}</div>
      {query.data && total > pageSize && (
        <div className="flex h-9 shrink-0 items-center justify-between border-t border-line px-3 text-xs text-muted">
          <span>
            {formatCount(page * pageSize + 1)}–{formatCount(Math.min(total, (page + 1) * pageSize))} z {formatCount(total)}
            {query.isFetching && <span className="ml-2 text-subtle">odświeżanie…</span>}
          </span>
          <div className="flex gap-1">
            <Button size="sm" variant="ghost" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>
              ‹ Poprzednia
            </Button>
            <span className="px-2 leading-6 font-mono">
              {page + 1}/{pages}
            </span>
            <Button size="sm" variant="ghost" disabled={page + 1 >= pages} onClick={() => setPage((p) => p + 1)}>
              Następna ›
            </Button>
          </div>
        </div>
      )}
    </Panel>
  );
}

function SummaryTiles() {
  const { healthIntervalMs, jobPollMs } = useSettings();
  const health = useHealth(healthIntervalMs);
  const jobs = useSessionJobs();
  const artifacts = useArtifacts({ limit: 1, offset: 0 });
  // Same query keys as JobProgress, so these share its cache and polling.
  const jobQueries = useQueries({
    queries: jobs.map((job) => ({
      queryKey: queryKeys.job(job.id),
      queryFn: ({ signal }: { signal: AbortSignal }) => getJob(job.id, signal),
      refetchInterval: (query: Query<Job, Error>) => jobRefetchInterval(query, jobPollMs),
      retry: false,
    })),
  });
  const statuses = jobQueries.map((q) => q.data?.status);
  const active = statuses.filter((s) => s === "queued" || s === "running").length;
  const failed = statuses.filter((s) => s === "failed").length;
  const done = statuses.filter((s) => s === "done").length;

  const backendOffline = health.isError && (!isApiError(health.error) || health.error.isOffline);

  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      <StatTile
        label="Serwer"
        loading={health.isPending}
        value={backendOffline ? "niedostępny" : health.isError ? "błąd" : STATUS_LABELS[String(health.data?.status)] ?? (health.data?.status as string | undefined) ?? "dostępny"}
        hint={health.data?.version ? `wersja ${String(health.data.version)}` : backendOffline ? "Brak połączenia z 127.0.0.1:8000" : undefined}
        aside={!health.isPending && <Badge variant={backendOffline ? "danger" : health.isError ? "warning" : "success"} dot>{backendOffline ? "niedostępny" : "dostępny"}</Badge>}
      />
      <StatTile
        label="Artefakty"
        loading={artifacts.isPending}
        value={artifacts.data ? formatCount(artifacts.data.total) : null}
        hint={artifacts.isError ? (isApiError(artifacts.error) && artifacts.error.isNotImplemented ? "obsługa niezaimplementowana" : "niedostępne") : "według danych serwera"}
      />
      <StatTile label="Aktywne zadania" value={jobs.length ? formatCount(active) : null} hint={`Obserwowane w tej przeglądarce: ${formatCount(jobs.length)}`} />
      <StatTile
        label="Zakończone zadania"
        value={jobs.length ? formatCount(done + failed) : null}
        hint={jobs.length ? `Ukończone: ${formatCount(done)} · błędy: ${formatCount(failed)}` : "jeszcze brak"}
        aside={failed > 0 && <Badge variant="danger">Błędy: {formatCount(failed)}</Badge>}
      />
    </div>
  );
}

/** GALLERY: Pulpit notice when the local face gallery / logo templates are not built yet. */
function ReferenceNotice() {
  const st = useReferenceStatus(false);
  if (!st.data || (st.data.face_entries > 0 && st.data.logo_templates > 0)) return null;
  return (
    <Panel title="Galeria referencyjna">
      <p className="mb-2 text-xs text-abstain-ink" data-testid="reference-notice">
        Galeria twarzy i wzorce logo nie są dołączone do HYDRA (dane biometryczne, znaki towarowe). Można je zbudować lokalnie z
        zaimportowanych materiałów; potem przelicz analizę, aby pojawiły się geny ID/logo.
      </p>
      <ReferenceGalleryPanel compact />
    </Panel>
  );
}

export function OverviewPage() {
  return (
    <div className="flex flex-col gap-3 p-4">
      <SummaryTiles />
      <div className="grid gap-3 xl:grid-cols-[minmax(320px,420px)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-3">
          <Panel title="Import">
            <p data-testid="import-note" className="mb-2 text-xs text-muted">
              Ten panel zapisuje pliki w magazynie treści (status każdego pliku poniżej) bez grupowania w kampanie.
              Aby przeanalizować zbiór incydentów, użyj{" "}
              <Link to="/analyse?tab=files" className="text-accent hover:underline">Nowa analiza: plik, folder lub ZIP</Link>.
            </p>
            <FileDropzone />
          </Panel>
          <JobsPanel />
          <ReferenceNotice />
        </div>
        <ArtifactsPanel />
      </div>
    </div>
  );
}
