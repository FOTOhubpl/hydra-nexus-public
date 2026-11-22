/** External-dataset badge: any incident source outside the native inputs is shown as `ext:<source>`. */
const NATIVE = new Set(["nask-sample", "nask", "upload", "url", "screenshot", "file", "path", "api", "hint", "synthetic"]);

export function extSource(source: string | null | undefined): string | null {
  if (!source || NATIVE.has(source)) return null;
  return `ext:${source}`;
}
