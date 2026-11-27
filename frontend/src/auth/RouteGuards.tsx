import { Navigate, Outlet, useLocation } from "react-router";

import { ErrorState } from "../components/ui/ErrorState";
import { FullPageSpinner } from "../components/ui/Spinner";
import { safeRedirect } from "../lib/redirect";
import { useAuth } from "./AuthProvider";

export interface LocationState {
  from?: string;
}

/** Only for signed-in users; others are sent to /login and brought back afterwards. */
export function ProtectedRoute() {
  const { status, restoreError, retryRestore } = useAuth();
  const location = useLocation();

  if (status === "loading") return <FullPageSpinner label="Restoring your session…" />;
  if (status === "error") {
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <ErrorState
          title="Cannot reach the server"
          message={restoreError?.message ?? "Your session could not be restored."}
          onRetry={retryRestore}
        />
      </div>
    );
  }
  if (status === "anonymous") {
    const from = `${location.pathname}${location.search}`;
    return <Navigate to="/login" replace state={{ from } satisfies LocationState} />;
  }
  return <Outlet />;
}

/**
 * Login and registration: signed-in users are sent on, to the page they originally asked
 * for (when sent here by ProtectedRoute) or to the dashboard.
 */
export function PublicOnlyRoute() {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") return <FullPageSpinner label="Loading…" />;
  if (status === "authenticated") {
    const from = safeRedirect((location.state as LocationState | null)?.from);
    return <Navigate to={from} replace />;
  }
  return <Outlet />;
}
