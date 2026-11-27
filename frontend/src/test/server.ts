import { setupServer } from "msw/node";

import { FakeBackend } from "./fakeBackend";

export const server = setupServer();

/** Fresh fake backend per test; returns it for seeding and assertions. */
export function startFakeBackend(): FakeBackend {
  const backend = new FakeBackend();
  server.resetHandlers(...backend.handlers());
  return backend;
}
