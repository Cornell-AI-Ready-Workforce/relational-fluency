'use client';

import { useState, useRef, useCallback } from 'react';

interface UseSpeechSynthesisReturn {
  speak: (text: string) => void;
  cancel: () => void;
  isSpeaking: boolean;
}

export function useSpeechSynthesis(): UseSpeechSynthesisReturn {
  const [isSpeaking, setIsSpeaking] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.src = '';
      audioRef.current = null;
    }
    setIsSpeaking(false);
  }, []);

  const speak = useCallback(async (text: string) => {
    // Cancel any in-progress speech
    cancel();

    const controller = new AbortController();
    abortRef.current = controller;

    setIsSpeaking(true);

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

      const audioBlob = await response.blob();
      if (controller.signal.aborted) {
        setIsSpeaking(false);
        return;
      }

      const url = URL.createObjectURL(audioBlob);
      const audio = new Audio(url);
      audioRef.current = audio;

      audio.onended = () => {
        URL.revokeObjectURL(url);
        setIsSpeaking(false);
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        setIsSpeaking(false);
      };

      await audio.play();
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        console.error('TTS error:', err);
      }
      setIsSpeaking(false);
    }
  }, [cancel]);

  return { speak, cancel, isSpeaking };
}
