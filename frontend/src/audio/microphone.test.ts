import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FakeAudioContext,
  type FakeStream,
  installFakeAudio,
  mediaError,
  tone,
  uninstallFakeAudio,
} from "../test/fakeAudio";
import {
  MicrophoneCapture,
  MicrophoneError,
  WORKLET_URL,
  classifyGetUserMediaError,
  meterLevel,
  type AudioFrame,
} from "./microphone";

afterEach(uninstallFakeAudio);

async function openCapture(frames: AudioFrame[] = [], onDeviceLost = vi.fn()) {
  const capture = new MicrophoneCapture({ onFrame: (f) => frames.push(f), onDeviceLost });
  await capture.open();
  return capture;
}

async function openError(): Promise<MicrophoneError> {
  try {
    await openCapture();
  } catch (error) {
    if (error instanceof MicrophoneError) return error;
    throw error;
  }
  throw new Error("expected open() to fail");
}

describe("MicrophoneCapture", () => {
  it("asks for audio only and builds a silent graph with the worklet", async () => {
    const audio = installFakeAudio({ sampleRate: 48_000 });
    const capture = await openCapture();
    expect(audio.getUserMedia).toHaveBeenCalledOnce();
    expect(audio.getUserMedia).toHaveBeenCalledWith({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
    const context = audio.context();
    expect(context.addModule).toHaveBeenCalledWith(WORKLET_URL);
    expect(context.state).toBe("running"); // resumed
    expect(capture.inputSampleRate).toBe(48_000);
    const worklet = audio.worklet();
    expect(worklet.name).toBe("pcm-capture");
    expect(worklet.options.processorOptions).toEqual({
      targetSampleRate: 16000,
      frameSamples: 1600,
    });
    // Nothing is connected until begin(): no audio flows before the server is ready.
    expect(context.sources[0]?.connect).not.toHaveBeenCalled();

    capture.begin();
    expect(context.sources[0]?.connected).toEqual([worklet]);
    expect(worklet.connected).toEqual([context.gains[0]]);
    expect(context.gains[0]?.gain.value).toBe(0); // zero gain: no feedback to the speakers
    expect(context.gains[0]?.connected).toEqual([context.destination]);
    capture.close();
  });

  it("delivers 16 kHz PCM16 frames from 48 kHz stereo input", async () => {
    const audio = installFakeAudio({ sampleRate: 48_000 });
    const frames: AudioFrame[] = [];
    const capture = await openCapture(frames);
    capture.begin();
    audio.worklet().feed(tone(0.5));
    expect(frames.length).toBeGreaterThanOrEqual(4);
    expect(frames.every((f) => f.pcm.byteLength === 3200)).toBe(true);
    await capture.finish();
    const total = frames.reduce((n, f) => n + f.samples, 0);
    expect(total).toBe(8000); // exactly 0.5 s at 16 kHz, tail included
    expect(frames.at(-1)?.pcm.byteLength).toBeLessThanOrEqual(3200);
  });

  it("supports 44.1 kHz devices", async () => {
    const audio = installFakeAudio({ sampleRate: 44_100 });
    const frames: AudioFrame[] = [];
    const capture = await openCapture(frames);
    capture.begin();
    audio.worklet().feed(tone(1, { sampleRate: 44_100, channels: 1 }));
    await capture.finish();
    expect(frames.reduce((n, f) => n + f.samples, 0)).toBe(16_000);
  });

  it("releases the microphone and closes the AudioContext after finish()", async () => {
    const audio = installFakeAudio();
    const capture = await openCapture();
    capture.begin();
    await capture.finish();
    expect(audio.stream.tracks[0]?.stop).toHaveBeenCalled();
    expect(audio.context().closeSpy).toHaveBeenCalledOnce();
    expect(audio.worklet().port.closed).toBe(true);
    expect(audio.worklet().disconnect).toHaveBeenCalled();
    expect(capture.isClosed).toBe(true);
    capture.close(); // idempotent
    expect(audio.context().closeSpy).toHaveBeenCalledOnce();
  });

  describe("flush", () => {
    afterEach(() => {
      vi.useRealTimers();
    });

    async function capturing(sampleSeconds = 0.25) {
      const audio = installFakeAudio();
      const frames: AudioFrame[] = [];
      const capture = await openCapture(frames);
      capture.begin();
      audio.worklet().feed(tone(sampleSeconds));
      return { audio, frames, capture };
    }

    it("resolves only after the worklet confirms every remaining frame", async () => {
      const { audio, frames, capture } = await capturing();
      const before = frames.length;
      await capture.finish();
      expect(audio.worklet().port.posted).toContainEqual({ type: "flush" });
      expect(frames.length).toBeGreaterThan(before); // the tail arrived before resolving
      expect(frames.reduce((n, f) => n + f.samples, 0)).toBe(4000); // 0.25 s, nothing lost
    });

    it("waits for a slow worklet within the timeout", async () => {
      vi.useFakeTimers();
      const { audio, frames, capture } = await capturing();
      audio.worklet().flushDelayMs = 1_500;
      let settled = false;
      const done = capture.finish(2_000).then(() => (settled = true));
      await vi.advanceTimersByTimeAsync(1_400);
      expect(settled).toBe(false); // still waiting: no early "success"
      expect(audio.context().closeSpy).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(100);
      await done;
      expect(frames.reduce((n, f) => n + f.samples, 0)).toBe(4000);
      expect(audio.context().closeSpy).toHaveBeenCalledOnce();
    });

    it("rejects with flush_timeout when the worklet never confirms, and still releases", async () => {
      vi.useFakeTimers();
      const { audio, frames, capture } = await capturing();
      audio.worklet().respondToFlush = false;
      const done = capture.finish(2_000);
      const outcome = done.then(
        () => "resolved",
        (e: unknown) => e,
      );
      await vi.advanceTimersByTimeAsync(2_000);
      const error = await outcome;
      expect(error).toBeInstanceOf(MicrophoneError);
      expect(error).toMatchObject({ code: "flush_timeout" });
      expect(audio.stream.tracks[0]?.stop).toHaveBeenCalled();
      expect(audio.context().closeSpy).toHaveBeenCalledOnce();
      expect(audio.worklet().port.closed).toBe(true);
      const count = frames.length;
      audio
        .worklet()
        .port.deliver({ type: "frame", pcm: new ArrayBuffer(2), samples: 1, rms: 0, peak: 0 });
      expect(frames).toHaveLength(count); // nothing after the failure
    });

    it("rejects with flush_incomplete when frames went missing in the worklet", async () => {
      const { audio, capture } = await capturing();
      audio.worklet().misreportSamples = 160; // the worklet produced 10 ms we never got
      await expect(capture.finish()).rejects.toMatchObject({ code: "flush_incomplete" });
      expect(audio.context().closeSpy).toHaveBeenCalledOnce();
    });

    it("rejects with aborted if close() runs during the flush", async () => {
      vi.useFakeTimers();
      const { audio, capture } = await capturing();
      audio.worklet().flushDelayMs = 500;
      const done = capture.finish();
      capture.close();
      await expect(done).rejects.toMatchObject({ code: "aborted" });
      await vi.advanceTimersByTimeAsync(500); // the late reply changes nothing
    });

    it("resolves immediately when nothing was captured", async () => {
      const audio = installFakeAudio();
      const capture = await openCapture();
      await expect(capture.finish()).resolves.toBeUndefined(); // open() but never begin()
      expect(audio.context().closeSpy).toHaveBeenCalled();
    });
  });

  it("ignores frames after close()", async () => {
    const audio = installFakeAudio();
    const frames: AudioFrame[] = [];
    const capture = await openCapture(frames);
    capture.begin();
    capture.close();
    audio
      .worklet()
      .port.deliver({ type: "frame", pcm: new ArrayBuffer(3200), samples: 1600, rms: 0, peak: 0 });
    expect(frames).toHaveLength(0);
  });

  it("reports a removed device", async () => {
    const audio = installFakeAudio();
    const lost = vi.fn();
    const capture = await openCapture([], lost);
    capture.begin();
    audio.stream.tracks[0]?.unplug();
    expect(lost).toHaveBeenCalledOnce();
    capture.close();
  });

  it("maps permission denied", async () => {
    installFakeAudio({ getUserMediaError: mediaError("NotAllowedError") });
    const error = await openError();
    expect(error.code).toBe("permission_denied");
    expect(error.message).toMatch(/allow the microphone/i);
    expect(FakeAudioContext.instances).toHaveLength(0); // nothing else was created
  });

  it("maps a missing microphone", async () => {
    installFakeAudio({ getUserMediaError: mediaError("NotFoundError") });
    expect((await openError()).code).toBe("no_microphone");
  });

  it("refuses an insecure page before asking for permission", async () => {
    const audio = installFakeAudio({ secure: false });
    expect((await openError()).code).toBe("insecure_context");
    expect(audio.getUserMedia).not.toHaveBeenCalled();
  });

  it("detects a browser without AudioWorklet", async () => {
    const audio = installFakeAudio();
    vi.stubGlobal("AudioWorkletNode", undefined);
    expect((await openError()).code).toBe("worklet_unsupported");
    expect(audio.getUserMedia).not.toHaveBeenCalled();
  });

  it("detects a browser without getUserMedia", async () => {
    installFakeAudio();
    Object.defineProperty(navigator, "mediaDevices", { value: undefined, configurable: true });
    expect((await openError()).code).toBe("unsupported");
  });

  it("reports an AudioContext failure and releases the microphone", async () => {
    const audio = installFakeAudio();
    FakeAudioContext.failAddModule = true;
    expect((await openError()).code).toBe("audio_context_failed");
    expect(audio.stream.tracks[0]?.stop).toHaveBeenCalled();
    expect(audio.context().closeSpy).toHaveBeenCalled();
  });

  it("releases a stream granted after close() (permission prompt answered late)", async () => {
    const audio = installFakeAudio();
    let grant: (value: FakeStream) => void = () => undefined;
    audio.getUserMedia.mockImplementation(
      () =>
        new Promise((resolve) => {
          grant = resolve;
        }),
    );
    const capture = new MicrophoneCapture({ onFrame: vi.fn() });
    const opening = capture.open();
    capture.close();
    grant(audio.stream);
    await expect(opening).rejects.toMatchObject({ code: "aborted" });
    expect(audio.stream.tracks[0]?.stop).toHaveBeenCalled();
    expect(FakeAudioContext.instances).toHaveLength(0);
  });
});

describe("helpers", () => {
  it("classifies getUserMedia errors", () => {
    expect(classifyGetUserMediaError(mediaError("SecurityError")).code).toBe("permission_denied");
    expect(classifyGetUserMediaError(mediaError("OverconstrainedError")).code).toBe(
      "no_microphone",
    );
    expect(classifyGetUserMediaError(mediaError("NotReadableError")).code).toBe("device_busy");
    expect(classifyGetUserMediaError(new Error("x")).code).toBe("unknown");
  });

  it("maps RMS to a -60..0 dB meter", () => {
    expect(meterLevel(0)).toBe(0);
    expect(meterLevel(1)).toBe(1);
    expect(meterLevel(0.001)).toBe(0); // -60 dB
    expect(meterLevel(0.0316)).toBeCloseTo(0.5, 2); // -30 dB
  });
});
