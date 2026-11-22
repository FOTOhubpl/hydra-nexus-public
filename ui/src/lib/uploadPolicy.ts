/**
 * Client-side mirror of the name part of ADAPT's data-vs-junk rule (hydra.adapters.nask_folder.is_data_file,
 * audit A03): only OS / VCS / archiver junk is dropped before upload. Hidden data files (`strona_1/.har`) are
 * sent; the server makes the final call (it also looks at the bytes) and reports what it skipped.
 */
const JUNK_NAMES = new Set([".ds_store", "thumbs.db", "desktop.ini", ".localized"]);
const JUNK_DIRS = new Set(["__MACOSX", ".git", ".svn", ".hg"]);

export function isOsJunk(path: string): boolean {
  const parts = path.replace(/\\/g, "/").split("/").filter((p) => p && p !== ".");
  const name = parts[parts.length - 1] ?? "";
  if (!name) return true;
  if (JUNK_NAMES.has(name.toLowerCase()) || name.startsWith("._")) return true;
  return parts.slice(0, -1).some((p) => JUNK_DIRS.has(p));
}

export function splitUploads<T extends { path: string }>(entries: T[]): { keep: T[]; junk: T[] } {
  const keep: T[] = [];
  const junk: T[] = [];
  for (const e of entries) (isOsJunk(e.path) ? junk : keep).push(e);
  return { keep, junk };
}
