/** Tests for public/pcm-worklet.js: the exact file the browser loads as the AudioWorklet. */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_FRAME_SAMPLES,
  PcmFramer,
  PcmPipeline,
  Resampler,
  downmixToMono,
  encodePcm16,
  floatToInt16,
} from "../../public/pcm-worklet.js";

function sine(rate: number, hz: number, seconds: number, amp = 0.5): Float32Array {
  const n = Math.round(rate * seconds);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = amp * Math.sin((2 * Math.PI * hz * i) / rate);
  return out;
}

function rms(a: Float32Array, from = 0, to = a.length): number {
  let s = 0;
  for (let i = from; i < to; i++) s += (a[i] ?? 0) ** 2;
  return Math.sqrt(s / (to - from));
}

function concat(parts: Float32Array[]): Float32Array {
  const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0));
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out;
}

/** Resample in render-quantum blocks (128 samples), like the AudioWorklet does. */
function resampleStream(input: Float32Array, from: number, block = 128): Float32Array {
  const r = new Resampler(from, 16_000);
  const parts: Float32Array[] = [];
  for (let i = 0; i < input.length; i += block) parts.push(r.process(input.subarray(i, i + block)));
  parts.push(r.flush());
  return concat(parts);
}

/** Dominant frequency by zero crossings. */
function zeroCrossingHz(a: Float32Array, rate: number): number {
  let crossings = 0;
  for (let i = 1; i < a.length; i++) if ((a[i - 1] ?? 0) < 0 !== (a[i] ?? 0) < 0) crossings++;
  return (crossings / 2) * (rate / a.length);
}

describe("mono conversion", () => {
  it("averages the channels", () => {
    const left = Float32Array.from([1, 0.5, -1]);
    const right = Float32Array.from([0, 0.5, 1]);
    expect([...downmixToMono([left, right])]).toEqual([0.5, 0.5, 0]);
  });

  it("passes a single channel through and handles no input", () => {
    const mono = Float32Array.from([0.1, 0.2]);
    expect(downmixToMono([mono])).toBe(mono);
    expect(downmixToMono([])).toHaveLength(0);
  });
});

describe("PCM16 encoding", () => {
  it("scales, rounds and clamps to the int16 range", () => {
    expect(floatToInt16(0)).toBe(0);
    expect(floatToInt16(1)).toBe(32767);
    expect(floatToInt16(-1)).toBe(-32768);
    expect(floatToInt16(0.5)).toBe(16384);
    expect(floatToInt16(1.7)).toBe(32767); // clamped
    expect(floatToInt16(-3)).toBe(-32768);
    expect(floatToInt16(Number.NaN)).toBe(0);
  });

  it("writes little-endian bytes", () => {
    const bytes = new Uint8Array(encodePcm16([1, -1, 0.5]));
    expect([...bytes]).toEqual([0xff, 0x7f, 0x00, 0x80, 0x00, 0x40]);
  });
});

describe("resampling", () => {
  it.each([48_000, 44_100])("converts %i Hz to 16 kHz with the right length and pitch", (rate) => {
    const out = resampleStream(sine(rate, 440, 1), rate);
    expect(out).toHaveLength(16_000);
    const middle = out.subarray(400, out.length - 400);
    expect(zeroCrossingHz(middle, 16_000)).toBeCloseTo(440, -1);
    expect(rms(middle)).toBeCloseTo(0.5 / Math.SQRT2, 2); // passband gain ~1
  });

  it("removes content above 8 kHz instead of aliasing it", () => {
    const out = resampleStream(sine(48_000, 12_000, 0.5), 48_000);
    expect(rms(out, 200, out.length - 200)).toBeLessThan(0.002); // > 45 dB down
  });

  it("keeps speech-band content up to ~6 kHz", () => {
    const out = resampleStream(sine(48_000, 6_000, 0.5), 48_000);
    expect(rms(out, 200, out.length - 200)).toBeGreaterThan(0.33);
  });

  it("gives identical output however the input is split", () => {
    const input = sine(48_000, 1_000, 0.3);
    const whole = resampleStream(input, 48_000, input.length);
    for (const block of [128, 1, 7, 1000]) {
      const split = resampleStream(input, 48_000, block);
      expect(split.length).toBe(whole.length);
      let maxDiff = 0;
      for (let i = 0; i < whole.length; i++)
        maxDiff = Math.max(maxDiff, Math.abs((split[i] ?? 0) - (whole[i] ?? 0)));
      expect(maxDiff).toBeLessThan(1e-6);
    }
  });

  it("emits ceil(n * 16000 / rate) samples after flush, for any length", () => {
    for (const [rate, n] of [
      [48_000, 1],
      [48_000, 128],
      [48_000, 4_801],
      [44_100, 44_101],
      [44_100, 300],
    ] as const) {
      const out = resampleStream(new Float32Array(n).fill(0.1), rate);
      expect(out.length).toBe(Math.ceil((n * 16_000) / rate));
    }
  });

  it("is a pass-through at 16 kHz", () => {
    const input = sine(16_000, 300, 0.1);
    const r = new Resampler(16_000, 16_000);
    expect([...r.process(input)]).toEqual([...input]);
    expect(r.flush()).toHaveLength(0);
  });

  it("refuses input after flush and invalid rates", () => {
    const r = new Resampler(48_000, 16_000);
    r.flush();
    expect(() => r.process(new Float32Array(1))).toThrow();
    expect(() => new Resampler(0, 16_000)).toThrow(RangeError);
  });
});

describe("framing", () => {
  it("produces 100 ms frames of exactly 3200 bytes and keeps the remainder", () => {
    const framer = new PcmFramer();
    expect(DEFAULT_FRAME_SAMPLES).toBe(1600);
    const frames = framer.push(new Float32Array(1600 * 2 + 500).fill(0.25));
    expect(frames.map((f) => f.pcm.byteLength)).toEqual([3200, 3200]);
    const last = framer.flush();
    expect(last?.pcm.byteLength).toBe(1000);
    expect(last?.samples).toBe(500);
    expect(framer.flush()).toBeNull();
  });

  it("never loses samples across frame boundaries", () => {
    const framer = new PcmFramer(1600);
    const input = Float32Array.from({ length: 5000 }, (_, i) => ((i % 200) - 100) / 200);
    const frames = [];
    for (let i = 0; i < input.length; i += 333)
      frames.push(...framer.push(input.subarray(i, i + 333)));
    const tail = framer.flush();
    if (tail) frames.push(tail);
    expect(frames.every((f) => f.pcm.byteLength <= 3200)).toBe(true);
    const decoded = frames.flatMap((f) => [...new Int16Array(f.pcm)]);
    expect(decoded).toEqual([...input].map(floatToInt16));
  });

  it("reports frame level", () => {
    const framer = new PcmFramer(4);
    const [frame] = framer.push([0.5, -0.5, 0.5, -0.5]);
    expect(frame?.rms).toBeCloseTo(0.5, 3);
    expect(frame?.peak).toBeCloseTo(0.5, 3);
  });
});

describe("pipeline (stereo 48 kHz microphone -> 16 kHz mono PCM16 frames)", () => {
  it("delivers all audio in <= 3200-byte frames including the tail", () => {
    const pipeline = new PcmPipeline(48_000);
    const left = sine(48_000, 300, 1.05);
    const right = sine(48_000, 300, 1.05, 0.1);
    const frames = [];
    for (let i = 0; i < left.length; i += 128) {
      frames.push(...pipeline.process([left.subarray(i, i + 128), right.subarray(i, i + 128)]));
    }
    expect(frames.every((f) => f.pcm.byteLength === 3200)).toBe(true);
    frames.push(...pipeline.flush());
    const total = frames.reduce((n, f) => n + f.samples, 0);
    expect(total).toBe(16_800); // 1.05 s
    expect(pipeline.totalSamples).toBe(total);
    expect(frames.at(-1)?.pcm.byteLength).toBe(16_800 * 2 - 10 * 3200);
  });
});

describe("AudioWorklet registration", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("registers 'pcm-capture', posts frames and flushes on request", async () => {
    const registered: Record<string, new (options: unknown) => unknown> = {};
    class Port {
      onmessage: ((e: { data: unknown }) => void) | null = null;
      messages: unknown[] = [];
      postMessage(m: unknown) {
        this.messages.push(m);
      }
    }
    vi.stubGlobal(
      "AudioWorkletProcessor",
      class {
        port = new Port();
      },
    );
    vi.stubGlobal("sampleRate", 48_000);
    vi.stubGlobal("registerProcessor", (name: string, ctor: new (options: unknown) => unknown) => {
      registered[name] = ctor;
    });
    vi.resetModules(); // evaluate the module again, now inside the fake worklet scope
    await import("../../public/pcm-worklet.js");

    const Processor = registered["pcm-capture"];
    expect(Processor).toBeDefined();
    if (!Processor) return;
    const node = new Processor({ processorOptions: { frameSamples: 1600 } }) as {
      port: Port;
      process: (inputs: Float32Array[][]) => boolean;
    };
    const block = sine(48_000, 440, 128 / 48_000);
    for (let i = 0; i < 40; i++) expect(node.process([[block]])).toBe(true); // 5120 in -> 1706 out
    expect(node.process([[]])).toBe(true); // no channels (input disconnected) is fine
    const frames = node.port.messages as { type: string; pcm: ArrayBuffer }[];
    expect(frames.map((m) => [m.type, m.pcm.byteLength])).toEqual([["frame", 3200]]);

    node.port.onmessage?.({ data: { type: "flush" } });
    const after = node.port.messages as {
      type: string;
      pcm?: ArrayBuffer;
      totalSamples?: number;
    }[];
    expect(after.at(-1)).toEqual({ type: "flushed", totalSamples: Math.ceil((40 * 128) / 3) });
    expect(after.at(-2)?.pcm?.byteLength).toBe((Math.ceil((40 * 128) / 3) - 1600) * 2);
    expect(node.process([[block]])).toBe(false); // done: lets the node be collected
  });
});
