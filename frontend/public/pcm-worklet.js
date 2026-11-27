// @ts-check
/**
 * AudioWorklet: microphone audio -> 16 kHz mono PCM16 little-endian frames.
 *
 * Runs on the audio rendering thread, never on the UI thread. For each 128-sample render
 * quantum it:
 *   1. downmixes all input channels to mono,
 *   2. resamples from the context rate (44.1 kHz, 48 kHz, ...) to 16 kHz with a
 *      windowed-sinc low-pass resampler whose state carries across calls,
 *   3. clamps to [-1, 1] and encodes signed 16-bit little-endian,
 *   4. cuts the stream into fixed frames (default 1600 samples = 100 ms = 3200 bytes),
 *   5. posts each frame (transferred ArrayBuffer) plus its RMS/peak level to the main thread.
 *
 * On {type: "flush"} it drains the resampler, posts the final partial frame (if any) and
 * then {type: "flushed"}; after that process() returns false so the node can be collected.
 *
 * The DSP pieces are exported so the test suite runs this exact file (AudioWorklet
 * modules are ES modules; the exports are simply unused by the browser).
 *
 * Messages to the main thread:
 *   {type: "frame", pcm: ArrayBuffer, samples: number, rms: number, peak: number}
 *   {type: "flushed", totalSamples: number}
 */

export const TARGET_SAMPLE_RATE = 16000;
export const DEFAULT_FRAME_SAMPLES = 1600; // 100 ms at 16 kHz -> 3200 bytes

/**
 * Average all channels into one. A single channel is returned as is (no copy).
 * @param {ReadonlyArray<Float32Array>} channels
 * @returns {Float32Array}
 */
export function downmixToMono(channels) {
  const first = channels[0];
  if (!first) return new Float32Array(0);
  if (channels.length === 1) return first;
  const out = new Float32Array(first.length);
  const scale = 1 / channels.length;
  for (let i = 0; i < out.length; i++) {
    let sum = 0;
    for (const channel of channels) sum += channel[i] ?? 0;
    out[i] = sum * scale;
  }
  return out;
}

/**
 * Float sample -> signed 16-bit integer, clamped to the valid range.
 * @param {number} sample
 */
export function floatToInt16(sample) {
  const s = Number.isNaN(sample) ? 0 : Math.max(-1, Math.min(1, sample));
  return Math.round(s < 0 ? s * 0x8000 : s * 0x7fff);
}

/**
 * Encode float samples as PCM16 little-endian bytes.
 * @param {ArrayLike<number>} samples
 * @returns {ArrayBuffer}
 */
export function encodePcm16(samples) {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < samples.length; i++)
    view.setInt16(i * 2, floatToInt16(samples[i] ?? 0), true);
  return buffer;
}

/**
 * Streaming windowed-sinc resampler (Blackman window), mono.
 *
 * The low-pass cutoff sits just below the output Nyquist frequency, so content above 8 kHz
 * is removed before decimating instead of aliasing into the speech band. All state (the
 * input history and the output clock) is kept between calls, so the result does not depend
 * on how the input is split into blocks.
 */
export class Resampler {
  /**
   * @param {number} inputRate
   * @param {number} outputRate
   * @param {{zeroCrossings?: number, cutoff?: number, tableResolution?: number}} [options]
   */
  constructor(inputRate, outputRate, options = {}) {
    if (!(inputRate > 0) || !(outputRate > 0))
      throw new RangeError("Sample rates must be positive");
    this.inputRate = inputRate;
    this.outputRate = outputRate;
    this.step = inputRate / outputRate; // input samples per output sample
    this.passthrough = inputRate === outputRate;

    const zeroCrossings = options.zeroCrossings ?? 16;
    // Cutoff relative to the input Nyquist frequency, with a small transition band.
    this.fc = (options.cutoff ?? 0.92) * Math.min(1, outputRate / inputRate);
    this.halfWidth = Math.ceil(zeroCrossings / this.fc); // kernel half-width, input samples
    this.resolution = options.tableResolution ?? 512; // table points per input sample

    // Kernel table over x in [0, halfWidth]: h(x) = fc * sinc(fc * x) * blackman(x).
    const size = this.halfWidth * this.resolution + 2;
    this.table = new Float32Array(size);
    for (let i = 0; i < size; i++) {
      const x = i / this.resolution;
      if (x > this.halfWidth) break;
      const t = Math.PI * this.fc * x;
      const sinc = x === 0 ? 1 : Math.sin(t) / t;
      const w =
        0.42 +
        0.5 * Math.cos((Math.PI * x) / this.halfWidth) +
        0.08 * Math.cos((2 * Math.PI * x) / this.halfWidth);
      this.table[i] = this.fc * sinc * w;
    }

    // Input history; index 0 holds absolute input sample `bufferStart`. It starts with
    // halfWidth zeros so the first output (at input time 0) has a full left context.
    this.buffer = new Float32Array(Math.max(4096, this.halfWidth * 4));
    this.length = this.halfWidth;
    this.bufferStart = -this.halfWidth;
    this.totalIn = 0; // real input samples received
    this.outIndex = 0; // index of the next output sample
    this.flushed = false;
  }

  /** @param {number} x distance in input samples */
  kernel(x) {
    const ax = Math.abs(x) * this.resolution;
    const i = Math.floor(ax);
    if (i >= this.table.length - 1) return 0;
    const frac = ax - i;
    const a = this.table[i] ?? 0;
    const b = this.table[i + 1] ?? 0;
    return a + (b - a) * frac;
  }

  /** @param {Float32Array} input */
  append(input) {
    if (this.length + input.length > this.buffer.length) {
      const next = new Float32Array(Math.max(this.buffer.length * 2, this.length + input.length));
      next.set(this.buffer.subarray(0, this.length));
      this.buffer = next;
    }
    this.buffer.set(input, this.length);
    this.length += input.length;
  }

  /**
   * Produce every output sample whose kernel window is fully available.
   * @param {number} limitAbs produce outputs at input times t with t + halfWidth < limitAbs
   * @param {number} [maxTime] and t < maxTime (used by flush)
   * @returns {Float32Array}
   */
  produce(limitAbs, maxTime = Infinity) {
    const out = [];
    for (;;) {
      const t = this.outIndex * this.step; // absolute input time of this output
      if (t + this.halfWidth >= limitAbs || t >= maxTime) break;
      const center = t - this.bufferStart;
      const first = Math.max(0, Math.ceil(center - this.halfWidth));
      const last = Math.min(this.length - 1, Math.floor(center + this.halfWidth));
      let acc = 0;
      for (let k = first; k <= last; k++) acc += (this.buffer[k] ?? 0) * this.kernel(center - k);
      out.push(acc);
      this.outIndex++;
    }
    // Drop history that no future output can reach.
    const nextCenter = this.outIndex * this.step - this.bufferStart;
    const drop = Math.max(0, Math.floor(nextCenter - this.halfWidth) - 1);
    if (drop > 0) {
      this.buffer.copyWithin(0, drop, this.length);
      this.length -= drop;
      this.bufferStart += drop;
    }
    return Float32Array.from(out);
  }

  /**
   * Feed input samples; returns the output samples that are ready.
   * @param {Float32Array} input
   * @returns {Float32Array}
   */
  process(input) {
    if (this.flushed) throw new Error("Resampler already flushed");
    this.totalIn += input.length;
    if (this.passthrough) return Float32Array.from(input);
    this.append(input);
    return this.produce(this.bufferStart + this.length);
  }

  /**
   * End of stream: emit the remaining outputs (right edge padded with silence). After a
   * flush, the total output count is ceil(totalIn * outputRate / inputRate).
   * @returns {Float32Array}
   */
  flush() {
    if (this.flushed) return new Float32Array(0);
    this.flushed = true;
    if (this.passthrough) return new Float32Array(0);
    this.append(new Float32Array(this.halfWidth + 2));
    return this.produce(Infinity, this.totalIn);
  }
}

/**
 * Collects PCM16 samples into fixed-size frames. Frames never exceed `frameSamples`;
 * nothing is dropped at frame boundaries; flush() returns the final partial frame.
 */
export class PcmFramer {
  /** @param {number} [frameSamples] */
  constructor(frameSamples = DEFAULT_FRAME_SAMPLES) {
    if (!Number.isInteger(frameSamples) || frameSamples <= 0)
      throw new RangeError("frameSamples must be a positive integer");
    this.frameSamples = frameSamples;
    this.frame = new DataView(new ArrayBuffer(frameSamples * 2));
    this.filled = 0;
    this.sumSquares = 0;
    this.peak = 0;
  }

  /**
   * @param {ArrayLike<number>} samples float samples at the target rate
   * @returns {Array<{pcm: ArrayBuffer, samples: number, rms: number, peak: number}>}
   */
  push(samples) {
    const frames = [];
    for (let i = 0; i < samples.length; i++) {
      const value = floatToInt16(samples[i] ?? 0);
      this.frame.setInt16(this.filled * 2, value, true);
      const f = value / 0x8000;
      this.sumSquares += f * f;
      this.peak = Math.max(this.peak, Math.abs(f));
      this.filled++;
      if (this.filled === this.frameSamples) frames.push(this.take());
    }
    return frames;
  }

  /** The remaining partial frame, or null when empty. */
  flush() {
    return this.filled > 0 ? this.take() : null;
  }

  take() {
    const samples = this.filled;
    const pcm = this.frame.buffer.slice(0, samples * 2);
    const result = { pcm, samples, rms: Math.sqrt(this.sumSquares / samples), peak: this.peak };
    this.filled = 0;
    this.sumSquares = 0;
    this.peak = 0;
    return result;
  }
}

/**
 * Whole pipeline for one stream: channels in -> PCM16 frames out.
 */
/** @typedef {{pcm: ArrayBuffer, samples: number, rms: number, peak: number}} PcmFrame */

export class PcmPipeline {
  /**
   * @param {number} inputRate
   * @param {{targetSampleRate?: number, frameSamples?: number}} [options]
   */
  constructor(inputRate, options = {}) {
    this.resampler = new Resampler(inputRate, options.targetSampleRate ?? TARGET_SAMPLE_RATE);
    this.framer = new PcmFramer(options.frameSamples ?? DEFAULT_FRAME_SAMPLES);
    this.totalSamples = 0; // output samples emitted in frames
  }

  /** @param {ReadonlyArray<Float32Array>} channels */
  process(channels) {
    return this.count(this.framer.push(this.resampler.process(downmixToMono(channels))));
  }

  flush() {
    const frames = this.framer.push(this.resampler.flush());
    const last = this.framer.flush();
    if (last) frames.push(last);
    return this.count(frames);
  }

  /**
   * @param {PcmFrame[]} frames
   * @returns {PcmFrame[]}
   */
  count(frames) {
    for (const frame of frames) this.totalSamples += frame.samples;
    return frames;
  }
}

// ----- registration (only inside an AudioWorkletGlobalScope)

/** @type {any} */
const scope = globalThis;

if (
  typeof scope.registerProcessor === "function" &&
  typeof scope.AudioWorkletProcessor === "function"
) {
  class PcmCaptureProcessor extends scope.AudioWorkletProcessor {
    /** @param {{processorOptions?: {targetSampleRate?: number, frameSamples?: number}}} options */
    constructor(options) {
      super();
      /** @type {number} */
      const rate = scope.sampleRate; // the AudioContext rate, e.g. 48000
      this.pipeline = new PcmPipeline(rate, options.processorOptions ?? {});
      this.done = false;
      /** @param {MessageEvent} event */
      this.port.onmessage = (event) => {
        if (event.data?.type === "flush" && !this.done) {
          this.done = true;
          this.post(this.pipeline.flush());
          this.port.postMessage({ type: "flushed", totalSamples: this.pipeline.totalSamples });
        }
      };
    }

    /** @param {Array<{pcm: ArrayBuffer, samples: number, rms: number, peak: number}>} frames */
    post(frames) {
      for (const frame of frames) this.port.postMessage({ type: "frame", ...frame }, [frame.pcm]);
    }

    /** @param {Float32Array[][]} inputs */
    process(inputs) {
      if (this.done) return false;
      const input = inputs[0];
      if (input && input.length > 0) this.post(this.pipeline.process(input));
      return true;
    }
  }
  scope.registerProcessor("pcm-capture", PcmCaptureProcessor);
}
