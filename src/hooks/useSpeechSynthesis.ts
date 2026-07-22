'use client';

import { useState, useRef, useCallback } from 'react';

interface UseSpeechSynthesisReturn {
  speak: (text: string) => void;
  cancel: () => void;
  unlockAudio: () => void; // call on a user gesture (e.g. Join click) to unlock Chrome autoplay
  isSpeaking: boolean;
  audioProgress: number; // 0–1
}

export function useSpeechSynthesis(): UseSpeechSynthesisReturn {
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [audioProgress, setAudioProgress] = useState(0);

  const ctxRef = useRef<AudioContext | null>(null);
  const sourceRef = useRef<AudioBufferSourceNode | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const rafRef = useRef<number | null>(null);
  const startTimeRef = useRef<number>(0);
  const durationRef = useRef<number>(0);

  // Returns the shared AudioContext, creating it if needed.
  const getCtx = useCallback(() => {
    if (!ctxRef.current || ctxRef.current.state === 'closed') {
      ctxRef.current = new AudioContext();
    }
    return ctxRef.current;
  }, []);

  // Must be called synchronously inside a user-gesture handler (e.g. button onClick).
  // Resumes the AudioContext and plays a silent frame to fully unlock Chrome.
  const unlockAudio = useCallback(() => {
    const ctx = getCtx();
    const resume = ctx.state === 'suspended' ? ctx.resume() : Promise.resolve();
    resume.then(() => {
      const buf = ctx.createBuffer(1, 1, 22050);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      src.start(0);
    });
  }, [getCtx]);

  const stopRaf = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  }, []);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    stopRaf();
    if (sourceRef.current) {
      try { sourceRef.current.stop(); } catch { /* already stopped */ }
      sourceRef.current = null;
    }
    setIsSpeaking(false);
    setAudioProgress(0);
  }, [stopRaf]);

  const speak = useCallback(async (text: string) => {
    cancel();

    const controller = new AbortController();
    abortRef.current = controller;
    setIsSpeaking(true);
    setAudioProgress(0);

    try {
      const response = await fetch('/api/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
        signal: controller.signal,
      });

      if (!response.ok || controller.signal.aborted) {
        setIsSpeaking(false);
        return;
      }

      const arrayBuffer = await response.arrayBuffer();
      if (controller.signal.aborted) { setIsSpeaking(false); return; }

      const ctx = getCtx();

      // Re-resume in case the context was suspended in the meantime
      if (ctx.state === 'suspended') await ctx.resume();

      const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
      if (controller.signal.aborted) { setIsSpeaking(false); return; }

      const source = ctx.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(ctx.destination);
      sourceRef.current = source;

      durationRef.current = audioBuffer.duration;
      startTimeRef.current = ctx.currentTime;

      // rAF loop to track progress
      const tick = () => {
        const elapsed = ctx.currentTime - startTimeRef.current;
        const p = Math.min(1, elapsed / durationRef.current);
        setAudioProgress(p);
        if (p < 1 && sourceRef.current) {
          rafRef.current = requestAnimationFrame(tick);
        }
      };

      source.onended = () => {
        stopRaf();
        sourceRef.current = null;
        setAudioProgress(1);
        setIsSpeaking(false);
      };

      source.start(0);
      rafRef.current = requestAnimationFrame(tick);

    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        console.error('TTS error:', err);
      }
      setIsSpeaking(false);
      setAudioProgress(0);
    }
  }, [cancel, getCtx, stopRaf]);

  return { speak, cancel, unlockAudio, isSpeaking, audioProgress };
}
