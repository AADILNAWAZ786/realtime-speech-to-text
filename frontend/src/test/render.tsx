import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";

import { createAppApi } from "../api/ApiProvider";
import { AppProviders } from "../app/AppProviders";
import { createQueryClient } from "../app/queryClient";
import { routes } from "../app/routes";
import { tokenStorage } from "../auth/tokenStorage";
import { API, type FakeBackend } from "./fakeBackend";

/** Render the whole app (real routes, providers and API client) at `path`. */
export function renderApp(path = "/") {
  const queryClient = createQueryClient();
  queryClient.setDefaultOptions({
    queries: { ...queryClient.getDefaultOptions().queries, retry: false },
  });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const user = userEvent.setup();
  const utils = render(
    <AppProviders api={createAppApi(API)} queryClient={queryClient}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { ...utils, user, router, queryClient };
}

/** Seed a user and store a valid token, as if they had signed in before a page refresh. */
export function signInAs(
  backend: FakeBackend,
  email = "asha@example.com",
  name: string | null = "Asha",
) {
  const account = backend.addUser(email, "correct-horse-1", name);
  tokenStorage.set(backend.issueToken(account), 3600);
  return account;
}

export function pathOf(router: ReturnType<typeof createMemoryRouter>) {
  return `${router.state.location.pathname}${router.state.location.search}`;
}
