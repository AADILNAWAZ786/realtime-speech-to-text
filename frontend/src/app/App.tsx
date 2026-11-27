import { useState } from "react";
import { createBrowserRouter, RouterProvider } from "react-router";

import { createAppApi } from "../api/ApiProvider";
import { AppProviders } from "./AppProviders";
import { createQueryClient } from "./queryClient";
import { routes } from "./routes";

export function App({ apiBaseUrl }: { apiBaseUrl: string }) {
  const [api] = useState(() => createAppApi(apiBaseUrl));
  const [queryClient] = useState(createQueryClient);
  const [router] = useState(() => createBrowserRouter(routes));
  return (
    <AppProviders api={api} queryClient={queryClient}>
      <RouterProvider router={router} />
    </AppProviders>
  );
}
