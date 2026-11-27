# Browser audio processing

How the web client turns microphone input into the stream that `/ws/transcribe` expects:
**PCM16 little-endian, 16 kHz, mono** (`docs/websocket-protocol.md`).

Code:

| File | Role |
| --- | --- |
| `frontend/public/pcm-worklet.js` | AudioWorklet module: downmix, resample, encode, frame |
| `frontend/src/audio/microphone.ts` | `MicrophoneCapture`: permission, audio graph, flush, cleanup, errors |
| `frontend/src/hooks/useMicrophone.ts` | React binding (state, level, captured time) |
| `frontend/src/audio/pcmWorklet.test.ts` | DSP tests, run against the worklet file itself |

## Audio graph

```
getUserMedia({audio})        AudioContext (native rate: 44.1 / 48 kHz …)
        │
MediaStreamAudioSourceNode ──► AudioWorkletNode "pcm-capture" ──► GainNode (gain 0) ──► destination
                                       │
                                       └─ port.postMessage({type:"frame", pcm, …}) ──► main thread ──► WebSocket
```

- **Permission only on Start.** `getUserMedia` is called from the Start button, never on
  page load. Constraints ask for audio only, with the browser's echo cancellation, noise
  suppression and automatic gain control. No `sampleRate` or `channelCount` is requested:
  browsers treat those as hints, so the client never relies on them. It reads
  `AudioContext.sampleRate` and resamples itself.
- **Zero-gain output.** The worklet is connected to the destination through a `GainNode`
  whose gain is 0. That keeps the node pulled by the rendering graph on every browser,
  while the microphone is never played back (no feedback or echo).
- **No audio before the server is ready.** The graph is built during `open()` but the
  microphone source is connected to the worklet only in `begin()`, which runs after
  `session_started`. Nothing is captured, buffered or sent before authentication.
- **Off the UI thread.** All per-sample work runs in the AudioWorklet on the audio rendering
  thread. The main thread only forwards ready-made `ArrayBuffer`s (transferred, not copied)
  to the WebSocket and updates the level meter 10 times a second.
- **MediaRecorder is not used.** It produces compressed containers (WebM/Opus) whose format
  depends on the browser. The server needs raw PCM, so the worklet produces it directly.

## Worklet pipeline (per 128-sample render quantum)

1. **Downmix to mono.** Average of all input channels. The node uses
   `channelCountMode: "max"` so stereo microphones arrive as two channels and are averaged
   (not dropped).
2. **Resample to 16 kHz.** A streaming windowed-sinc resampler (below).
3. **Clamp and encode.** Samples are clamped to [-1, 1] and converted to signed 16-bit
   (`-1 → -32768`, `1 → 32767`, rounded) and written little-endian through a `DataView`, so
   the byte order does not depend on the platform.
4. **Frame.** Samples go into a fixed frame of 1600 samples = **100 ms = 3200 bytes**. A full
   frame is posted to the main thread with its RMS and peak level. Frames are never larger
   than 3200 bytes and samples are never lost at frame boundaries.
5. **Flush on stop.** On `{type: "flush"}` the worklet drains the resampler (the right-hand
   side of the filter is padded with silence), posts the last partial frame (an even number
   of bytes, smaller than 3200) and then `{type: "flushed", totalSamples}`. Messages on a
   port are ordered, so every frame arrives before the confirmation. Then `process()`
   returns `false`.

### Confirming the flush

`MicrophoneCapture.finish()` resolves **only** when both of these hold:

- `flushed` arrived within `FLUSH_TIMEOUT_MS` (2 s), and
- its `totalSamples` equals the number of samples delivered through `onFrame` (the worklet
  receives audio only after `begin()`, so the two counts must match).

Otherwise it rejects with a typed `MicrophoneError`:

| Code | Meaning |
| --- | --- |
| `flush_timeout` | No `flushed` within the timeout (stuck or overloaded audio thread) |
| `flush_incomplete` | `flushed` arrived, but samples were lost on the way |
| `aborted` | `close()` was called while the flush was pending |

The microphone and AudioContext are released in every case. A late `flushed` or frame
after a failure is ignored (the port is closed). `useLiveRecording.stop()` then does **not**
send `stop`, because the server would store a "completed" recording whose end was never
received. It aborts the socket instead (see `docs/websocket-frontend.md`).

## Resampler

`Resampler(inputRate, 16000)` in `pcm-worklet.js`:

- **Filter:** windowed sinc (Blackman window), 16 zero crossings, cutoff 0.92 × the output
  Nyquist frequency (7.36 kHz). Speech energy useful to Whisper is below 7 kHz.
  Content above 8 kHz is removed *before* decimation instead of folding back (aliasing) into
  the speech band, which a naive "take every third sample" or linear interpolation would do.
- **Stateful streaming:** the input history and the output clock (`outIndex × step`, computed
  from the index so it never drifts) are kept between calls. The output is identical
  whether the input arrives in 128-sample quanta, single samples or one block. A test
  checks this.
- **Any input rate:** the fractional step (`inputRate / 16000`) handles 44.1 kHz (step
  2.75625) as well as 48 kHz (step 3). The kernel comes from a 512-points-per-sample lookup
  table with linear interpolation. At exactly 16 kHz the resampler is a pass-through.
- **Length:** after `flush()`, `n` input samples give exactly `ceil(n × 16000 / inputRate)`
  output samples. One second of 48 kHz audio gives 16 000 samples.

Measured response (`node` on the worklet file, 1 s sine at 0.5 amplitude):

| Input | 100 Hz – 6 kHz | 6.8 kHz | 7.2 kHz | 7.6 kHz | 9 kHz | 12 kHz | 20 kHz |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 48 kHz (107 taps) | 0.0 dB | -0.8 dB | -3.8 dB | -10.8 dB | -84 dB | -98 dB | -122 dB |
| 44.1 kHz (97 taps) | 0.0 dB | -0.9 dB | -3.8 dB | -10.7 dB | -89 dB | -96 dB | -142 dB |

Added latency is half the filter, about 1.1 ms. Processing 60 s of 48 kHz stereo takes about
0.5 s of one CPU core in Node (under 1% of real time).

## Level meter

The worklet reports the RMS of each 100 ms frame. The UI maps it to a -60…0 dBFS scale
(`meterLevel()` in `microphone.ts`) and draws 24 bars. Nothing is measured on the main
thread.

## Errors

`MicrophoneCapture.open()` throws a `MicrophoneError` with a code and a user-facing message:

| Code | Cause |
| --- | --- |
| `insecure_context` | Page not served over HTTPS (localhost is allowed). Checked before asking. |
| `unsupported` | No `navigator.mediaDevices.getUserMedia` or no `AudioContext` |
| `worklet_unsupported` | No `AudioWorkletNode` / `audioWorklet` |
| `permission_denied` | `NotAllowedError` / `SecurityError` |
| `no_microphone` | `NotFoundError` / `OverconstrainedError`, or a stream without audio tracks |
| `device_busy` | `NotReadableError` / `AbortError` (in use elsewhere, hardware error) |
| `audio_context_failed` | AudioContext creation or loading `/pcm-worklet.js` failed |
| `device_lost` | The track ended during recording (unplugged, permission revoked) |
| `aborted` | `close()` was called while the permission prompt was open |

## Cleanup

`close()` (idempotent) stops every track, closes the worklet port, disconnects all nodes
and closes the `AudioContext`. It runs:

- on `finish()` after the flush (or after a 2 s safety timeout if the worklet never answers),
- when the recording ends for any reason (`session_saved`, disconnect, error),
- when `open()` fails part way (no half-open microphone is left behind),
- when the recording page unmounts.

If the permission prompt is answered after the user cancelled, the stream that arrives late
is stopped immediately. A removed device (`ended` track event) stops the recording cleanly:
the remaining audio is flushed and `stop` is sent, so what was said is still saved.

## Tests

- `src/audio/pcmWorklet.test.ts` imports `public/pcm-worklet.js` itself. It covers mono
  conversion, PCM16 clamping and byte order, 48 kHz and 44.1 kHz to 16 kHz (length, pitch,
  gain), anti-aliasing, block-split invariance, exact output length, frame sizes,
  leftovers, and the registered processor's flush protocol.
- `src/audio/microphone.test.ts` mocks `getUserMedia`, `AudioContext` and `AudioWorkletNode`.
  The fake worklet node runs the real DSP code. It covers every error code, the
  silent-until-`begin()` graph, the zero-gain sink, flush, cleanup and device removal.
- `npm run e2e` runs real Chrome with a fake capture device that plays a 48 kHz stereo WAV,
  so the real AudioWorklet runs in the browser (`docs/websocket-frontend.md`).
