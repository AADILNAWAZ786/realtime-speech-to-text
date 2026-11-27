import { useCallback, useEffect, useRef, useState } from "react";

import {
  MicrophoneCapture,
  MicrophoneError,
  meterLevel,
  type AudioEnvironment,
  type AudioFrame,
} from "../audio/microphone";

export type MicrophoneStatus =
  "idle" | "requesting" | "ready" | "capturing" | "stopping" | "closed";

export interface MicrophoneOpenOptions {
  onFrame: (frame: AudioFrame) => void;
  onDeviceLost?: () => void;
}

export interface UseMicrophone {
  status: MicrophoneStatus;
  error: MicrophoneError | null;
  /** Meter level 0..1 of the latest frame. */
  level: number;
  /** Audio captured so far (ms at 16 kHz). */
  capturedMs: number;
  /** The microphone's native rate as used by the AudioContext, e.g. 48000. */
  inputSampleRate: number | null;
  /** Ask for permission and build the audio graph. Throws MicrophoneError. */
  open: (options: MicrophoneOpenOptions) => Promise<void>;
  /** Start delivering frames. */
  begin: () => void;
  /**
   * Stop new audio, deliver the remaining samples, release the device. Rejects with a
   * MicrophoneError (flush_timeout, flush_incomplete, aborted) if the worklet could not
   * confirm that every sample was delivered; the device is released either way.
   */
  finish: () => Promise<void>;
  /** Release everything now. */
  close: () => void;
}

/**
 * React binding for MicrophoneCapture. Permission is requested only when open() is called
 * (from the Start button). The device and AudioContext are released on finish(), close()
 * and unmount.
 */
export function useMicrophone(env?: AudioEnvironment): UseMicrophone {
  const captureRef = useRef<MicrophoneCapture | null>(null);
  const [status, setStatus] = useState<MicrophoneStatus>("idle");
  const [error, setError] = useState<MicrophoneError | null>(null);
  const [level, setLevel] = useState(0);
  const [capturedMs, setCapturedMs] = useState(0);
  const [inputSampleRate, setInputSampleRate] = useState<number | null>(null);

  const close = useCallback(() => {
    captureRef.current?.close();
    captureRef.current = null;
    setLevel(0);
    setStatus((s) => (s === "idle" ? s : "closed"));
  }, []);

  const open = useCallback(
    async ({ onFrame, onDeviceLost }: MicrophoneOpenOptions) => {
      captureRef.current?.close();
      setError(null);
      setLevel(0);
      setCapturedMs(0);
      setInputSampleRate(null);
      setStatus("requesting");
      let samples = 0;
      const capture = new MicrophoneCapture(
        {
          onFrame: (frame) => {
            samples += frame.samples;
            setCapturedMs(Math.floor(samples / 16));
            setLevel(meterLevel(frame.rms));
            onFrame(frame);
          },
          onDeviceLost: () => {
            setError(new MicrophoneError("device_lost"));
            onDeviceLost?.();
          },
        },
        env,
      );
      captureRef.current = capture;
      try {
        await capture.open();
      } catch (caught) {
        if (captureRef.current === capture) captureRef.current = null;
        const micError =
          caught instanceof MicrophoneError ? caught : new MicrophoneError("unknown");
        setError(micError);
        setStatus("closed");
        throw micError;
      }
      setInputSampleRate(capture.inputSampleRate);
      setStatus("ready");
    },
    [env],
  );

  const begin = useCallback(() => {
    captureRef.current?.begin();
    setStatus("capturing");
  }, []);

  const finish = useCallback(async () => {
    const capture = captureRef.current;
    if (!capture) return;
    setStatus("stopping");
    try {
      await capture.finish(); // released in every case, even when it rejects
    } catch (caught) {
      const micError = caught instanceof MicrophoneError ? caught : new MicrophoneError("unknown");
      setError(micError);
      throw micError;
    } finally {
      if (captureRef.current === capture) captureRef.current = null;
      setLevel(0);
      setStatus("closed");
    }
  }, []);

  // Never leave the microphone on after the component goes away.
  useEffect(
    () => () => {
      captureRef.current?.close();
      captureRef.current = null;
    },
    [],
  );

  return { status, error, level, capturedMs, inputSampleRate, open, begin, finish, close };
}
