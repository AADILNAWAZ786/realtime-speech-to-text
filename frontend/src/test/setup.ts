import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";

import { tokenStorage } from "../auth/tokenStorage";
import { server } from "./server";

// jsdom has no layout engine; pagination scrolls to the top.
window.scrollTo = () => undefined;

// Any request without a handler fails the test: nothing may reach a real network.
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  tokenStorage.clear();
  window.localStorage.clear();
});
afterAll(() => server.close());
