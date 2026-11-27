/**
 * The recording page end to end in jsdom: real hooks, real socket client, real worklet DSP
 * (via the fake AudioWorkletNode), a FakeWebSocket playing the server, and the MSW fake
 * REST backend for history.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { tokenStorage } from "../auth/tokenStorage";
import {
  installFakeAudio,
  mediaError,
  tone,
  uninstallFakeAudio,
  type FakeAudio,
} from "./fakeAudio";
import type { FakeBackend } from "./fakeBackend";
import { FakeWebSocket, sessionSaved, sessionStarted } from "./fakeWebSocket";
import { pathOf, renderApp, signInAs } from "./render";
import { startFakeBackend } from "./server";

let audio: FakeAudio;

beforeEach(() => {
  FakeWebSocket.instances = [];
  audio = installFakeAudio({ sampleRate: 48_000 });
  vi.stubGlobal("WebSocket", FakeWebSocket);
});

afterEach(uninstallFakeAudio);

const server = () => FakeWebSocket.latest();
const send = (message: object | string) => act(() => server().receive(message));
const feed = (seconds: number) => act(() => audio.worklet().feed(tone(seconds)));
const startButton = () => screen.getByRole("button", { name: "Start recording" });
const stopButton = () => screen.getByRole("button", { name: "Stop recording" });
const status = () => screen.getByTestId("recording-status");

async function openRecordPage(backend: FakeBackend, path = "/record") {
  signInAs(backend);
  const view = renderApp(path);
  if (path === "/record") await screen.findByRole("heading", { name: "New recording" });
  return view;
}

/** Click start, accept the socket and send session_started. */
async function startRecording(user: ReturnType<typeof renderApp>["user"]) {
  await user.click(startButton());
  await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
  act(() => server().open());
  send(sessionStarted());
  await screen.findByText("Recording", { selector: "p" });
}

const partial = (utterance_id: number, version: number, text: string) => ({
  type: "partial",
  utterance_id,
  version,
  text,
  raw_text: text,
  start_ms: utterance_id * 3000,
  end_ms: utterance_id * 3000 + 900,
});
const final = (seq: number, utterance_id: number, text: string, saved = true) => ({
  type: "final",
  seq,
  utterance_id,
  text,
  raw_text: text,
  start_ms: utterance_id * 3000,
  end_ms: utterance_id * 3000 + 1800,
  language: "hi",
  avg_logprob: -0.2,
  commit_reason: "silence",
  saved,
});

describe("recording page", () => {
  it("does not touch the microphone or the network until Start is clicked", async () => {
    const backend = startFakeBackend();
    await openRecordPage(backend);
    expect(status()).toHaveTextContent("Ready to record");
    expect(screen.getByLabelText("Language")).toHaveValue("mixed");
    expect(screen.getByLabelText("Output script")).toBeInTheDocument();
    expect(audio.getUserMedia).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("records end to end: permission, start, audio, partials, finals, stop, session_saved, history", async () => {
    const backend = startFakeBackend();
    const account = signInAs(backend);
    const { user, router } = renderApp("/"); // dashboard first, so its list is cached
    await screen.findByText("No transcriptions yet");
    await user.click(screen.getAllByRole("link", { name: "New recording" })[0]!);
    await screen.findByRole("heading", { name: "New recording" });

    await user.selectOptions(screen.getByLabelText("Output script"), "roman");
    await user.type(screen.getByLabelText("Title (optional)"), "Standup");
    await user.click(startButton());

    // Permission first, then the socket; audio is held back until session_started.
    expect(audio.getUserMedia).toHaveBeenCalledOnce();
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(status()).toHaveTextContent("Connecting");
    expect(server().url).toBe("ws://api.test/ws/transcribe");
    act(() => server().open());
    expect(server().textMessages[0]).toEqual({
      type: "start",
      token: tokenStorage.get(),
      speech_mode: "mixed",
      script_preference: "roman",
      format: "pcm_s16le",
      sample_rate: 16000,
      channels: 1,
      title: "Standup",
    });
    feed(0.3); // the microphone is not connected to the worklet yet
    expect(server().audioFrames).toHaveLength(0); // nothing is sent before session_started

    send(sessionStarted());
    await screen.findByText("Recording", { selector: "p" });
    expect(screen.getByText(/48\.0 kHz mic → 16 kHz/)).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByLabelText("Language")).toBeDisabled();

    feed(1); // 1 s of 48 kHz stereo -> 16 kHz mono PCM16
    expect(server().audioFrames.length).toBeGreaterThanOrEqual(9);
    expect(server().audioFrames.every((f) => f.byteLength === 3200)).toBe(true);
    expect(screen.getByRole("timer")).toHaveTextContent("0:01");
    expect(Number(screen.getByRole("meter").getAttribute("aria-valuenow"))).toBeGreaterThan(50);

    // Partials replace each other; a final replaces its partial.
    send(partial(0, 1, "aaj"));
    send(partial(0, 2, "aaj ki meeting"));
    const transcript = screen.getByLabelText("Transcript");
    let partials = transcript.querySelectorAll('[data-kind="partial"]');
    expect(partials).toHaveLength(1);
    expect(partials[0]).toHaveTextContent("aaj ki meeting");
    expect(partials[0]?.querySelector("p")).toHaveClass("italic");

    send(final(0, 0, "aaj ki meeting cancel ho gai"));
    send(partial(1, 1, "kal phir"));
    send(final(1, 1, "kal phir call karte hain", false));
    send({ type: "error", code: "persistence_failed", message: "x", fatal: false, seq: 1 });
    const finals = transcript.querySelectorAll('[data-kind="final"]');
    expect([...finals].map((f) => f.getAttribute("data-seq"))).toEqual(["0", "1"]);
    expect(finals[0]).toHaveTextContent("aaj ki meeting cancel ho gai");
    expect(finals[1]).toHaveTextContent("not saved yet");
    partials = transcript.querySelectorAll('[data-kind="partial"]');
    expect(partials).toHaveLength(0);

    send({ type: "preview_paused", backlog_ms: 4200 });
    expect(screen.getByText(/preview paused/i)).toBeInTheDocument();
    send({ type: "preview_resumed", backlog_ms: 200 });
    expect(screen.queryByText(/preview paused/i)).not.toBeInTheDocument();

    // Stop: tail frames, then stop; the socket stays open for session_saved.
    const framesBeforeStop = server().audioFrames.length;
    await user.click(stopButton());
    await waitFor(() =>
      expect(server().textMessages.map((m) => m.type)).toEqual(["start", "stop"]),
    );
    expect(server().audioFrames.length).toBeGreaterThan(framesBeforeStop); // flushed tail
    expect(server().sent.at(-1)).toBe(JSON.stringify({ type: "stop" }));
    const totalSamples = server().audioBytes / 2;
    expect(totalSamples).toBe(16_000); // exactly the 1 s captured after session_started
    expect(server().closedByClient).toBeNull();
    expect(status()).toHaveTextContent("Processing final audio");

    // The server stores the recording and confirms it.
    const record = backend.addTranscription(account, {
      title: "Standup",
      text: "aaj ki meeting cancel ho gai kal phir call karte hain",
      source: "live",
      speech_mode: "mixed",
      script_preference: "roman",
      duration_ms: 1300,
    });
    send(sessionSaved({ transcription_id: record.id, duration_ms: 1300, unsaved_segments: [] }));
    act(() => server().serverClose(1000));
    await screen.findByText("Recording saved");
    expect(screen.getByLabelText("Recording result")).toHaveTextContent("Saved");
    expect(screen.getByLabelText("Recording result")).toHaveTextContent(
      "Duration 0:01 · 2 segments saved",
    );
    expect(screen.getByLabelText("Recording result")).not.toHaveTextContent("not saved");
    expect(screen.getByRole("link", { name: "Open transcription" })).toHaveAttribute(
      "href",
      `/transcriptions/${record.id}`,
    );
    expect(audio.stream.tracks[0]?.stop).toHaveBeenCalled();
    expect(audio.context().closeSpy).toHaveBeenCalledOnce();

    // History was invalidated: the dashboard shows the new recording without a reload.
    await user.click(screen.getAllByRole("link", { name: "Dashboard" })[0]!);
    const list = await screen.findByRole("list", { name: "Transcriptions" });
    expect(within(list).getByRole("link", { name: "Standup" })).toBeInTheDocument();
    expect(within(list).getByText("Live recording")).toBeInTheDocument();
    expect(pathOf(router)).toBe("/");
  });

  it("shows unsaved segments and failed utterances from session_saved", async () => {
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    await startRecording(user);
    send({
      type: "utterance_failed",
      utterance_id: 3,
      start_ms: 9000,
      end_ms: 9500,
      code: "inference_failed",
    });
    await user.click(stopButton());
    send(
      sessionSaved({
        final_count: 3,
        saved_segments: 2,
        unsaved_segments: [2],
        failed_utterances: 1,
      }),
    );
    const result = await screen.findByLabelText("Recording result");
    expect(result).toHaveTextContent("Completed, but some segments were not saved");
    expect(result).toHaveTextContent("2 segments saved · 1 not saved");
    expect(result).toHaveTextContent("segment #3");
    expect(result).toHaveTextContent("1 part of the audio could not be transcribed");
    expect(status()).toHaveTextContent("Recording completed, but not fully saved");
    expect(screen.queryByText("Recording saved")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Transcript")).toHaveTextContent(
      "1 part of the audio could not be transcribed",
    );
  });

  it("explains a denied microphone and never opens a socket", async () => {
    audio = installFakeAudio({ getUserMediaError: mediaError("NotAllowedError") });
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    await user.click(startButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/allow the microphone/i);
    expect(status()).toHaveTextContent("Could not record");
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(startButton()).toBeEnabled(); // can try again
  });

  it("explains a missing microphone", async () => {
    audio = installFakeAudio({ getUserMediaError: mediaError("NotFoundError") });
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    await user.click(startButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/no microphone was found/i);
  });

  it("handles an authentication failure and releases the microphone", async () => {
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    await user.click(startButton());
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    act(() => server().open());
    send({ type: "error", code: "invalid_token", message: "Invalid token", fatal: true });
    act(() => server().serverClose(4401));
    expect(await screen.findByRole("alert")).toHaveTextContent(/sign in again/i);
    expect(screen.getByRole("button", { name: "Sign in again" })).toBeInTheDocument();
    expect(audio.stream.tracks[0]?.stop).toHaveBeenCalled();
    expect(audio.context().closeSpy).toHaveBeenCalled();
  });

  it("reports a lost connection as interrupted, with a link to what was saved", async () => {
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    await startRecording(user);
    feed(0.5);
    send(final(0, 0, "pehla vakya"));
    send(partial(1, 1, "doosra adhoora"));
    act(() => server().serverClose(1006));
    await screen.findByText("Recording interrupted");
    expect(screen.queryByText("Recording saved")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /connection to the transcription server was lost/i,
    );
    expect(screen.getByLabelText("Recording result")).toHaveTextContent(
      "Interrupted (not confirmed by the server)",
    );
    expect(screen.getByLabelText("Recording result")).toHaveTextContent(
      "1 line shown below was confirmed saved",
    );
    expect(screen.getByRole("link", { name: "Open transcription" })).toHaveAttribute(
      "href",
      `/transcriptions/${sessionStarted().transcription_id}`,
    );
    expect(screen.getByText("pehla vakya")).toBeInTheDocument(); // saved final kept
    expect(screen.queryByText("doosra adhoora")).not.toBeInTheDocument(); // never-stored partial removed
    expect(audio.context().closeSpy).toHaveBeenCalled();
  });

  it("reports a server-side stop (fatal error, then session_saved interrupted)", async () => {
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    await startRecording(user);
    send({ type: "error", code: "server_overloaded", message: "x", fatal: true });
    send(sessionSaved({ status: "interrupted", end_reason: "overloaded" }));
    await screen.findByText("Recording interrupted");
    expect(screen.getByRole("alert")).toHaveTextContent(/fell too far behind/i);
  });

  it("explains close code 1009", async () => {
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    await startRecording(user);
    act(() => server().serverClose(1009));
    expect(await screen.findByRole("alert")).toHaveTextContent(/too large/i);
  });

  it("starts only one recording on a double click", async () => {
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    await user.dblClick(startButton());
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(audio.getUserMedia).toHaveBeenCalledOnce();
  });

  it("stops cleanly when the microphone is unplugged", async () => {
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    await startRecording(user);
    feed(0.2);
    act(() => audio.stream.tracks[0]?.unplug());
    await waitFor(() => expect(server().textMessages.at(-1)).toEqual({ type: "stop" }));
    expect(screen.getByRole("note")).toHaveTextContent(/microphone was disconnected/i);
  });

  it("can cancel while connecting", async () => {
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    await user.click(startButton());
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(status()).toHaveTextContent("Ready to record");
    expect(server().closedByClient).not.toBeNull();
    expect(audio.context().closeSpy).toHaveBeenCalled();
  });

  it("asks before leaving during a recording, then stops and finishes in the background", async () => {
    const backend = startFakeBackend();
    const { user, router, queryClient } = await openRecordPage(backend);
    await startRecording(user);
    feed(0.5);

    await user.click(screen.getAllByRole("link", { name: "Dashboard" })[0]!);
    const dialog = await screen.findByRole("dialog", { name: "Leave while recording?" });
    await user.click(within(dialog).getByRole("button", { name: "Keep recording" }));
    expect(pathOf(router)).toBe("/record");
    expect(status()).toHaveTextContent("Recording");

    await user.click(screen.getAllByRole("link", { name: "Dashboard" })[0]!);
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Stop and leave" }),
    );
    await waitFor(() => expect(pathOf(router)).toBe("/"));
    expect(server().textMessages.at(-1)).toEqual({ type: "stop" });
    expect(server().closedByClient).toBeNull(); // still waiting for session_saved
    expect(audio.context().closeSpy).toHaveBeenCalled(); // microphone released

    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    send(sessionSaved());
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["transcriptions"] });
  });

  it("aborts an active recording if the page unmounts unexpectedly", async () => {
    const backend = startFakeBackend();
    const { user, unmount } = await openRecordPage(backend);
    await startRecording(user);
    unmount();
    expect(server().closedByClient).toEqual({ code: 4000 });
    expect(audio.context().closeSpy).toHaveBeenCalled();
  });

  it("warns before the tab is closed while recording", async () => {
    const backend = startFakeBackend();
    const { user } = await openRecordPage(backend);
    const idle = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(idle);
    expect(idle.defaultPrevented).toBe(false);
    await startRecording(user);
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  describe("what was saved (session_saved fields)", () => {
    const cases = [
      {
        name: "completed and fully saved",
        saved: {},
        headline: "Saved",
        statusText: "Recording saved",
        details: ["Duration 0:07 · 2 segments saved"],
      },
      {
        name: "completed, some segments not saved",
        saved: { saved_segments: 1, unsaved_segments: [1] },
        headline: "Completed, but some segments were not saved",
        statusText: "Recording completed, but not fully saved",
        details: ["1 segment saved · 1 not saved", "segment #2"],
      },
      {
        name: "finished, but database finalisation failed",
        saved: { record_saved: false },
        headline: "Finished, but the recording could not be finalised",
        statusText: "Recording finished, but saving failed",
        details: ["2 segments saved", "could not finish storing this recording"],
      },
      {
        name: "interrupted",
        saved: {
          status: "interrupted",
          end_reason: "idle_timeout",
          saved_segments: 1,
          final_count: 1,
        },
        headline: "Interrupted",
        statusText: "Recording interrupted",
        details: ["1 segment saved"],
      },
    ];

    it.each(cases)("$name", async ({ saved, headline, statusText, details }) => {
      const backend = startFakeBackend();
      const { user } = await openRecordPage(backend);
      await startRecording(user);
      await user.click(stopButton());
      await waitFor(() => expect(server().textMessages.at(-1)).toEqual({ type: "stop" }));
      send(sessionSaved({ duration_ms: 6900, ...saved }));
      const result = await screen.findByLabelText("Recording result");
      expect(within(result).getByText(headline, { selector: "p" })).toBeInTheDocument();
      for (const text of details) expect(result).toHaveTextContent(text);
      expect(status()).toHaveTextContent(statusText);
      if (statusText !== "Recording saved") {
        expect(screen.queryByText("Recording saved")).not.toBeInTheDocument();
        expect(result).not.toHaveTextContent(/^Saved/);
      }
      // The record exists in every case: keep the way to it.
      expect(within(result).getByRole("link", { name: "Open transcription" })).toHaveAttribute(
        "href",
        `/transcriptions/${sessionStarted().transcription_id}`,
      );
    });
  });

  describe("when the last audio cannot be flushed", () => {
    it("waits for a slow worklet, then stops normally", async () => {
      const backend = startFakeBackend();
      const { user } = await openRecordPage(backend);
      await startRecording(user);
      feed(0.5);
      audio.worklet().flushDelayMs = 800;
      await user.click(stopButton());
      expect(server().textMessages.map((m) => m.type)).toEqual(["start"]); // not yet
      await waitFor(() => expect(server().textMessages.at(-1)).toEqual({ type: "stop" }));
      expect(server().audioBytes / 2).toBe(8000); // the whole 0.5 s, tail included
      send(sessionSaved());
      await screen.findByText("Recording saved");
    });

    it("never sends stop; aborts so the server keeps it as interrupted; says what was lost", async () => {
      const backend = startFakeBackend();
      const { user } = await openRecordPage(backend);
      await startRecording(user);
      feed(0.5);
      send(final(0, 0, "pehla vakya"));
      audio.worklet().respondToFlush = false;
      await user.click(stopButton());
      expect(status()).toHaveTextContent("Processing final audio");

      await waitFor(() => expect(status()).toHaveTextContent("Recording interrupted"), {
        timeout: 4_000,
      });
      // No normal stop: the server would otherwise store a "completed" recording.
      expect(server().textMessages.map((m) => m.type)).toEqual(["start"]);
      expect(server().closedByClient).toEqual({ code: 4000 }); // server: disconnected -> interrupted
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent(/did not return the last part of the recording/i);
      expect(alert).toHaveTextContent(/final seconds were not sent or transcribed/i);
      expect(screen.queryByText("Recording saved")).not.toBeInTheDocument();
      expect(screen.getByLabelText("Recording result")).toHaveTextContent(
        "Interrupted (not confirmed by the server)",
      );
      expect(screen.getByText("pehla vakya")).toBeInTheDocument();
      // Released regardless.
      expect(audio.stream.tracks[0]?.stop).toHaveBeenCalled();
      expect(audio.context().closeSpy).toHaveBeenCalledOnce();
      expect(startButton()).toBeEnabled(); // a new recording can start
    });

    it("cleans up on unmount after a failed flush", async () => {
      const backend = startFakeBackend();
      const { user, unmount } = await openRecordPage(backend);
      await startRecording(user);
      audio.worklet().respondToFlush = false;
      await user.click(stopButton());
      await waitFor(() => expect(status()).toHaveTextContent("Recording interrupted"), {
        timeout: 4_000,
      });
      const sockets = FakeWebSocket.instances.length;
      unmount();
      expect(FakeWebSocket.instances).toHaveLength(sockets); // nothing reopened
      expect(audio.context().closeSpy).toHaveBeenCalledOnce(); // not closed twice
    });

    it("keeps the user on the page if 'Stop and leave' fails, then lets them leave", async () => {
      const backend = startFakeBackend();
      const { user, router } = await openRecordPage(backend);
      await startRecording(user);
      audio.worklet().respondToFlush = false;

      await user.click(screen.getAllByRole("link", { name: "Dashboard" })[0]!);
      const dialog = await screen.findByRole("dialog", { name: "Leave while recording?" });
      await user.click(within(dialog).getByRole("button", { name: "Stop and leave" }));

      await waitFor(() => expect(status()).toHaveTextContent("Recording interrupted"), {
        timeout: 4_000,
      });
      expect(pathOf(router)).toBe("/record"); // stayed to show the failure
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent(/final seconds were not sent/i);
      expect(server().textMessages.map((m) => m.type)).toEqual(["start"]);

      // Nothing is active any more: leaving is no longer blocked.
      await user.click(screen.getAllByRole("link", { name: "Dashboard" })[0]!);
      await waitFor(() => expect(pathOf(router)).toBe("/"));
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });
});
