/** The local FastAPI schema, fetched same-origin (`/api/openapi.json`; hydra/api.py strips the `/api` prefix). */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiFetch, shouldRetry } from "../../../api/client";
import { parseOperations, type OpenApiDoc, type Operation } from "../../../lib/openapi";

export const OPENAPI_PATH = "/openapi.json";

export function useOpenApi() {
  const query = useQuery({
    queryKey: ["integrations", "openapi"],
    queryFn: ({ signal }) => apiFetch<OpenApiDoc>(OPENAPI_PATH, { signal }),
    staleTime: 5 * 60_000,
    retry: shouldRetry,
  });
  const ops: Operation[] = useMemo(() => (query.data?.paths ? parseOperations(query.data) : []), [query.data]);
  return { ...query, ops };
}

/** Base URL shown in snippets: the origin this UI is served from (the loopback API itself in production). */
export function snippetBase(): string {
  // Vite dev server: only /api/* is proxied to FastAPI. Production: the API serves both /v1 and /api/v1.
  const suffix = import.meta.env.DEV ? "/api" : "";
  return (typeof window !== "undefined" && window.location?.origin && window.location.origin !== "null" ? window.location.origin : "http://127.0.0.1:8000") + suffix;
}
