import { createContext, use, type ReactNode } from "react";

import { ApiClient } from "./client";
import { createEndpoints, type Endpoints } from "./endpoints";
import { tokenStorage } from "../auth/tokenStorage";

export interface AppApi {
  /** REST base URL (VITE_API_BASE_URL); the WebSocket URL is derived from it. */
  baseUrl: string;
  client: ApiClient;
  endpoints: Endpoints;
  /** Subscribe to "an authenticated request got 401"; returns an unsubscribe function. */
  onUnauthorized: (listener: () => void) => () => void;
}

export function createAppApi(baseUrl: string, fetchImpl?: typeof fetch): AppApi {
  const listeners = new Set<() => void>();
  const client = new ApiClient({
    baseUrl,
    getToken: () => tokenStorage.get(),
    onUnauthorized: () => {
      for (const listener of listeners) listener();
    },
    ...(fetchImpl ? { fetchImpl } : {}),
  });
  return {
    baseUrl,
    client,
    endpoints: createEndpoints(client),
    onUnauthorized: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

const ApiContext = createContext<AppApi | null>(null);

export function ApiProvider({ api, children }: { api: AppApi; children: ReactNode }) {
  return <ApiContext value={api}>{children}</ApiContext>;
}

export function useApi(): AppApi {
  const api = use(ApiContext);
  if (!api) throw new Error("useApi must be used inside <ApiProvider>");
  return api;
}
