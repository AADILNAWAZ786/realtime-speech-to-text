import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { tokenStorage } from "../auth/tokenStorage";
import { pathOf, renderApp, signInAs } from "./render";
import { startFakeBackend } from "./server";

describe("login", () => {
  it("validates fields before calling the API", async () => {
    const backend = startFakeBackend();
    const { user } = renderApp("/login");
    await user.click(await screen.findByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Enter your email address.")).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Email"), "not-an-email");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
    expect(backend.requests).toEqual([]);
  });

  it("shows a clear message for wrong credentials and stays signed out", async () => {
    const backend = startFakeBackend();
    backend.addUser("asha@example.com", "correct-horse-1");
    const { user, router } = renderApp("/login");
    await user.type(await screen.findByLabelText("Email"), "asha@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password.");
    expect(tokenStorage.get()).toBeNull();
    expect(pathOf(router)).toBe("/login");
  });

  it("signs in, stores the token and opens the dashboard", async () => {
    const backend = startFakeBackend();
    backend.addUser("asha@example.com", "correct-horse-1", "Asha");
    const { user, router } = renderApp("/login");
    await user.type(await screen.findByLabelText("Email"), "  Asha@Example.com ");
    await user.type(screen.getByLabelText("Password"), "correct-horse-1");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Welcome back, Asha" })).toBeInTheDocument();
    expect(pathOf(router)).toBe("/");
    expect(tokenStorage.get()).toMatch(/^token-/);
    // JWT goes in the Authorization header only, never in a URL.
    expect(backend.requests.join("\n")).not.toContain("token-");
  });

  it("explains when the server cannot be reached", async () => {
    const backend = startFakeBackend();
    backend.failWith = "network";
    const { user } = renderApp("/login");
    await user.type(await screen.findByLabelText("Email"), "asha@example.com");
    await user.type(screen.getByLabelText("Password"), "whatever-1");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/cannot reach the server/i);
  });
});

describe("registration", () => {
  it("validates password length and confirmation", async () => {
    const backend = startFakeBackend();
    const { user } = renderApp("/register");
    await user.type(await screen.findByLabelText("Email"), "new@example.com");
    await user.type(screen.getByLabelText("Password"), "short");
    await user.type(screen.getByLabelText("Confirm password"), "different");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByText("Use at least 8 characters.")).toBeInTheDocument();
    expect(screen.getByText("The passwords do not match.")).toBeInTheDocument();
    expect(backend.requests).toEqual([]);
  });

  it("creates the account, signs in and lands on the dashboard", async () => {
    const backend = startFakeBackend();
    const { user, router } = renderApp("/register");
    await user.type(await screen.findByLabelText("Name (optional)"), "Ravi");
    await user.type(screen.getByLabelText("Email"), "ravi@example.com");
    await user.type(screen.getByLabelText("Password"), "a-long-password");
    await user.type(screen.getByLabelText("Confirm password"), "a-long-password");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("heading", { name: "Welcome back, Ravi" })).toBeInTheDocument();
    expect(pathOf(router)).toBe("/");
    expect(backend.requests.slice(0, 3)).toEqual([
      "POST /auth/register",
      "POST /auth/login",
      "GET /auth/me",
    ]);
  });

  it("omits an empty name instead of sending null", async () => {
    const backend = startFakeBackend();
    const { user } = renderApp("/register");
    await user.type(await screen.findByLabelText("Email"), "noname@example.com");
    await user.type(screen.getByLabelText("Password"), "a-long-password");
    await user.type(screen.getByLabelText("Confirm password"), "a-long-password");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(backend.bodies[0]).toEqual({ email: "noname@example.com", password: "a-long-password" });
  });

  it("maps a duplicate email to the email field", async () => {
    const backend = startFakeBackend();
    backend.addUser("taken@example.com", "correct-horse-1");
    const { user } = renderApp("/register");
    await user.type(await screen.findByLabelText("Email"), "taken@example.com");
    await user.type(screen.getByLabelText("Password"), "a-long-password");
    await user.type(screen.getByLabelText("Confirm password"), "a-long-password");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(
      await screen.findByText("An account with this email already exists."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
  });
});

describe("protected routes and session persistence", () => {
  it("redirects anonymous users to /login and returns them afterwards", async () => {
    const backend = startFakeBackend();
    const account = backend.addUser("asha@example.com", "correct-horse-1");
    const record = backend.addTranscription(account, { title: "Standup notes", text: "hello" });
    const { user, router } = renderApp(`/transcriptions/${record.id}`);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(pathOf(router)).toBe("/login");
    await user.type(screen.getByLabelText("Email"), "asha@example.com");
    await user.type(screen.getByLabelText("Password"), "correct-horse-1");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Standup notes" })).toBeInTheDocument();
    expect(pathOf(router)).toBe(`/transcriptions/${record.id}`);
  });

  it("restores the user from a stored token after a refresh", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    renderApp("/");
    expect(screen.getByText("Restoring your session…")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Welcome back, Asha" })).toBeInTheDocument();
    expect(backend.requests[0]).toBe("GET /auth/me");
  });

  it("sends signed-in users away from /login", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    const { router } = renderApp("/login");
    await waitFor(() => expect(pathOf(router)).toBe("/"));
  });

  it("drops an invalid stored token and asks to sign in", async () => {
    startFakeBackend();
    tokenStorage.set("token-revoked", 3600);
    const { router } = renderApp("/");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(pathOf(router)).toBe("/login");
    expect(tokenStorage.get()).toBeNull();
  });

  it("ignores an expired stored token without calling the API", async () => {
    const backend = startFakeBackend();
    window.localStorage.setItem(
      tokenStorage.key,
      JSON.stringify({ token: "token-old", expiresAt: Date.now() - 1000 }),
    );
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(backend.requests).toEqual([]);
  });

  it("keeps the token and offers a retry when the server is down", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    backend.failWith = 500;
    const { user } = renderApp("/");
    expect(
      await screen.findByRole("heading", { name: "Cannot reach the server" }),
    ).toBeInTheDocument();
    expect(tokenStorage.get()).not.toBeNull();
    backend.failWith = null;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Welcome back, Asha" })).toBeInTheDocument();
  });

  it("signs out when an authenticated request is rejected with 401", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    const { router } = renderApp("/");
    await screen.findByRole("heading", { name: "Welcome back, Asha" });
    backend.tokens.clear(); // token revoked server-side
    await router.navigate("/?q=x"); // next authenticated request gets 401
    expect(
      await screen.findByText("Your session has ended. Please sign in again."),
    ).toBeInTheDocument();
    await waitFor(() => expect(pathOf(router)).toBe("/login"));
    expect(tokenStorage.get()).toBeNull();
  });

  it("logs out, clears the token and returns to /login", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    const { user, router } = renderApp("/");
    await screen.findByRole("heading", { name: "Welcome back, Asha" });
    await user.click(screen.getAllByRole("button", { name: "Sign out" })[0]!);
    await waitFor(() => expect(pathOf(router)).toBe("/login"));
    expect(tokenStorage.get()).toBeNull();
    expect(screen.getByText("Signed out")).toBeInTheDocument();
  });
});
