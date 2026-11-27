import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { FakeBackend } from "./fakeBackend";
import { pathOf, renderApp, signInAs } from "./render";
import { startFakeBackend } from "./server";

function seed(backend: FakeBackend, count: number) {
  const account = signInAs(backend);
  for (let i = 1; i <= count; i++) {
    backend.addTranscription(account, {
      title: `Meeting ${i}`,
      text: i % 2 ? `Aaj ki meeting mein budget discuss kiya ${i}` : `Weekly sync notes ${i}`,
      source: i % 3 === 0 ? "live" : "manual",
      speech_mode: i % 2 ? "mixed" : "en",
      duration_ms: i % 3 === 0 ? 65_000 : null,
    });
  }
  return account;
}

const list = () => screen.findByRole("list", { name: "Transcriptions" });

describe("dashboard", () => {
  it("shows skeletons while loading, then stats and the newest transcriptions", async () => {
    const backend = startFakeBackend();
    seed(backend, 12);
    renderApp("/");
    expect(await screen.findByLabelText("Loading transcriptions")).toBeInTheDocument();
    const items = within(await list()).getAllByRole("listitem");
    expect(items).toHaveLength(10);
    expect(within(items[0]!).getByRole("link", { name: "Meeting 12" })).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/transcriptions\//),
    );
    expect(within(items[0]!).getByText("1:05")).toBeInTheDocument(); // duration
    expect(within(items[0]!).getByText("Live recording")).toBeInTheDocument();
    expect(await screen.findByLabelText("Total transcriptions: 12")).toBeInTheDocument();
    expect(screen.getByLabelText("Completed recordings: 4")).toBeInTheDocument();
    expect(screen.getByLabelText("Manual transcriptions: 8")).toBeInTheDocument();
    expect(screen.getByText(/Showing/)).toHaveTextContent("Showing 1–10 of 12");
  });

  it("links View and Edit actions to the detail page", async () => {
    const backend = startFakeBackend();
    seed(backend, 1);
    renderApp("/");
    await list();
    const id = backend.transcriptions[0]!.id;
    expect(screen.getByRole("link", { name: "View Meeting 1" })).toHaveAttribute(
      "href",
      `/transcriptions/${id}`,
    );
    expect(screen.getByRole("link", { name: "Edit Meeting 1" })).toHaveAttribute(
      "href",
      `/transcriptions/${id}?edit=1`,
    );
  });

  it("paginates through the URL", async () => {
    const backend = startFakeBackend();
    seed(backend, 12);
    const { user, router } = renderApp("/");
    await list();
    await user.click(screen.getByRole("button", { name: "Page 2" }));
    await waitFor(() =>
      expect(screen.getByText(/Showing/)).toHaveTextContent("Showing 11–12 of 12"),
    );
    expect(pathOf(router)).toBe("/?page=2");
    expect(within(await list()).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    expect(backend.requests).toContain("GET /transcriptions?page=2&page_size=10");
    await user.click(screen.getByRole("button", { name: "Previous page" }));
    await waitFor(() => expect(pathOf(router)).toBe("/"));
  });

  it("changes the page size", async () => {
    const backend = startFakeBackend();
    seed(backend, 12);
    const { user, router } = renderApp("/?page=2");
    await list();
    await user.selectOptions(screen.getByLabelText("Per page"), "20");
    await waitFor(() => expect(pathOf(router)).toBe("/?page_size=20"));
    await waitFor(async () =>
      expect(within(await list()).getAllByRole("listitem")).toHaveLength(12),
    );
  });

  it("searches with a debounce and resets to page 1", async () => {
    const backend = startFakeBackend();
    seed(backend, 12);
    const { user, router } = renderApp("/?page=2");
    await list();
    await user.type(screen.getByRole("searchbox", { name: "Search transcriptions" }), "budget");
    await waitFor(() => expect(pathOf(router)).toBe("/?q=budget"));
    await waitFor(() => expect(screen.getByText(/Showing/)).toHaveTextContent("of 6"));
    // One request for the final term, not one per keystroke.
    expect(backend.requests.filter((r) => r.includes("q="))).toEqual([
      "GET /transcriptions?page=1&page_size=10&q=budget",
    ]);
  });

  it("filters by language mode, status and source", async () => {
    const backend = startFakeBackend();
    seed(backend, 12);
    const { user, router } = renderApp("/");
    await list();
    await user.selectOptions(screen.getByLabelText("Filter by source"), "live");
    await user.selectOptions(screen.getByLabelText("Filter by language mode"), "en");
    await user.selectOptions(screen.getByLabelText("Filter by status"), "completed");
    await waitFor(() =>
      expect(pathOf(router)).toBe("/?source=live&speech_mode=en&status=completed"),
    );
    await waitFor(async () =>
      expect(within(await list()).getAllByRole("listitem")).toHaveLength(2),
    );
  });

  it("shows a no-results state with a way to clear filters", async () => {
    const backend = startFakeBackend();
    seed(backend, 3);
    const { user, router } = renderApp("/?q=nothing-matches");
    expect(await screen.findByText("No matching transcriptions")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(pathOf(router)).toBe("/"));
    expect(screen.getByRole("searchbox")).toHaveValue("");
    expect(within(await list()).getAllByRole("listitem")).toHaveLength(3);
  });

  it("shows an empty state for a new account", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    renderApp("/");
    expect(await screen.findByText("No transcriptions yet")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Pagination" })).not.toBeInTheDocument();
  });

  it("shows an error state with retry when the list fails", async () => {
    const backend = startFakeBackend();
    seed(backend, 2);
    backend.failWith = 500;
    backend.failOnly = "GET /transcriptions?page=1&page_size=10";
    const { user } = renderApp("/");
    expect(
      await screen.findByRole("heading", { name: "Could not load transcriptions" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/internal server error/i)).toBeInTheDocument();
    backend.failWith = null;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(within(await list()).getAllByRole("listitem")).toHaveLength(2);
  });

  it("moves to the last page when the requested page is past the end", async () => {
    const backend = startFakeBackend();
    seed(backend, 12);
    const { router } = renderApp("/?page=9");
    await waitFor(() => expect(pathOf(router)).toBe("/?page=2"));
  });
});
