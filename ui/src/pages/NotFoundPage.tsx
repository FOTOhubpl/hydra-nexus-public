import { Link, useLocation } from "react-router";
import { EmptyState } from "../components/EmptyState";

export function NotFoundPage() {
  const { pathname } = useLocation();
  return (
    <EmptyState
      testId="not-found"
      title="Nie ma takiej strony"
      detail={pathname}
      action={
        <Link to="/" className="text-sm text-accent hover:underline">
          Wróć do pulpitu
        </Link>
      }
    />
  );
}
