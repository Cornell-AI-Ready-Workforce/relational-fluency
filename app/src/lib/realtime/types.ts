import { InterviewFormat } from '@/lib/types';

// Model-agnostic realtime speech-to-speech session interface.
// OpenAI Realtime is the current implementation (openaiAdapter);
// gemini-3.1-flash-live-preview will plug in as a second adapter
// behind this same interface.

export type RealtimeProvider = 'openai' | 'gemini';

export interface RealtimeSessionConfig {
  format: InterviewFormat;
  candidateName: string;
}

export interface RealtimeCallbacks {
  /** Final transcript of one completed user utterance */
  onUserTranscript: (text: string) => void;
  /** Incremental assistant transcript while it speaks ('' resets) */
  onAssistantDelta: (delta: string) => void;
  /** Final transcript of one completed assistant turn */
  onAssistantTranscript: (text: string) => void;
  /** Agent audio playback started/stopped */
  onSpeakingChange: (speaking: boolean) => void;
  /** VAD detected the user speaking / stopping */
  onUserSpeakingChange: (speaking: boolean) => void;
  /** The model signalled the interview is complete */
  onComplete: () => void;
  onError: (message: string) => void;
}

export interface RealtimeAdapter {
  connect(config: RealtimeSessionConfig, callbacks: RealtimeCallbacks): Promise<void>;
  /** Mute/unmute the participant microphone */
  setMuted(muted: boolean): void;
  /** Interrupt the agent's current speech */
  cancelResponse(): void;
  /** Close the session; resolves with the mixed two-sided session recording */
  disconnect(): Promise<{ blob: Blob | null; durationSeconds: number }>;
}
