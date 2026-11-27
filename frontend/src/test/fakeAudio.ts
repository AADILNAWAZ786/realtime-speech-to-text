/**
 * Browser audio doubles for tests: getUserMedia, AudioContext and AudioWorkletNode.
 * The fake worklet node runs the REAL processing code from public/pcm-worklet.js, so
 * tests exercise the production downmix / resample / PCM16 / framing path.
 */
import { vi, type Mock } from "vitest";

import { PcmPipeline } from "../../public/pcm-worklet.js";

export class FakeTrack extends EventTarget {
  readonly kind = "audio";
  readyState: "live" | "ended" = "live";
  stop = vi.fn(() => {
    this.readyState = "ended";
  });
  /** Simulate the device being unplugged. */
  unplug(): void {
    this.readyState = "ended";
    this.dispatchEvent(new Event("ended"));
  }
}

export class FakeStream {
  constructor(readonly tracks: FakeTrack[] = [new FakeTrack()]) {}
  getAudioTracks() {
    return this.tracks;
  }
  getTracks() {
    return this.tracks;
  }
}

class FakeNode {
  connected: unknown[] = [];
  connect = vi.fn((target: unknown) => {
    this.connected.push(target);
    return target;
  });
  disconnect = vi.fn(() => {
    this.connected = [];
  });
}

export class FakeGainNode extends FakeNode {
  gain = { value: 1 };
}

export class FakeWorkletPort {
  onmessage: ((event: MessageEvent) => void) | null = null;
  closed = false;
  posted: unknown[] = [];
  constructor(private readonly node: FakeAudioWorkletNode) {}
  postMessage(message: unknown): void {
    this.posted.push(message);
    if ((message as { type?: string }).type === "flush") this.node.flush();
  }
  close(): void {
    this.closed = true;
  }
  deliver(data: unknown): void {
    if (this.closed) return; // a closed MessagePort delivers nothing
    this.onmessage?.({ data } as MessageEvent);
  }
}

export class FakeAudioWorkletNode extends FakeNode {
  static instances: FakeAudioWorkletNode[] = [];
  readonly port = new FakeWorkletPort(this);
  readonly pipeline: PcmPipeline;
  /** When false, "flush" gets no "flushed" reply (simulates a stuck worklet). */
  respondToFlush = true;
  /** Answer "flush" after this many ms (setTimeout) instead of immediately. */
  flushDelayMs: number | null = null;
  /** Report a different totalSamples in "flushed" (simulates frames lost in the worklet). */
  misreportSamples = 0;

  constructor(
    readonly context: FakeAudioContext,
    readonly name: string,
    readonly options: AudioWorkletNodeOptions,
  ) {
    super();
    const processorOptions = options.processorOptions as
      { targetSampleRate?: number; frameSamples?: number } | undefined;
    this.pipeline = new PcmPipeline(context.sampleRate, processorOptions ?? {});
    FakeAudioWorkletNode.instances.push(this);
  }

  /** Push microphone audio (one Float32Array per channel) through the real worklet code. */
  feed(channels: Float32Array[]): void {
    // Like the real graph: no input reaches the worklet until a source is connected to it.
    if (!this.context.sources.some((source) => source.connected.includes(this))) return;
    const length = channels[0]?.length ?? 0;
    for (let offset = 0; offset < length; offset += 128) {
      const block = channels.map((c) => c.subarray(offset, offset + 128));
      for (const frame of this.pipeline.process(block))
        this.port.deliver({ type: "frame", ...frame });
    }
  }

  flush(): void {
    if (!this.respondToFlush) return;
    const reply = () => {
      for (const frame of this.pipeline.flush()) this.port.deliver({ type: "frame", ...frame });
      this.port.deliver({
        type: "flushed",
        totalSamples: this.pipeline.totalSamples + this.misreportSamples,
      });
    };
    if (this.flushDelayMs === null) reply();
    else setTimeout(reply, this.flushDelayMs);
  }
}

export class FakeAudioContext {
  static instances: FakeAudioContext[] = [];
  static sampleRateForNext = 48_000;
  static failAddModule = false;
  readonly sampleRate = FakeAudioContext.sampleRateForNext;
  state: AudioContextState = "suspended";
  readonly destination = new FakeNode();
  readonly addModule = vi.fn((url: string) =>
    FakeAudioContext.failAddModule
      ? Promise.reject(new Error(`cannot load ${url}`))
      : Promise.resolve(),
  );
  readonly closeSpy = vi.fn();
  readonly sources: FakeNode[] = [];
  readonly gains: FakeGainNode[] = [];

  constructor(readonly options?: AudioContextOptions) {
    FakeAudioContext.instances.push(this);
  }

  get audioWorklet() {
    return { addModule: this.addModule };
  }

  resume(): Promise<void> {
    this.state = "running";
    return Promise.resolve();
  }

  close(): Promise<void> {
    this.state = "closed";
    this.closeSpy();
    return Promise.resolve();
  }

  createMediaStreamSource(stream: unknown) {
    const node = Object.assign(new FakeNode(), { stream });
    this.sources.push(node);
    return node;
  }

  createGain() {
    const node = new FakeGainNode();
    this.gains.push(node);
    return node;
  }
}

export interface FakeAudio {
  getUserMedia: Mock<() => Promise<FakeStream>>;
  stream: FakeStream;
  context: () => FakeAudioContext;
  worklet: () => FakeAudioWorkletNode;
}

/**
 * Install the fakes as browser globals (window.isSecureContext, navigator.mediaDevices,
 * AudioContext, AudioWorkletNode). Undo with uninstallFakeAudio().
 */
export function installFakeAudio({
  sampleRate = 48_000,
  getUserMediaError,
  secure = true,
}: { sampleRate?: number; getUserMediaError?: Error; secure?: boolean } = {}): FakeAudio {
  FakeAudioContext.instances = [];
  FakeAudioWorkletNode.instances = [];
  FakeAudioContext.sampleRateForNext = sampleRate;
  FakeAudioContext.failAddModule = false;
  const stream = new FakeStream();
  const getUserMedia = vi.fn<() => Promise<FakeStream>>(() =>
    getUserMediaError === undefined ? Promise.resolve(stream) : Promise.reject(getUserMediaError),
  );
  Object.defineProperty(window, "isSecureContext", { value: secure, configurable: true });
  Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("AudioWorkletNode", FakeAudioWorkletNode);
  return {
    getUserMedia,
    stream,
    context: () => {
      const context = FakeAudioContext.instances.at(-1);
      if (!context) throw new Error("no AudioContext was created");
      return context;
    },
    worklet: () => {
      const node = FakeAudioWorkletNode.instances.at(-1);
      if (!node) throw new Error("no AudioWorkletNode was created");
      return node;
    },
  };
}

export function uninstallFakeAudio(): void {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "mediaDevices");
  Reflect.deleteProperty(window, "isSecureContext");
}

/** A DOMException-like error as thrown by getUserMedia. */
export function mediaError(name: string): Error {
  const error = new Error(name);
  error.name = name;
  return error;
}

/** `seconds` of a sine tone, one array per channel (channel 2 is a quieter copy). */
export function tone(
  seconds: number,
  {
    sampleRate = 48_000,
    hz = 440,
    channels = 2,
  }: { sampleRate?: number; hz?: number; channels?: number } = {},
): Float32Array[] {
  const n = Math.round(seconds * sampleRate);
  return Array.from({ length: channels }, (_, c) => {
    const data = new Float32Array(n);
    for (let i = 0; i < n; i++)
      data[i] = (c === 0 ? 0.5 : 0.25) * Math.sin((2 * Math.PI * hz * i) / sampleRate);
    return data;
  });
}
