import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { http, HttpResponse } from "msw";

import { LIMITS } from "../api/types";
import { API } from "./fakeBackend";
import { pathOf, renderApp, signInAs } from "./render";
import { server, startFakeBackend } from "./server";

const SEGMENTS = [
  {
    seq: 0,
    start_ms: 0,
    end_ms: 2400,
    raw_text: "aaj ki meeting",
    language: "hi",
    avg_logprob: -0.2,
  },
  {
    seq: 1,
    start_ms: 2400,
    end_ms: 5100,
    raw_text: "budget discuss karte hain",
    language: "hi",
    avg_logprob: -0.3,
  },
];

function seedLive() {
  const backend = startFakeBackend();
  const account = signInAs(backend);
  const record = backend.addTranscription(account, {
    title: "Team sync",
    text: "Aaj ki meeting. Budget discuss karte hain.",
    source: "live",
    speech_mode: "mixed",
    script_preference: "roman",
    detected_language: "hi",
    duration_ms: 5100,
    segments: SEGMENTS,
  });
  return { backend, account, record };
}

describe("create", () => {
  it("validates, creates and opens the new transcription", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    const { user, router } = renderApp("/transcriptions/new");
    await user.click(await screen.findByRole("button", { name: "Save transcription" }));
    expect(screen.getByText("Enter the transcription text.")).toBeInTheDocument();
    expect(backend.requests.some((r) => r.startsWith("POST"))).toBe(false);

    await user.type(screen.getByLabelText("Title (optional)"), "  Call with Priya ");
    await user.selectOptions(screen.getByLabelText("Language mode"), "mixed");
    await user.selectOptions(screen.getByLabelText("Output script"), "devanagari");
    await user.type(screen.getByLabelText("Text"), "Kal ka plan finalize karna hai.");
    await user.click(screen.getByRole("button", { name: "Save transcription" }));

    expect(await screen.findByRole("heading", { name: "Call with Priya" })).toBeInTheDocument();
    expect(backend.bodies.at(-1)).toEqual({
      title: "Call with Priya",
      text: "Kal ka plan finalize karna hai.",
      speech_mode: "mixed",
      script_preference: "devanagari",
    });
    expect(pathOf(router)).toBe(`/transcriptions/${backend.transcriptions[0]!.id}`);
    expect(screen.getByText("Transcription saved")).toBeInTheDocument();
  });

  it("omits an empty title so the server assigns one", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    const { user } = renderApp("/transcriptions/new");
    await user.type(await screen.findByLabelText("Text"), "Just text");
    await user.click(screen.getByRole("button", { name: "Save transcription" }));
    await screen.findByText("Transcription saved");
    expect(backend.bodies.at(-1)).not.toHaveProperty("title");
  });

  it("shows server validation errors next to the field", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    // A rule only the server knows about (e.g. the UTF-8 byte limit) rejects the text.
    server.use(
      http.post(`${API}/transcriptions`, () =>
        HttpResponse.json(
          {
            detail: "Request validation failed",
            code: "validation_error",
            errors: [
              { loc: ["body", "text"], msg: "Value error, text is too long", type: "value_error" },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    const { user } = renderApp("/transcriptions/new");
    await user.type(await screen.findByLabelText("Text"), "नमस्ते");
    await user.click(screen.getByRole("button", { name: "Save transcription" }));
    expect(await screen.findByText("text is too long")).toBeInTheDocument();
    expect(screen.getByLabelText("Text")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent("Request validation failed");
  });

  it("enforces the text length limit", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    const { user } = renderApp("/transcriptions/new");
    const text = await screen.findByLabelText("Text");
    // Paste rather than type 100k characters.
    await user.click(text);
    await user.paste("a".repeat(LIMITS.textMax + 1));
    await user.click(screen.getByRole("button", { name: "Save transcription" }));
    expect(screen.getByText("Use at most 100,000 characters.")).toBeInTheDocument();
  });
});

describe("detail", () => {
  it("shows metadata, the full text and raw segments separately", async () => {
    const { backend, record } = seedLive();
    renderApp(`/transcriptions/${record.id}`);
    expect(await screen.findByRole("heading", { name: "Team sync" })).toBeInTheDocument();
    expect(backend.requests).toContain(`GET /transcriptions/${record.id}?include_segments=true`);
    expect(screen.getByText("Hindi + English (Hinglish)")).toBeInTheDocument();
    expect(screen.getByText("Roman (Latin script)")).toBeInTheDocument();
    expect(screen.getByText("Hindi (hi)")).toBeInTheDocument();
    expect(screen.getByText("0:05")).toBeInTheDocument();
    expect(screen.getByText("Aaj ki meeting. Budget discuss karte hain.")).toBeInTheDocument();
    const segments = within(screen.getByRole("list", { name: "Recognised segments" })).getAllByRole(
      "listitem",
    );
    expect(segments).toHaveLength(2);
    expect(segments[1]).toHaveTextContent("0:02.4–0:05.1");
    expect(segments[1]).toHaveTextContent("budget discuss karte hain");
  });

  it("shows a not-found state for an unknown or foreign id", async () => {
    const backend = startFakeBackend();
    signInAs(backend);
    const stranger = backend.addUser("other@example.com", "another-pass-1");
    const foreign = backend.addTranscription(stranger, { title: "Private", text: "secret" });
    renderApp(`/transcriptions/${foreign.id}`);
    expect(await screen.findByText("Transcription not found")).toBeInTheDocument();
    expect(screen.queryByText("secret")).not.toBeInTheDocument();
  });
});

describe("edit", () => {
  it("opens in edit mode from ?edit=1 and PATCHes only the changed field", async () => {
    const { backend, record } = seedLive();
    const { user, router } = renderApp(`/transcriptions/${record.id}?edit=1`);
    const text = await screen.findByLabelText("Text");
    await user.clear(text);
    await user.type(text, "Aaj ki meeting mein budget final hua.");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("Changes saved")).toBeInTheDocument();
    expect(backend.bodies.at(-1)).toEqual({ text: "Aaj ki meeting mein budget final hua." });
    expect(screen.getByText("Aaj ki meeting mein budget final hua.")).toBeInTheDocument();
    expect(screen.getByText("Edited")).toBeInTheDocument();
    expect(pathOf(router)).toBe(`/transcriptions/${record.id}`);
    // Raw segments are untouched by the edit.
    expect(screen.getByText("budget discuss karte hain")).toBeInTheDocument();
  });

  it("requires a title when editing", async () => {
    const { backend, record } = seedLive();
    const { user } = renderApp(`/transcriptions/${record.id}`);
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.clear(screen.getByLabelText("Title"));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(screen.getByText("Enter a title.")).toBeInTheDocument();
    expect(backend.requests.some((r) => r.startsWith("PATCH"))).toBe(false);
  });

  it("asks before discarding unsaved changes", async () => {
    const { backend, record } = seedLive();
    const { user } = renderApp(`/transcriptions/${record.id}?edit=1`);
    await user.type(await screen.findByLabelText("Title"), " (draft)");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    const dialog = screen.getByRole("dialog", { name: "Discard your changes?" });
    await user.click(within(dialog).getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Title")).toHaveValue("Team sync (draft)");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Discard changes" }),
    );
    expect(screen.getByRole("heading", { name: "Team sync" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Title")).not.toBeInTheDocument();
    expect(backend.requests.some((r) => r.startsWith("PATCH"))).toBe(false);
  });
});

describe("delete", () => {
  it("deletes only after confirmation and returns to the dashboard", async () => {
    const { backend, record } = seedLive();
    const { user, router } = renderApp(`/transcriptions/${record.id}`);
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog", { name: "Delete this transcription?" });
    expect(dialog).toHaveTextContent("“Team sync”");

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(backend.transcriptions).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(pathOf(router)).toBe("/"));
    expect(backend.requests).toContain(`DELETE /transcriptions/${record.id}`);
    // The deleted record is never re-requested (no 404 flash).
    const afterDelete = backend.requests.slice(
      backend.requests.indexOf(`DELETE /transcriptions/${record.id}`) + 1,
    );
    expect(afterDelete.filter((r) => r.includes(record.id))).toEqual([]);
    expect(backend.transcriptions).toHaveLength(0);
    expect(await screen.findByText("No transcriptions yet")).toBeInTheDocument();
    expect(screen.getByText("Transcription deleted")).toBeInTheDocument();
  });

  it("reports a failed delete and keeps the transcription", async () => {
    const { backend, record } = seedLive();
    const { user, router } = renderApp(`/transcriptions/${record.id}`);
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    backend.failWith = 500;
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("Could not delete")).toBeInTheDocument();
    expect(pathOf(router)).toBe(`/transcriptions/${record.id}`);
    expect(backend.transcriptions).toHaveLength(1);
  });
});
