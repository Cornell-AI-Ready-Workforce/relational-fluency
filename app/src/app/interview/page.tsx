'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { Message, InterviewFormat, InterviewSession, SelfReport } from '@/lib/types';
import { getSystemPrompt } from '@/lib/prompts';
import { VoiceButton } from '@/components/VoiceButton';
import { useSpeechRecognition } from '@/hooks/useSpeechRecognition';
import { useSpeechSynthesis } from '@/hooks/useSpeechSynthesis';
import { useAudioRecorder } from '@/hooks/useAudioRecorder';
import { setAudioBlob } from '@/lib/audioStore';

interface SelfReportModalProps {
  candidateName: string;
  onSubmit: (report: SelfReport) => void;
}

function SelfReportModal({ candidateName, onSubmit }: SelfReportModalProps) {
  const [scores, setScores] = useState({
    communication: 3,
    collaboration: 3,
    conflictResolution: 3,
    adaptability: 3,
  });
  const [reflection, setReflection] = useState('');

  const dimensions = [
    { key: 'communication' as const, label: 'Communication', desc: 'Clarity, active listening, expressing ideas' },
    { key: 'collaboration' as const, label: 'Collaboration', desc: 'Teamwork, sharing credit, supporting others' },
    { key: 'conflictResolution' as const, label: 'Conflict Resolution', desc: 'Handling disagreements constructively' },
    { key: 'adaptability' as const, label: 'Adaptability', desc: 'Flexibility and resilience in team settings' },
  ];

  const getScoreLabel = (score: number) => {
    const labels = ['', 'Needs significant work', 'Developing', 'Adequate', 'Strong', 'Excellent'];
    return labels[score] || '';
  };

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-2xl shadow-2xl max-w-lg w-full max-h-[90vh] overflow-y-auto">
        <div className="p-6 border-b border-gray-100">
          <h2 className="text-xl font-bold text-gray-900">Self-Assessment</h2>
          <p className="text-sm text-gray-500 mt-1">
            {candidateName}, how would you rate yourself on these dimensions?
          </p>
        </div>

        <div className="p-6 space-y-6">
          {dimensions.map(({ key, label, desc }) => (
            <div key={key}>
              <div className="flex items-center justify-between mb-2">
                <div>
                  <p className="font-medium text-gray-900 text-sm">{label}</p>
                  <p className="text-xs text-gray-500">{desc}</p>
                </div>
                <div className="text-right">
                  <span className="text-xl font-bold text-purple-600">{scores[key]}</span>
                  <span className="text-gray-400 text-sm">/5</span>
                </div>
              </div>
              <input
                type="range"
                min={1}
                max={5}
                step={1}
                value={scores[key]}
                onChange={(e) => setScores(prev => ({ ...prev, [key]: Number(e.target.value) }))}
                className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-purple-600"
              />
              <div className="flex justify-between text-xs text-gray-400 mt-1">
                <span>1</span>
                <span className="text-purple-600 font-medium">{getScoreLabel(scores[key])}</span>
                <span>5</span>
              </div>
            </div>
          ))}

          <div>
            <label className="block font-medium text-gray-900 text-sm mb-2">
              Personal reflection
            </label>
            <p className="text-xs text-gray-500 mb-2">
              What do you feel went well? What would you do differently?
            </p>
            <textarea
              value={reflection}
              onChange={(e) => setReflection(e.target.value)}
              placeholder="Share your thoughts on how you performed..."
              rows={4}
              className="w-full px-4 py-3 rounded-xl border border-gray-200 text-gray-900 placeholder-gray-400 text-sm focus:outline-none focus:ring-2 focus:ring-purple-500 resize-none"
            />
          </div>
        </div>

        <div className="p-6 border-t border-gray-100">
          <button
            onClick={() => onSubmit({ ...scores, reflection })}
            className="w-full py-3 bg-purple-600 hover:bg-purple-700 text-white rounded-xl font-semibold transition-colors"
          >
            Generate My Report
          </button>
        </div>
      </div>
    </div>
  );
}

interface TextInputFallbackProps {
  onSubmit: (text: string) => void;
  isProcessing: boolean;
}

function TextInputFallback({ onSubmit, isProcessing }: TextInputFallbackProps) {
  const [text, setText] = useState('');

  const handleSubmit = () => {
    if (text.trim() && !isProcessing) {
      onSubmit(text.trim());
      setText('');
    }
  };

  return (
    <div className="flex gap-2">
      <input
        type="text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) handleSubmit(); }}
        placeholder="Type your response..."
        disabled={isProcessing}
        className="flex-1 px-4 py-3 rounded-xl border border-gray-200 bg-white text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-purple-500 text-sm"
      />
      <button
        onClick={handleSubmit}
        disabled={!text.trim() || isProcessing}
        className="px-5 py-3 bg-purple-600 hover:bg-purple-700 disabled:bg-gray-200 text-white rounded-xl font-medium transition-colors text-sm"
      >
        Send
      </button>
    </div>
  );
}

type OrbState = 'idle' | 'speaking' | 'listening' | 'processing';

function InterviewOrb({ state }: { state: OrbState }) {
  const gradients: Record<OrbState, string> = {
    idle: 'radial-gradient(circle at 35% 35%, #e9d5ff, #c084fc, #a855f7, #ec4899)',
    speaking: 'radial-gradient(circle at 35% 35%, #f0abfc, #c026d3, #9333ea, #f43f5e)',
    listening: 'radial-gradient(circle at 35% 35%, #fda4af, #fb7185, #e879f9, #a855f7)',
    processing: 'radial-gradient(circle at 35% 35%, #ddd6fe, #a78bfa, #8b5cf6, #d946ef)',
  };

  const shadows: Record<OrbState, string> = {
    idle: '0 0 60px 20px rgba(168, 85, 247, 0.35), 0 0 120px 40px rgba(236, 72, 153, 0.15)',
    speaking: '0 0 80px 30px rgba(147, 51, 234, 0.5), 0 0 160px 60px rgba(244, 63, 94, 0.2)',
    listening: '0 0 80px 30px rgba(251, 113, 133, 0.5), 0 0 160px 60px rgba(232, 121, 249, 0.25)',
    processing: '0 0 60px 20px rgba(139, 92, 246, 0.4), 0 0 120px 40px rgba(217, 70, 239, 0.15)',
  };

  const ringColor = state === 'speaking'
    ? 'rgba(192, 38, 211, 0.4)'
    : state === 'listening'
    ? 'rgba(251, 113, 133, 0.4)'
    : 'rgba(168, 85, 247, 0.3)';

  return (
    <div className="relative flex items-center justify-center w-64 h-64">
      {/* Ripple rings — only when speaking */}
      {state === 'speaking' && (
        <>
          <div
            className="absolute w-48 h-48 rounded-full animate-ripple-1"
            style={{ background: `radial-gradient(circle, ${ringColor}, transparent)` }}
          />
          <div
            className="absolute w-48 h-48 rounded-full animate-ripple-2"
            style={{ background: `radial-gradient(circle, ${ringColor}, transparent)` }}
          />
          <div
            className="absolute w-48 h-48 rounded-full animate-ripple-3"
            style={{ background: `radial-gradient(circle, ${ringColor}, transparent)` }}
          />
        </>
      )}

      {/* Main orb */}
      <div
        className={`
          relative w-48 h-48 rounded-full flex items-center justify-center overflow-hidden
          ${state === 'idle' ? 'animate-breathe animate-float' : ''}
          ${state === 'speaking' ? 'animate-breathe' : ''}
          ${state === 'processing' ? 'opacity-80' : ''}
        `}
        style={{
          background: gradients[state],
          boxShadow: shadows[state],
        }}
      >
        {/* Wave bars — only when listening */}
        {state === 'listening' && (
          <div className="flex items-center gap-1.5">
            <span className="w-1.5 h-8 bg-white/80 rounded-full animate-wave-1" />
            <span className="w-1.5 h-8 bg-white/80 rounded-full animate-wave-2" />
            <span className="w-1.5 h-8 bg-white/80 rounded-full animate-wave-3" />
            <span className="w-1.5 h-8 bg-white/80 rounded-full animate-wave-4" />
            <span className="w-1.5 h-8 bg-white/80 rounded-full animate-wave-5" />
          </div>
        )}

        {/* Processing shimmer overlay */}
        {state === 'processing' && (
          <div
            className="absolute inset-0 rounded-full"
            style={{
              background: 'linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.3) 50%, transparent 100%)',
              backgroundSize: '200% auto',
              animation: 'shimmer 1.5s linear infinite',
            }}
          />
        )}
      </div>
    </div>
  );
}

export default function InterviewPage() {
  const router = useRouter();
  const [candidateName, setCandidateName] = useState('');
  const [netId, setNetId] = useState('');
  const [format, setFormat] = useState<InterviewFormat>('star');
  const [messages, setMessages] = useState<Message[]>([]);
  const [streamingContent, setStreamingContent] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [isComplete, setIsComplete] = useState(false);
  const [showSelfReport, setShowSelfReport] = useState(false);
  const [selfReport, setSelfReport] = useState<SelfReport | undefined>();
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [startTime] = useState(Date.now());
  const [hasStarted, setHasStarted] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const sessionRef = useRef<{ candidateName: string; netId: string; format: InterviewFormat } | null>(null);

  const { transcript, interimTranscript, isListening, startListening, stopListening, error: speechError, isSupported } = useSpeechRecognition();
  const { speak, cancel: cancelSpeech, isSpeaking } = useSpeechSynthesis();
  const { startRecording, stopRecording, durationSeconds: audioDuration } = useAudioRecorder();

  // Load session config from sessionStorage
  useEffect(() => {
    const configStr = sessionStorage.getItem('interviewConfig');
    if (!configStr) {
      router.push('/');
      return;
    }
    const config = JSON.parse(configStr);
    setCandidateName(config.candidateName);
    setNetId(config.netId ?? '');
    setFormat(config.format);
    sessionRef.current = config;
  }, [router]);

  // Timer
  useEffect(() => {
    const interval = setInterval(() => {
      setElapsedSeconds(s => s + 1);
    }, 1000);
    return () => clearInterval(interval);
  }, []);

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingContent]);

  // Start the interview with the first AI message
  const startInterview = useCallback(async (name: string, fmt: InterviewFormat) => {
    if (hasStarted) return;
    setHasStarted(true);
    setIsProcessing(true);

    // Start audio recording silently in background
    try {
      await startRecording();
    } catch {
      // Recording is optional — don't block the interview
    }

    const systemPrompt = getSystemPrompt(fmt);
    // Anthropic API requires at least one message — send a kick-off to prompt the AI to start
    const initialMessages: Message[] = [{
      role: 'candidate',
      content: 'Hello, I am ready to begin the interview.',
      timestamp: Date.now(),
    }];

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: initialMessages,
          format: fmt,
          candidateName: name,
          systemPrompt,
        }),
      });

      if (!response.ok || !response.body) {
        throw new Error('Failed to get AI response');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let fullContent = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value);
        fullContent += chunk;
        setStreamingContent(fullContent);
      }

      const aiMessage: Message = {
        role: 'interviewer',
        content: fullContent,
        timestamp: Date.now(),
      };

      setMessages([aiMessage]);
      setStreamingContent('');

      // Check for completion signal
      if (fullContent.includes('"action": "complete"') || fullContent.includes('"action":"complete"')) {
        setIsComplete(true);
        if (fmt === 'star') {
          setShowSelfReport(true);
        } else {
          finishInterview([], undefined, name, fmt, fullContent);
        }
      } else {
        speak(fullContent);
      }
    } catch (err) {
      console.error('Error starting interview:', err);
    } finally {
      setIsProcessing(false);
    }
  }, [hasStarted, speak, startRecording]);

  // Trigger interview start when config is loaded
  useEffect(() => {
    if (candidateName && format && !hasStarted) {
      startInterview(candidateName, format);
    }
  }, [candidateName, format, hasStarted, startInterview]);

  // Handle completed speech recognition — auto submit
  useEffect(() => {
    if (transcript && !isListening && !isProcessing && !isSpeaking) {
      handleCandidateResponse(transcript);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transcript, isListening]);

  const handleCandidateResponse = async (text: string) => {
    if (!text.trim() || isProcessing || isComplete) return;

    cancelSpeech();

    const candidateMessage: Message = {
      role: 'candidate',
      content: text,
      timestamp: Date.now(),
    };

    const updatedMessages = [...messages, candidateMessage];
    setMessages(updatedMessages);
    setIsProcessing(true);

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: updatedMessages,
          format,
          candidateName,
        }),
      });

      if (!response.ok || !response.body) {
        throw new Error('Failed to get AI response');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let fullContent = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value);
        fullContent += chunk;
        setStreamingContent(fullContent);
      }

      const aiMessage: Message = {
        role: 'interviewer',
        content: fullContent,
        timestamp: Date.now(),
      };

      const finalMessages = [...updatedMessages, aiMessage];
      setMessages(finalMessages);
      setStreamingContent('');

      // Check for completion signal
      if (fullContent.includes('"action": "complete"') || fullContent.includes('"action":"complete"')) {
        setIsComplete(true);
        speak(fullContent);
        if (format === 'star') {
          // Wait a moment for TTS to start, then show modal
          setTimeout(() => setShowSelfReport(true), 500);
        } else {
          setTimeout(() => finishInterview(finalMessages, undefined, candidateName, format, fullContent), 2000);
        }
      } else {
        speak(fullContent);
      }
    } catch (err) {
      console.error('Error getting AI response:', err);
    } finally {
      setIsProcessing(false);
    }
  };

  const finishInterview = async (
    finalMessages: Message[],
    report: SelfReport | undefined,
    name: string,
    fmt: InterviewFormat,
    lastAiMessage?: string
  ) => {
    void lastAiMessage;
    const messagesToSave = finalMessages.length > 0 ? finalMessages : messages;

    // Stop audio recording and store blob
    try {
      const blob = await stopRecording();
      if (blob) {
        setAudioBlob(blob, audioDuration);
      }
    } catch {
      // Recording failure should not block saving the interview
    }

    const session: InterviewSession = {
      format: fmt,
      candidateName: name,
      netId,
      messages: messagesToSave,
      selfReport: report,
      startTime,
      endTime: Date.now(),
    };
    sessionStorage.setItem('interviewSession', JSON.stringify(session));
    router.push('/report');
  };

  const handleSelfReportSubmit = (report: SelfReport) => {
    setSelfReport(report);
    setShowSelfReport(false);
    finishInterview(messages, report, candidateName, format);
  };

  const handleFinishEarly = () => {
    cancelSpeech();
    if (format === 'star') {
      setShowSelfReport(true);
    } else {
      finishInterview(messages, undefined, candidateName, format);
    }
  };

  const formatTime = (seconds: number) => {
    const m = Math.floor(seconds / 60).toString().padStart(2, '0');
    const s = (seconds % 60).toString().padStart(2, '0');
    return `${m}:${s}`;
  };

  const formatTitle = format === 'star' ? 'STAR Behavioral Interview' : 'Role Play Scenarios';

  // Determine orb state
  const orbState: OrbState = isListening
    ? 'listening'
    : isSpeaking
    ? 'speaking'
    : isProcessing
    ? 'processing'
    : 'idle';

  // Determine status label
  const statusLabel = isListening
    ? 'Listening...'
    : isSpeaking
    ? 'Speaking...'
    : isProcessing
    ? 'Thinking...'
    : isComplete
    ? 'Interview complete'
    : 'Ready';

  // Get the last interviewer message to display
  const lastInterviewerMessage = [...messages].reverse().find(m => m.role === 'interviewer');
  const displayContent = streamingContent || lastInterviewerMessage?.content || '';
  const cleanDisplayContent = displayContent
    .replace(/\{"action":\s*"complete"\}/g, '')
    .replace(/\{"action":"complete"\}/g, '')
    .trim();

  return (
    <div className="flex flex-col h-screen bg-gray-50">
      {/* Header */}
      <header className="flex-shrink-0 bg-white/70 backdrop-blur-md border-b border-purple-100 px-6 py-3">
        <div className="max-w-3xl mx-auto flex items-center justify-between">
          <div>
            <h1 className="font-semibold text-gray-900 text-sm">{formatTitle}</h1>
            {candidateName && (
              <p className="text-xs text-gray-500">Interviewing: {candidateName}</p>
            )}
          </div>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2 text-gray-500">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <span className="text-sm font-mono">{formatTime(elapsedSeconds)}</span>
            </div>
            {!isComplete && (
              <button
                onClick={handleFinishEarly}
                className="text-sm text-gray-500 hover:text-red-600 border border-gray-200 hover:border-red-300 px-3 py-1.5 rounded-lg transition-colors"
              >
                Finish Early
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Center area */}
      <div className="flex-1 flex flex-col items-center justify-center px-6 gap-8">
        {/* AI message display */}
        <div
          key={messages.length}
          className="animate-fade-in text-center max-w-lg"
        >
          {cleanDisplayContent ? (
            <>
              <p className="text-xs font-medium text-purple-500 mb-3 uppercase tracking-wider">
                AI Interviewer
              </p>
              <p className="text-xl leading-relaxed text-gray-800">
                {cleanDisplayContent}
              </p>
            </>
          ) : isProcessing ? null : (
            <p className="text-gray-400 text-base italic">Waiting to begin...</p>
          )}
        </div>

        {/* Processing dots — centered under message */}
        {isProcessing && !streamingContent && (
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 bg-purple-300 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
            <span className="w-2 h-2 bg-pink-300 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
            <span className="w-2 h-2 bg-purple-300 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
          </div>
        )}

        {/* The Orb */}
        <InterviewOrb state={orbState} />

        {/* Status label */}
        <p className="text-sm font-medium text-purple-400 tracking-wide">
          {statusLabel}
        </p>

        {/* Live interim transcript */}
        {interimTranscript && (
          <p className="text-sm text-gray-500 italic text-center max-w-md">
            {interimTranscript}
          </p>
        )}
      </div>

      {/* Bottom bar */}
      {!isComplete && (
        <div className="flex-shrink-0 px-6 py-6">
          <div className="max-w-3xl mx-auto flex flex-col items-center gap-3">
            {speechError && (
              <p className="text-xs text-red-500 text-center">{speechError}</p>
            )}

            {isSupported ? (
              <VoiceButton
                isListening={isListening}
                isProcessing={isProcessing}
                isDisabled={isComplete || messages.length === 0}
                isSpeaking={isSpeaking}
                onStart={startListening}
                onStop={stopListening}
                transcript={transcript}
                interimTranscript={interimTranscript}
                isSupported={isSupported}
              />
            ) : (
              <div className="w-full max-w-md">
                <p className="text-xs text-amber-600 text-center mb-3">
                  Voice input requires <strong>Google Chrome</strong>. You&apos;re using a browser that doesn&apos;t support the Web Speech API. Using text input instead — or switch to Chrome for voice.
                </p>
                <TextInputFallback
                  onSubmit={handleCandidateResponse}
                  isProcessing={isProcessing}
                />
              </div>
            )}

            {isSpeaking && (
              <button
                onClick={cancelSpeech}
                className="text-xs text-gray-400 hover:text-gray-600 underline"
              >
                Skip audio
              </button>
            )}
          </div>
        </div>
      )}

      {/* Hidden scroll anchor (kept for logic compatibility) */}
      <div ref={messagesEndRef} />

      {/* Self Report Modal */}
      {showSelfReport && (
        <SelfReportModal
          candidateName={candidateName}
          onSubmit={handleSelfReportSubmit}
        />
      )}
    </div>
  );
}
