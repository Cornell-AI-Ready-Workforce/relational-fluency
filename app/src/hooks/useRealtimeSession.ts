'use client';

import { useState, useRef, useCallback } from 'react';
import { Message, InterviewFormat } from '@/lib/types';
import { createRealtimeAdapter, RealtimeAdapter } from '@/lib/realtime';

export type RealtimeStatus = 'idle' | 'connecting' | 'live' | 'ended' | 'error';

interface UseRealtimeSessionReturn {
  status: RealtimeStatus;
  /** Full conversation transcript, in the app's Message format */
  messages: Message[];
  /** Incremental transcript of what the agent is currently saying */
  liveAssistantText: string;
  /** Agent audio is playing */
  isSpeaking: boolean;
  /** VAD hears the participant speaking */
  isUserSpeaking: boolean;
  /** The model called complete_interview */
  isComplete: boolean;
  muted: boolean;
  error: string | null;
  connect: (format: InterviewFormat, candidateName: string) => Promise<void>;
  toggleMute: () => void;
  /** Interrupt the agent's current speech */
  skipResponse: () => void;
  /** End the session; resolves with the mixed two-sided recording */
  end: () => Promise<{ blob: Blob | null; durationSeconds: number }>;
}

export function useRealtimeSession(): UseRealtimeSessionReturn {
  const [status, setStatus] = useState<RealtimeStatus>('idle');
  const [messages, setMessages] = useState<Message[]>([]);
  const [liveAssistantText, setLiveAssistantText] = useState('');
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [isUserSpeaking, setIsUserSpeaking] = useState(false);
  const [isComplete, setIsComplete] = useState(false);
  const [muted, setMuted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const adapterRef = useRef<RealtimeAdapter | null>(null);

  const connect = useCallback(async (format: InterviewFormat, candidateName: string) => {
    if (adapterRef.current) return;
    setStatus('connecting');
    setError(null);

    const adapter = createRealtimeAdapter('openai');
    adapterRef.current = adapter;

    try {
      await adapter.connect(
        { format, candidateName },
        {
          onUserTranscript: (text) =>
            setMessages((prev) => [...prev, { role: 'candidate', content: text, timestamp: Date.now() }]),
          onAssistantTranscript: (text) =>
            setMessages((prev) => [...prev, { role: 'interviewer', content: text, timestamp: Date.now() }]),
          onAssistantDelta: setLiveAssistantText,
          onSpeakingChange: setIsSpeaking,
          onUserSpeakingChange: setIsUserSpeaking,
          onComplete: () => setIsComplete(true),
          onError: (message) => setError(message),
        }
      );
      setStatus('live');
    } catch (err) {
      adapterRef.current = null;
      setStatus('error');
      const msg =
        err instanceof DOMException && err.name === 'NotAllowedError'
          ? 'Microphone permission denied. Please allow microphone access.'
          : err instanceof Error
          ? err.message
          : 'Could not connect to the interviewer.';
      setError(msg);
      throw err;
    }
  }, []);

  const toggleMute = useCallback(() => {
    setMuted((prev) => {
      adapterRef.current?.setMuted(!prev);
      return !prev;
    });
  }, []);

  const skipResponse = useCallback(() => {
    adapterRef.current?.cancelResponse();
  }, []);

  const end = useCallback(async () => {
    const adapter = adapterRef.current;
    adapterRef.current = null;
    setStatus('ended');
    setIsSpeaking(false);
    setIsUserSpeaking(false);
    if (!adapter) return { blob: null, durationSeconds: 0 };
    return adapter.disconnect();
  }, []);

  return {
    status,
    messages,
    liveAssistantText,
    isSpeaking,
    isUserSpeaking,
    isComplete,
    muted,
    error,
    connect,
    toggleMute,
    skipResponse,
    end,
  };
}
