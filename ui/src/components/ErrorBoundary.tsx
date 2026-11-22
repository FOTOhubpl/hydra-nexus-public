import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "./Button";
import { EmptyState } from "./EmptyState";

export interface ErrorBoundaryProps {
  children: ReactNode;
  /** Custom fallback; receives the error and a reset callback. */
  fallback?: (error: Error, reset: () => void) => ReactNode;
  /** When any value changes, a caught error is cleared (e.g. the route path). */
  resetKeys?: readonly unknown[];
  /** Label for the default fallback, e.g. the region that failed. */
  label?: string;
  onError?: (error: Error, info: ErrorInfo) => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

function keysChanged(a: readonly unknown[] = [], b: readonly unknown[] = []): boolean {
  return a.length !== b.length || a.some((value, index) => !Object.is(value, b[index]));
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.props.onError?.(error, info);
    if (import.meta.env.DEV) console.error("[ErrorBoundary]", error, info.componentStack);
  }

  componentDidUpdate(prev: ErrorBoundaryProps): void {
    if (this.state.error && keysChanged(prev.resetKeys, this.props.resetKeys)) {
      this.reset();
    }
  }

  reset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error, this.reset);
    return (
      <EmptyState
        tone="error"
        title={this.props.label ? `${this.props.label}: błąd wyświetlania` : "Błąd wyświetlania"}
        description="Pozostała część aplikacji działa. Spróbuj ponownie albo odśwież stronę, jeśli błąd się powtarza."
        detail={error.message}
        action={
          <Button size="sm" onClick={this.reset}>
            Spróbuj ponownie
          </Button>
        }
      />
    );
  }
}
