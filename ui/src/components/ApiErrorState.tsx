import type { ReactNode } from "react";
import { isApiError } from "../api/client";
import { EmptyState } from "./EmptyState";

export interface ApiErrorStateProps {
  error: unknown;
  /** What was being loaded, in Polish, e.g. "artefakty". */
  resource: string;
  /** Endpoint shown when the backend has not implemented it yet. */
  endpoint?: string;
  action?: ReactNode;
  compact?: boolean;
}

/** Polish reading of the server's resource-404 details (B3, audit f450a70). Unknown details pass through. */
const NOT_FOUND_PL: [RegExp, string][] = [
  [/^campaign not (found )?in this run$/i, "Ta kampania nie istnieje w bieżącym przebiegu (identyfikatory zmieniają się po ponownej analizie)."],
  [/^incident not in this run$/i, "Ten incydent nie należy do bieżącego przebiegu."],
  [/^incident not found$/i, "Nie ma takiego incydentu w bazie."],
  [/^pair not scored/i, "Ta para nie była oceniana w bieżącym przebiegu (to nie jest para kandydująca)."],
  [/^no analysis run yet$/i, "Nie uruchomiono jeszcze żadnej analizy."],
  [/^run not found$/i, "Nie ma takiego przebiegu analizy."],
  [/^artifact not found$/i, "Nie ma takiego artefaktu."],
  [/^(analyse )?job not found$/i, "Nie ma takiego zadania (mogło zostać usunięte po restarcie serwera)."],
];

export function notFoundMessagePl(detail: string): string {
  const d = detail.trim();
  return NOT_FOUND_PL.find(([re]) => re.test(d))?.[1] ?? d;
}

/** Maps an API failure onto the right empty state: offline, missing resource, not implemented, or error. */
export function ApiErrorState({ error, resource, endpoint, action, compact }: ApiErrorStateProps) {
  if (isApiError(error) && error.isOffline) {
    return (
      <EmptyState
        compact={compact}
        tone="offline"
        title="Serwer lokalny niedostępny"
        description={`Nie można wczytać: ${resource}. Lokalne API nie odpowiada; uruchom je na 127.0.0.1:8000. Widok ponawia próbę automatycznie.`}
        action={action}
      />
    );
  }
  if (isApiError(error) && error.isResourceNotFound) {
    return (
      <EmptyState
        compact={compact}
        tone="neutral"
        title="Nie znaleziono"
        description={notFoundMessagePl(error.message)}
        detail={`HTTP 404 · ${error.path}`}
        action={action}
        testId="resource-not-found"
      />
    );
  }
  if (isApiError(error) && error.isNotImplemented) {
    return (
      <EmptyState
        compact={compact}
        tone="pending"
        title="Punkt końcowy API jeszcze niedostępny"
        description={`Serwer nie udostępnia jeszcze: ${resource}.`}
        detail={endpoint ?? error.path}
        action={action}
      />
    );
  }
  const message = error instanceof Error ? error.message : String(error);
  return (
    <EmptyState
      compact={compact}
      tone="error"
      title={`Nie udało się wczytać: ${resource}`}
      description={message}
      detail={isApiError(error) && error.status !== null ? `HTTP ${error.status} · ${error.path}` : undefined}
      action={action}
    />
  );
}
