import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useArtifact } from "../api/client";
import { Badge } from "../components/Badge";
import { Button } from "../components/Button";
import { EmptyState } from "../components/EmptyState";
import { Panel } from "../components/Panel";
import { SplitPane } from "../components/SplitPane";
import { truncateMiddle } from "../lib/format";
import { statusVariant } from "../lib/status";
import { ArtifactLoader, ArtifactPreview, ArtifactProperties } from "./ArtifactView";

function PaneHeader({ id, side }: { id: string; side: "A" | "B" }) {
  const query = useArtifact(id);
  return (
    <>
      <span className="font-mono text-2xs font-semibold text-subtle">{side}</span>
      <Link to={`/item/${encodeURIComponent(id)}`} className="min-w-0 truncate font-mono text-xs text-fg hover:text-accent" title={id}>
        {truncateMiddle(id, 40)}
      </Link>
      {query.data && (
        <span className="ml-auto flex shrink-0 items-center gap-2">
          <span className="hidden truncate text-xs text-muted lg:inline">{query.data.mime}</span>
          <Badge variant={statusVariant(query.data.status)} dot>
            {query.data.status || "nieznany"}
          </Badge>
        </span>
      )}
    </>
  );
}

function PaneBody({ id, otherId, showPreview }: { id: string; otherId: string; showPreview: boolean }) {
  const other = useArtifact(otherId);
  return (
    <ArtifactLoader id={id}>
      {(artifact) => (
        <div className="flex flex-col">
          <ArtifactProperties artifact={artifact} compareTo={other.data ?? null} />
          {showPreview && (
            <div className="flex h-[420px] flex-col border-t border-line">
              <ArtifactPreview artifact={artifact} className="flex-1" />
            </div>
          )}
        </div>
      )}
    </ArtifactLoader>
  );
}

export function ComparePage() {
  const { a = "", b = "" } = useParams();
  const navigate = useNavigate();
  const [sync, setSync] = useState(true);
  const [showPreview, setShowPreview] = useState(true);

  if (!a || !b) return <ComparePicker />;

  return (
    <div className="flex h-full min-h-[480px] flex-col p-4">
      <SplitPane
        className="flex-1"
        syncScroll={sync}
        leftLabel={`Element ${a}`}
        rightLabel={`Element ${b}`}
        toolbar={
          <>
            <span className="text-xs text-muted">Różniące się właściwości są oznaczone po obu stronach.</span>
            <div className="ml-auto flex items-center gap-3 text-xs text-muted">
              <label className="flex cursor-pointer items-center gap-1.5 select-none">
                <input type="checkbox" className="accent-accent" checked={sync} onChange={(e) => setSync(e.target.checked)} />
                Synchronizuj przewijanie
              </label>
              <label className="flex cursor-pointer items-center gap-1.5 select-none">
                <input type="checkbox" className="accent-accent" checked={showPreview} onChange={(e) => setShowPreview(e.target.checked)} />
                Podgląd
              </label>
              <Button size="sm" onClick={() => navigate(`/detail/${encodeURIComponent(b)}/${encodeURIComponent(a)}`)}>
                Zamień ⇄
              </Button>
            </div>
          </>
        }
        leftHeader={<PaneHeader id={a} side="A" />}
        rightHeader={<PaneHeader id={b} side="B" />}
        left={<PaneBody id={a} otherId={b} showPreview={showPreview} />}
        right={<PaneBody id={b} otherId={a} showPreview={showPreview} />}
      />
    </div>
  );
}

function ComparePicker() {
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const navigate = useNavigate();
  const ready = a.trim() && b.trim();
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (ready) navigate(`/detail/${encodeURIComponent(a.trim())}/${encodeURIComponent(b.trim())}`);
  };
  const input =
    "h-7 w-full rounded-[4px] border border-line bg-canvas px-2 font-mono text-xs text-fg placeholder:font-sans placeholder:text-subtle focus:border-accent focus:outline-none";
  return (
    <div className="mx-auto flex max-w-xl flex-col gap-3 p-4">
      <Panel title="Porównaj dwa elementy">
        <form onSubmit={onSubmit} className="flex flex-col gap-3">
          <label className="flex flex-col gap-1 text-xs text-muted">
            Element A
            <input className={input} value={a} onChange={(e) => setA(e.target.value)} placeholder="identyfikator" />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted">
            Element B
            <input className={input} value={b} onChange={(e) => setB(e.target.value)} placeholder="identyfikator" />
          </label>
          <div className="flex justify-end">
            <Button variant="primary" type="submit" disabled={!ready}>
              Otwórz obok siebie
            </Button>
          </div>
        </form>
      </Panel>
      <EmptyState
        compact
        title="Wskazówka"
        description="W widoku Import danych zaznacz dwa wiersze w tabeli artefaktów i naciśnij Porównaj."
      />
    </div>
  );
}
