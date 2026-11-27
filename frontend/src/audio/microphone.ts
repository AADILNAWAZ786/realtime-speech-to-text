/**
 * Microphone capture: getUserMedia -> AudioContext -> AudioWorklet ("pcm-capture").
 * The worklet (public/pcm-worklet.js) does all audio processing off the UI thread and
 * posts 16 kHz mono PCM16 frames. Framework-free; React binds it in hooks/useMicrophone.
 *
 *   MediaStreamSource ──► AudioWorkletNode("pcm-capture") ──► Gain(0) ──► destination
 *
 * The zero-gain branch keeps the worklet pulled by the audio graph without ever playing
 * the microphone back through the speakers (no feedback).
 */

export const WORKLET_URL = "/pcm-worklet.js";
export const WORKLET_NAME = "pcm-capture";
export const TARGET_SAMPLE_RATE = 16_000;
export const FRAME_SAMPLES = 1_600; // 100 ms -> 3200 bytes
/** How long finish() waits for the worklet to confirm it has delivered its last frames. */
export const FLUSH_TIMEOUT_MS = 2_000;

export type MicrophoneErrorCode =
  | "insecure_context"
  | "unsupported"
  | "worklet_unsupported"
  | "permission_denied"
  | "no_microphone"
  | "device_busy"
  | "audio_context_failed"
  | "device_lost"
  | "flush_timeout"
  | "flush_incomplete"
  | "aborted"
  | "unknown";

const MESSAGES: Record<MicrophoneErrorCode, string> = {
  insecure_context:
    "Microphone access needs a secure page. Open the app over HTTPS (or on localhost).",
  unsupported:
    "This browser cannot record audio. Use a current version of Chrome, Edge, Firefox or Safari.",
  worklet_unsupported:
    "This browser does not support AudioWorklet, which is needed for live audio. Use a current browser.",
  permission_denied:
    "Microphone access was blocked. Allow the microphone for this site in your browser settings and try again.",
  no_microphone: "No microphone was found. Connect a microphone and try again.",
  device_busy: "The microphone is in use by another application or could not be started.",
  audio_context_failed: "The audio system could not be started. Reload the page and try again.",
  device_lost: "The microphone was disconnected.",
  flush_timeout:
    "The audio processor did not return the last part of the recording in time, so it could not be sent.",
  flush_incomplete:
    "The audio processor lost part of the last audio, so the recording could not be finished normally.",
  aborted: "Recording was cancelled.",
  unknown: "The microphone could not be started.",
};

export class MicrophoneError extends Error {
  override name = "MicrophoneError";
  constructor(
    readonly code: MicrophoneErrorCode,
    message: string = MESSAGES[code],
  ) {
    super(message);
  }
}

/** Map a getUserMedia DOMException to our error codes. */
export function classifyGetUserMediaError(error: unknown): MicrophoneError {
  const name = (error as { name?: unknown } | null)?.name;
  switch (name) {
    case "NotAllowedError":
    case "PermissionDeniedError":
    case "SecurityError":
      return new MicrophoneError("permission_denied");
    case "NotFoundError":
    case "DevicesNotFoundError":
    case "OverconstrainedError":
      return new MicrophoneError("no_microphone");
    case "NotReadableError":
    case "TrackStartError":
    case "AbortError":
      return new MicrophoneError("device_busy");
    default:
      return new MicrophoneError("unknown");
  }
}

export interface AudioFrame {
  /** PCM16 little-endian, 16 kHz, mono. 3200 bytes except possibly the last frame. */
  pcm: ArrayBuffer;
  samples: number;
  rms: number;
  peak: number;
}

export interface CaptureCallbacks {
  onFrame: (frame: AudioFrame) => void;
  /** The microphone stopped on its own (unplugged, revoked permission). */
  onDeviceLost?: () => void;
}

/** Browser APIs, injectable for tests. */
export interface AudioEnvironment {
  isSecureContext: boolean;
  mediaDevices: Pick<MediaDevices, "getUserMedia"> | undefined;
  createAudioContext: () => AudioContext;
  createWorkletNode: (
    context: AudioContext,
    name: string,
    options: AudioWorkletNodeOptions,
  ) => AudioWorkletNode;
  hasAudioWorklet: boolean;
}

export function browserAudioEnvironment(): AudioEnvironment {
  const AudioContextCtor =
    typeof window === "undefined"
      ? undefined
      : (window.AudioContext as typeof AudioContext | undefined);
  return {
    isSecureContext: typeof window !== "undefined" && window.isSecureContext,
    mediaDevices: typeof navigator === "undefined" ? undefined : navigator.mediaDevices,
    createAudioContext: () => {
      if (!AudioContextCtor) throw new MicrophoneError("unsupported");
      return new AudioContextCtor({ latencyHint: "interactive" });
    },
    createWorkletNode: (context, name, options) => new AudioWorkletNode(context, name, options),
    hasAudioWorklet:
      typeof AudioWorkletNode !== "undefined" &&
      AudioContextCtor !== undefined &&
      "audioWorklet" in AudioContextCtor.prototype,
  };
}

const CONSTRAINTS: MediaStreamConstraints = {
  // No sampleRate/channelCount constraints: the browser picks its native format and the
  // worklet downmixes and resamples, which works the same on every device.
  audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  video: false,
};

type State = "idle" | "opening" | "open" | "capturing" | "flushing" | "closed";

/**
 * One capture session. open() asks for permission and builds the audio graph (silent);
 * begin() starts delivering frames; finish() flushes the last samples; close() releases
 * everything. Each instance is single-use.
 */
export class MicrophoneCapture {
  private state: State = "idle";
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private worklet: AudioWorkletNode | null = null;
  private sink: GainNode | null = null;
  /** The pending finish(): settled by "flushed", a timeout, or close(). */
  private flush: { resolve: () => void; reject: (error: MicrophoneError) => void } | null = null;
  /** Samples delivered through onFrame, checked against the worklet's own count on flush. */
  private deliveredSamples = 0;
  private trackEnded = (): void => {
    if (this.state === "closed") return;
    this.callbacks.onDeviceLost?.();
  };

  constructor(
    private readonly callbacks: CaptureCallbacks,
    private readonly env: AudioEnvironment = browserAudioEnvironment(),
  ) {}

  /** The AudioContext rate (e.g. 48000), available after open(). */
  get inputSampleRate(): number | null {
    return this.context?.sampleRate ?? null;
  }

  get isClosed(): boolean {
    return this.state === "closed";
  }

  /** close() may run while open() awaits the permission prompt or the worklet module. */
  private wasClosed(): boolean {
    return this.isClosed;
  }

  async open(): Promise<void> {
    if (this.state !== "idle") throw new Error("MicrophoneCapture.open() called twice");
    this.state = "opening";
    try {
      const { env } = this;
      if (!env.isSecureContext) throw new MicrophoneError("insecure_context");
      if (!env.mediaDevices?.getUserMedia) throw new MicrophoneError("unsupported");
      if (!env.hasAudioWorklet) throw new MicrophoneError("worklet_unsupported");

      try {
        this.stream = await env.mediaDevices.getUserMedia(CONSTRAINTS);
      } catch (error) {
        throw classifyGetUserMediaError(error);
      }
      if (this.wasClosed()) {
        // close() ran while the permission prompt was open: release what we just got.
        for (const track of this.stream.getTracks()) track.stop();
        this.stream = null;
        throw new MicrophoneError("aborted");
      }
      const tracks = this.stream.getAudioTracks();
      if (tracks.length === 0) throw new MicrophoneError("no_microphone");
      for (const track of tracks) track.addEventListener("ended", this.trackEnded);

      try {
        const context = env.createAudioContext();
        this.context = context;
        await context.audioWorklet.addModule(WORKLET_URL);
        if (this.wasClosed()) throw new MicrophoneError("aborted");
        if (context.state === "suspended") await context.resume();
        this.source = context.createMediaStreamSource(this.stream);
        this.worklet = env.createWorkletNode(context, WORKLET_NAME, {
          numberOfInputs: 1,
          numberOfOutputs: 1,
          channelCountMode: "max", // keep every mic channel; the worklet downmixes
          processorOptions: { targetSampleRate: TARGET_SAMPLE_RATE, frameSamples: FRAME_SAMPLES },
        });
        this.sink = context.createGain();
        this.sink.gain.value = 0;
      } catch (error) {
        throw error instanceof MicrophoneError
          ? error
          : new MicrophoneError("audio_context_failed");
      }
      this.worklet.port.onmessage = (event: MessageEvent<WorkletMessage>) =>
        this.onMessage(event.data);
      this.state = "open";
    } catch (error) {
      this.close();
      throw error;
    }
  }

  /** Start delivering frames (connects the microphone to the worklet). */
  begin(): void {
    if (this.state !== "open" || !this.source || !this.worklet || !this.sink || !this.context) {
      throw new Error("MicrophoneCapture.begin() before open()");
    }
    this.source.connect(this.worklet);
    this.worklet.connect(this.sink);
    this.sink.connect(this.context.destination);
    this.state = "capturing";
  }

  /**
   * Stop taking new audio and deliver everything still inside the worklet (resampler tail
   * and the last partial frame) through onFrame.
   *
   * Resolves only when the worklet confirms the flush AND its sample count matches what was
   * delivered, so a resolved finish() means every captured sample reached onFrame.
   * Otherwise it rejects with MicrophoneError "flush_timeout" (no confirmation within
   * `timeoutMs`), "flush_incomplete" (count mismatch) or "aborted" (close() meanwhile).
   * The microphone and AudioContext are released in every case.
   */
  finish(timeoutMs = FLUSH_TIMEOUT_MS): Promise<void> {
    if (this.state !== "capturing" || !this.worklet || !this.source) {
      // Nothing was captured (or it is already closed): nothing to flush.
      this.close();
      return Promise.resolve();
    }
    this.state = "flushing";
    // Stop new audio first, then ask the worklet for its tail.
    this.source.disconnect();
    const worklet = this.worklet;
    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.settleFlush(new MicrophoneError("flush_timeout"));
      }, timeoutMs);
      this.flush = {
        resolve: () => {
          clearTimeout(timer);
          resolve();
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      };
      worklet.port.postMessage({ type: "flush" });
    }).finally(() => this.close());
  }

  private settleFlush(error: MicrophoneError | null): void {
    const pending = this.flush;
    if (!pending) return;
    this.flush = null;
    if (error) pending.reject(error);
    else pending.resolve();
  }

  /** Release the microphone and the audio graph immediately. Idempotent. */
  close(): void {
    if (this.state === "closed") return;
    this.state = "closed";
    this.settleFlush(new MicrophoneError("aborted")); // only if a finish() is still pending
    for (const track of this.stream?.getTracks() ?? []) {
      track.removeEventListener("ended", this.trackEnded);
      track.stop();
    }
    if (this.worklet) {
      this.worklet.port.onmessage = null;
      this.worklet.port.close();
    }
    for (const node of [this.source, this.worklet, this.sink]) {
      try {
        node?.disconnect();
      } catch {
        // already disconnected
      }
    }
    if (this.context && this.context.state !== "closed") {
      this.context.close().catch(() => undefined);
    }
    this.stream = null;
    this.source = null;
    this.worklet = null;
    this.sink = null;
    this.context = null;
  }

  private onMessage(message: WorkletMessage): void {
    if (message.type === "frame") {
      if (this.state === "capturing" || this.state === "flushing") {
        this.deliveredSamples += message.samples;
        this.callbacks.onFrame({
          pcm: message.pcm,
          samples: message.samples,
          rms: message.rms,
          peak: message.peak,
        });
      }
    } else if (this.state === "flushing") {
      // Ports are ordered: every frame posted before "flushed" has been handled already.
      // The worklet only receives audio after begin(), so its total must equal ours.
      this.settleFlush(
        message.totalSamples === this.deliveredSamples
          ? null
          : new MicrophoneError("flush_incomplete"),
      );
    }
  }
}

type WorkletMessage =
  | { type: "frame"; pcm: ArrayBuffer; samples: number; rms: number; peak: number }
  | { type: "flushed"; totalSamples: number };

/** RMS (0..1) -> meter level (0..1) on a -60..0 dBFS scale. */
export function meterLevel(rms: number): number {
  if (!(rms > 0)) return 0;
  const db = 20 * Math.log10(rms);
  return Math.max(0, Math.min(1, (db + 60) / 60));
}
