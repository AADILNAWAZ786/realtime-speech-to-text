import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { ApiProvider, type AppApi } from "../api/ApiProvider";
import { AuthProvider } from "../auth/AuthProvider";
import { ToastProvider } from "../components/ui/Toast";

/** All app-wide providers. Tests use the same tree with a test API and query client. */
export function AppProviders({
  api,
  queryClient,
  children,
}: {
  api: AppApi;
  queryClient: QueryClient;
  children: ReactNode;
}) {
  return (
    <ApiProvider api={api}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <AuthProvider>{children}</AuthProvider>
        </ToastProvider>
      </QueryClientProvider>
    </ApiProvider>
  );
}
