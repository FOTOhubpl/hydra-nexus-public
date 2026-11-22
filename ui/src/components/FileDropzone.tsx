import { useEffect, useRef, useState, type DragEvent } from "react";
import { isApiError, useImport, type UploadEntry } from "../api/client";
import { formatBytes, formatCount } from "../lib/format";
import { addSessionJob } from "../lib/sessionJobs";
import { cx } from "../lib/cx";
import { Button } from "./Button";

export interface FileDropzoneProps {
  /** Called after the backend accepted the upload (202 + job_id). */
  onImported?: (jobId: string, fileCount: number) => void;
  /** Skip dotfiles such as .DS_Store (default true). */
  ignoreHidden?: boolean;
  /** Upload immediately on drop instead of staging for review. */
  autoSubmit?: boolean;
  disabled?: boolean;
  className?: string;
}

function isHidden(path: string): boolean {
  return path.split("/").some((part) => part.startsWith("."));
}

function readFile(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

function readBatch(reader: FileSystemDirectoryReader): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function walkEntry(entry: FileSystemEntry, out: UploadEntry[]): Promise<void> {
  if (entry.isFile) {
    const file = await readFile(entry as FileSystemFileEntry);
    out.push({ file, path: entry.fullPath.replace(/^\/+/, "") || file.name });
    return;
  }
  if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    // readEntries returns results in batches; keep reading until an empty batch.
    for (;;) {
      const batch = await readBatch(reader);
      if (batch.length === 0) break;
      for (const child of batch) await walkEntry(child, out);
    }
  }
}

/** Resolve a drop into files with relative paths, descending into dropped folders. */
export async function collectDropped(dataTransfer: DataTransfer): Promise<UploadEntry[]> {
  // webkitGetAsEntry must be called synchronously, before the first await.
  const roots = Array.from(dataTransfer.items ?? [])
    .filter((item) => item.kind === "file")
    .map((item) => (typeof item.webkitGetAsEntry === "function" ? item.webkitGetAsEntry() : null));
  const fallback = Array.from(dataTransfer.files ?? []);
  if (!roots.some(Boolean)) {
    return fallback.map((file) => ({ file, path: file.webkitRelativePath || file.name }));
  }
  const out: UploadEntry[] = [];
  for (const root of roots) if (root) await walkEntry(root, out);
  return out;
}

export function FileDropzone({ onImported, ignoreHidden = true, autoSubmit = false, disabled = false, className }: FileDropzoneProps) {
  const [staged, setStaged] = useState<UploadEntry[]>([]);
  const [dragDepth, setDragDepth] = useState(0);
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const importer = useImport();

  // React does not type the non-standard folder-picker attributes.
  useEffect(() => {
    folderInput.current?.setAttribute("webkitdirectory", "");
    folderInput.current?.setAttribute("directory", "");
  }, []);

  const busy = disabled || reading || importer.isPending;
  const totalBytes = staged.reduce((sum, entry) => sum + entry.file.size, 0);

  const submit = (entries: UploadEntry[]) => {
    if (entries.length === 0) return;
    importer.mutate(entries, {
      onSuccess: (accepted) => {
        addSessionJob(accepted.job_id, `Liczba plików: ${entries.length}`);
        setStaged([]);
        onImported?.(accepted.job_id, entries.length);
      },
    });
  };

  const stage = (entries: UploadEntry[]) => {
    const kept = ignoreHidden ? entries.filter((entry) => !isHidden(entry.path)) : entries;
    importer.reset();
    setReadError(null);
    if (autoSubmit) {
      submit(kept);
      return;
    }
    // Merge with anything already staged; the same path replaces the earlier pick.
    setStaged((prev) => {
      const byPath = new Map(prev.map((entry) => [entry.path, entry]));
      for (const entry of kept) byPath.set(entry.path, entry);
      return Array.from(byPath.values());
    });
  };

  const onDrop = async (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragDepth(0);
    if (busy) return;
    setReading(true);
    try {
      stage(await collectDropped(event.dataTransfer));
    } catch (error) {
      setReadError(error instanceof Error ? error.message : "Nie udało się odczytać upuszczonych plików");
    } finally {
      setReading(false);
    }
  };

  const onPick = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    stage(Array.from(files, (file) => ({ file, path: file.webkitRelativePath || file.name })));
  };

  const error = readError ?? (importer.error ? describeImportError(importer.error) : null);

  return (
    <div className={cx("flex flex-col gap-2", className)}>
      <div
        role="button"
        tabIndex={busy ? -1 : 0}
        aria-disabled={busy || undefined}
        aria-label="Upuść pliki lub foldery do importu albo naciśnij Enter, aby wybrać pliki"
        onKeyDown={(event) => {
          if ((event.key === "Enter" || event.key === " ") && !busy) {
            event.preventDefault();
            fileInput.current?.click();
          }
        }}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragDepth((d) => d + 1);
        }}
        onDragOver={(event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = busy ? "none" : "copy";
        }}
        onDragLeave={() => setDragDepth((d) => Math.max(0, d - 1))}
        onDrop={onDrop}
        className={cx(
          "flex flex-col items-center justify-center gap-2 rounded-md border border-dashed px-4 py-6 text-center transition-colors",
          dragDepth > 0 ? "border-accent bg-accent/5" : "border-line-strong bg-raised",
          busy && "opacity-60",
        )}
      >
        <div className="text-sm font-medium text-fg">
          {reading ? "Odczytywanie plików…" : dragDepth > 0 ? "Upuść, aby dodać do listy" : "Upuść tutaj pliki lub folder"}
        </div>
        <div className="text-xs text-subtle">Foldery zachowują ścieżki względne. Wysyłanie rozpocznie się po naciśnięciu Importuj.</div>
        <div className="mt-1 flex gap-2">
          <Button size="sm" disabled={busy} onClick={() => fileInput.current?.click()}>
            Wybierz pliki
          </Button>
          <Button size="sm" disabled={busy} onClick={() => folderInput.current?.click()}>
            Wybierz folder
          </Button>
        </div>
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          data-testid="file-input"
          onChange={(event) => {
            onPick(event.target.files);
            event.target.value = "";
          }}
        />
        <input
          ref={folderInput}
          type="file"
          multiple
          hidden
          data-testid="folder-input"
          onChange={(event) => {
            onPick(event.target.files);
            event.target.value = "";
          }}
        />
      </div>

      {staged.length > 0 && (
        <div className="rounded-md border border-line bg-raised">
          <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-1.5">
            <span className="text-xs text-muted">
              <span className="font-semibold text-fg">{formatCount(staged.length)}</span> na liście · {formatBytes(totalBytes)}
            </span>
            <div className="flex gap-2">
              <Button size="sm" variant="ghost" disabled={importer.isPending} onClick={() => setStaged([])}>
                Wyczyść
              </Button>
              <Button size="sm" variant="primary" disabled={busy} onClick={() => submit(staged)}>
                {importer.isPending ? "Wysyłanie…" : `Importuj ${formatCount(staged.length)}`}
              </Button>
            </div>
          </div>
          <ul className="max-h-32 overflow-auto px-3 py-1.5 font-mono text-xs text-muted">
            {staged.slice(0, 200).map((entry) => (
              <li key={entry.path} className="flex justify-between gap-3">
                <span className="truncate">{entry.path}</span>
                <span className="shrink-0 text-subtle">{formatBytes(entry.file.size)}</span>
              </li>
            ))}
            {staged.length > 200 && <li className="text-subtle">… pozostałe: {formatCount(staged.length - 200)}</li>}
          </ul>
        </div>
      )}

      {error && (
        <div role="alert" className="rounded-md border border-err/40 bg-err/10 px-3 py-2 text-xs text-err">
          {error}
        </div>
      )}
    </div>
  );
}

function describeImportError(error: unknown): string {
  if (isApiError(error)) {
    if (error.isOffline) return "Import nie powiódł się: serwer jest niedostępny.";
    if (error.isNotImplemented) return "Obsługa importu (POST /v1/import) nie jest jeszcze zaimplementowana.";
    return `Import nie powiódł się: ${error.message}`;
  }
  return error instanceof Error ? `Import nie powiódł się: ${error.message}` : "Import nie powiódł się.";
}
