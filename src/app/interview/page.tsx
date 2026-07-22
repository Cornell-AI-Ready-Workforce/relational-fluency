'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { Message, InterviewFormat, InterviewSession, SelfReport } from '@/lib/types';
import { getSystemPrompt } from '@/lib/prompts';
import { useSpeechRecognition } from '@/hooks/useSpeechRecognition';
import { useSpeechSynthesis } from '@/hooks/useSpeechSynthesis';
import { useAudioRecorder } from '@/hooks/useAudioRecorder';
import { setAudioBlob } from '@/lib/audioStore';

// ─── Woman Avatar SVG ────────────────────────────────────────────────────────
function WomanAvatar({ speaking }: { speaking: boolean }) {
  return (
    <div
      className="relative rounded-full overflow-hidden"
      style={{
        width: 120,
        height: 120,
        boxShadow: speaking
          ? '0 0 0 4px #4ade80, 0 0 20px rgba(74,222,128,0.4)'
          : '0 0 0 3px rgba(255,255,255,0.1)',
      }}
    >
      <svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg" width="120" height="120">
        {/* Background */}
        <defs>
          <radialGradient id="bgGrad" cx="50%" cy="40%" r="60%">
            <stop offset="0%" stopColor="#6366f1" />
            <stop offset="100%" stopColor="#4338ca" />
          </radialGradient>
          <radialGradient id="skinGrad" cx="50%" cy="40%" r="60%">
            <stop offset="0%" stopColor="#fcd9b6" />
            <stop offset="100%" stopColor="#f5b98e" />
          </radialGradient>
          <radialGradient id="hairGrad" cx="50%" cy="30%" r="60%">
            <stop offset="0%" stopColor="#3d1f0a" />
            <stop offset="100%" stopColor="#1a0a00" />
          </radialGradient>
        </defs>

        <circle cx="100" cy="100" r="100" fill="url(#bgGrad)" />

        {/* Shoulders / blouse */}
        <ellipse cx="100" cy="210" rx="72" ry="55" fill="#4f46e5" />
        <ellipse cx="100" cy="200" rx="50" ry="38" fill="#6366f1" />

        {/* Neck */}
        <rect x="88" y="138" width="24" height="30" rx="6" fill="url(#skinGrad)" />

        {/* Head */}
        <ellipse cx="100" cy="110" rx="42" ry="46" fill="url(#skinGrad)" />

        {/* Hair — back layer */}
        <ellipse cx="100" cy="80" rx="46" ry="38" fill="url(#hairGrad)" />
        {/* Hair — left side flowing */}
        <ellipse cx="62" cy="112" rx="18" ry="30" fill="url(#hairGrad)" />
        {/* Hair — right side flowing */}
        <ellipse cx="138" cy="112" rx="18" ry="30" fill="url(#hairGrad)" />
        {/* Hair top */}
        <ellipse cx="100" cy="68" rx="42" ry="22" fill="url(#hairGrad)" />

        {/* Ear left */}
        <ellipse cx="59" cy="112" rx="7" ry="9" fill="url(#skinGrad)" />
        {/* Ear right */}
        <ellipse cx="141" cy="112" rx="7" ry="9" fill="url(#skinGrad)" />

        {/* Eyes */}
        <ellipse cx="84" cy="108" rx="7" ry="5" fill="#2d1b00" />
        <ellipse cx="116" cy="108" rx="7" ry="5" fill="#2d1b00" />
        {/* Eye shine */}
        <circle cx="87" cy="106" r="2" fill="white" opacity="0.7" />
        <circle cx="119" cy="106" r="2" fill="white" opacity="0.7" />
        {/* Eyelashes */}
        <path d="M77 103 Q84 100 91 103" stroke="#2d1b00" strokeWidth="1.5" fill="none" />
        <path d="M109 103 Q116 100 123 103" stroke="#2d1b00" strokeWidth="1.5" fill="none" />

        {/* Nose */}
        <ellipse cx="100" cy="120" rx="4" ry="3" fill="#e8a077" opacity="0.6" />

        {/* Smile */}
        <path d="M89 130 Q100 140 111 130" stroke="#c47a5a" strokeWidth="2.5" fill="none" strokeLinecap="round" />

        {/* Eyebrows */}
        <path d="M77 99 Q84 95 91 98" stroke="#3d1f0a" strokeWidth="2.5" fill="none" strokeLinecap="round" />
        <path d="M109 98 Q116 95 123 99" stroke="#3d1f0a" strokeWidth="2.5" fill="none" strokeLinecap="round" />

        {/* Earring left */}
        <circle cx="59" cy="122" r="3" fill="#fbbf24" />
        {/* Earring right */}
        <circle cx="141" cy="122" r="3" fill="#fbbf24" />
      </svg>
    </div>
  );
}

// ─── Zoom-style icons ────────────────────────────────────────────────────────
function MicIcon({ muted }: { muted: boolean }) {
  if (muted) {
    return (
      <svg className="w-6 h-6" fill="currentColor" viewBox="0 0 24 24">
        <path d="M19 11h-1.7c0 .74-.16 1.43-.43 2.05l1.23 1.23c.56-.98.9-2.09.9-3.28zm-4.02.17c0-.06.02-.11.02-.17V5c0-1.66-1.34-3-3-3S9 3.34 9 5v.18l5.98 5.99zM4.27 3L3 4.27l6.01 6.01V11c0 1.66 1.33 3 2.99 3 .22 0 .44-.03.65-.08l1.66 1.66c-.71.33-1.5.52-2.31.52-2.76 0-5.3-2.1-5.3-5.1H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c.91-.13 1.77-.45 2.54-.9L19.73 21 21 19.73 4.27 3z" />
      </svg>
    );
  }
  return (
    <svg className="w-6 h-6" fill="currentColor" viewBox="0 0 24 24">
      <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" />
      <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
    </svg>
  );
}

function VideoOffIcon() {
  return (
    <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
      <path d="M21 6.5l-4-4-2 2-1 1v7.93L21 6.5zM3.27 2L2 3.27 4.73 6H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.21 0 .39-.08.54-.18L19.73 21 21 19.73 3.27 2zM15 17H5V8h.73L15 17.27V17z" />
    </svg>
  );
}

// ─── Self-Report Modal ───────────────────────────────────────────────────────
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
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50 p-4">
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

// ─── Text input fallback ─────────────────────────────────────────────────────
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
    <div className="flex gap-2 w-full max-w-md">
      <input
        type="text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) handleSubmit(); }}
        placeholder="Type your response..."
        disabled={isProcessing}
        className="flex-1 px-4 py-2 rounded-xl bg-[#3a3a3a] text-white placeholder-gray-500 border border-gray-600 focus:outline-none focus:ring-2 focus:ring-indigo-500 text-sm"
      />
      <button
        onClick={handleSubmit}
        disabled={!text.trim() || isProcessing}
        className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-gray-600 text-white rounded-xl font-medium transition-colors text-sm"
      >
        Send
      </button>
    </div>
  );
}

// ─── Synced subtitle component — one sentence at a time ─────────────────────
function SyncedSubtitle({ text, progress, isSpeaking }: { text: string; progress: number; isSpeaking: boolean }) {
  // Split into sentences on . ! ?
  const sentences = text.match(/[^.!?]+[.!?]*/g)?.map(s => s.trim()).filter(Boolean) ?? [text];

  if (!isSpeaking) {
    // Interview done speaking — show last sentence
    return (
      <p className="text-white text-sm leading-relaxed text-center">
        {sentences[sentences.length - 1]}
      </p>
    );
  }

  // Show the sentence currently being spoken
  const idx = Math.min(
    Math.floor(progress * sentences.length),
    sentences.length - 1
  );

  return (
    <p key={idx} className="text-white text-sm leading-relaxed text-center animate-fade-in">
      {sentences[idx]}
    </p>
  );
}

// ─── Main Page ───────────────────────────────────────────────────────────────
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
  const [hasJoined, setHasJoined] = useState(false); // user must click Join before audio plays

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const sessionRef = useRef<{ candidateName: string; netId: string; format: InterviewFormat } | null>(null);

  const { transcript, isListening, isTranscribing, startListening, stopListening, error: speechError, isSupported } = useSpeechRecognition();
  const { speak, cancel: cancelSpeech, unlockAudio, isSpeaking, audioProgress } = useSpeechSynthesis();
  const { startRecording, stopRecording, durationSeconds: audioDuration } = useAudioRecorder();

  // Load session config
  useEffect(() => {
    const configStr = sessionStorage.getItem('interviewConfig');
    if (!configStr) { router.push('/'); return; }
    const config = JSON.parse(configStr);
    setCandidateName(config.candidateName);
    setNetId(config.netId ?? '');
    setFormat(config.format);
    sessionRef.current = config;
  }, [router]);

  // Timer
  useEffect(() => {
    const interval = setInterval(() => setElapsedSeconds(s => s + 1), 1000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingContent]);

  const startInterview = useCallback(async (name: string, fmt: InterviewFormat) => {
    if (hasStarted) return;
    setHasStarted(true);
    setIsProcessing(true);

    try { await startRecording(); } catch { /* optional */ }

    const systemPrompt = getSystemPrompt(fmt);
    const initialMessages: Message[] = [{
      role: 'candidate',
      content: 'Hello, I am ready to begin the interview.',
      timestamp: Date.now(),
    }];

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: initialMessages, format: fmt, candidateName: name, systemPrompt }),
      });

      if (!response.ok || !response.body) throw new Error('Failed to get AI response');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let fullContent = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        fullContent += decoder.decode(value);
        setStreamingContent(fullContent);
      }

      const aiMessage: Message = { role: 'interviewer', content: fullContent, timestamp: Date.now() };
      setMessages([aiMessage]);
      setStreamingContent('');

      if (fullContent.includes('"action": "complete"') || fullContent.includes('"action":"complete"')) {
        setIsComplete(true);
        if (fmt === 'star') setShowSelfReport(true);
        else finishInterview([], undefined, name, fmt, fullContent);
      } else {
        speak(fullContent);
      }
    } catch (err) {
      console.error('Error starting interview:', err);
    } finally {
      setIsProcessing(false);
    }
  }, [hasStarted, speak, startRecording]);

  // Only start the interview once the user has clicked "Join" (ensures Chrome autoplay works)
  useEffect(() => {
    if (candidateName && format && !hasStarted && hasJoined) startInterview(candidateName, format);
  }, [candidateName, format, hasStarted, hasJoined, startInterview]);

  useEffect(() => {
    if (transcript && !isListening && !isTranscribing && !isProcessing && !isSpeaking) {
      handleCandidateResponse(transcript);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transcript, isListening, isTranscribing]);

  const handleCandidateResponse = async (text: string) => {
    if (!text.trim() || isProcessing || isComplete) return;
    cancelSpeech();

    const candidateMessage: Message = { role: 'candidate', content: text, timestamp: Date.now() };
    const updatedMessages = [...messages, candidateMessage];
    setMessages(updatedMessages);
    setIsProcessing(true);

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: updatedMessages, format, candidateName }),
      });

      if (!response.ok || !response.body) throw new Error('Failed to get AI response');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let fullContent = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        fullContent += decoder.decode(value);
        setStreamingContent(fullContent);
      }

      const aiMessage: Message = { role: 'interviewer', content: fullContent, timestamp: Date.now() };
      const finalMessages = [...updatedMessages, aiMessage];
      setMessages(finalMessages);
      setStreamingContent('');

      if (fullContent.includes('"action": "complete"') || fullContent.includes('"action":"complete"')) {
        setIsComplete(true);
        speak(fullContent);
        if (format === 'star') setTimeout(() => setShowSelfReport(true), 500);
        else setTimeout(() => finishInterview(finalMessages, undefined, candidateName, format, fullContent), 2000);
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

    try {
      const blob = await stopRecording();
      if (blob) setAudioBlob(blob, audioDuration);
    } catch { /* optional */ }

    const session: InterviewSession = {
      format: fmt, candidateName: name, netId,
      messages: messagesToSave, selfReport: report,
      startTime, endTime: Date.now(),
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
    if (format === 'star') setShowSelfReport(true);
    else finishInterview(messages, undefined, candidateName, format);
  };

  const handleMicClick = () => {
    if (isListening) stopListening();
    else startListening();
  };

  const formatTime = (seconds: number) => {
    const m = Math.floor(seconds / 60).toString().padStart(2, '0');
    const s = (seconds % 60).toString().padStart(2, '0');
    return `${m}:${s}`;
  };

  const formatTitle = format === 'star' ? 'STAR Behavioral Interview' : 'Role Play Scenarios';

  // Clean display content
  const lastInterviewerMessage = [...messages].reverse().find(m => m.role === 'interviewer');
  const displayContent = streamingContent || lastInterviewerMessage?.content || '';
  const cleanDisplayContent = displayContent
    .replace(/\{"action":\s*"complete"\}/g, '')
    .replace(/\{"action":"complete"\}/g, '')
    .trim();

  const micDisabled = isComplete || messages.length === 0 || isProcessing || isSpeaking || isTranscribing;

  // ── Join screen (shown before interview starts, ensures Chrome autoplay gesture) ──
  if (!hasJoined) {
    return (
      <div className="flex flex-col h-screen bg-[#1c1c1c] text-white items-center justify-center gap-8">
        <div className="flex flex-col items-center gap-6 max-w-sm text-center">
          {/* Preview of the interviewer tile */}
          <div className="relative rounded-2xl overflow-hidden flex items-center justify-center bg-[#2d2d2d]"
            style={{ width: 320, height: 220 }}>
            <div className="flex flex-col items-center gap-3">
              <WomanAvatar speaking={false} />
              <div className="flex items-center gap-1.5 text-gray-500 text-xs">
                <VideoOffIcon />
                <span>Camera is off</span>
              </div>
            </div>
            <div className="absolute bottom-3 left-3">
              <span className="text-white text-xs font-medium bg-black/60 px-2.5 py-1 rounded-md">
                Alex · AI Interviewer
              </span>
            </div>
            <div className="absolute top-3 right-3 text-gray-500 text-[10px] font-bold border border-gray-600 px-1.5 py-0.5 rounded">HD</div>
          </div>

          <div>
            <h2 className="text-xl font-semibold text-white">{formatTitle}</h2>
            {candidateName && <p className="text-gray-400 text-sm mt-1">Ready to join, {candidateName}?</p>}
          </div>

          {/* Browser note */}
          <div className="bg-[#2d2d2d] rounded-xl px-4 py-3 text-xs text-gray-400 text-left space-y-1">
            <p><span className="text-green-400 font-medium">Voice input & output:</span> powered by ElevenLabs</p>
            <p><span className="text-green-400 font-medium">Works in all browsers</span> — Chrome, Firefox, Safari</p>
          </div>

          <button
            onClick={() => { unlockAudio(); setHasJoined(true); }}
            className="w-full py-3 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl font-semibold text-base transition-colors"
          >
            Join Interview
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-screen bg-[#1c1c1c] text-white overflow-hidden">

      {/* ── Top bar ── */}
      <header className="flex-shrink-0 flex items-center justify-between px-5 py-3 bg-[#1c1c1c]">
        <div className="flex items-center gap-2">
          <svg className="w-4 h-4 text-green-400" fill="currentColor" viewBox="0 0 24 24">
            <path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zM12 17c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z" />
          </svg>
          <span className="text-white text-sm font-medium">{formatTitle}</span>
          {candidateName && (
            <span className="text-gray-500 text-sm">· {candidateName}</span>
          )}
        </div>

        <div className="flex items-center gap-3">
          {/* Recording dot */}
          <div className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
            <span className="text-gray-400 text-xs font-mono">{formatTime(elapsedSeconds)}</span>
          </div>
        </div>
      </header>

      {/* ── Main video area ── */}
      <div className="flex-1 relative flex items-center justify-center p-4">

        {/* Interviewer tile */}
        <div
          className="relative rounded-2xl overflow-hidden flex items-center justify-center transition-all duration-300"
          style={{
            width: '65vw',
            maxWidth: 720,
            height: '65vh',
            maxHeight: 480,
            background: 'linear-gradient(135deg, #2a2a2a 0%, #242424 100%)',
            boxShadow: isSpeaking
              ? '0 0 0 3px #4ade80, 0 0 40px rgba(74,222,128,0.2)'
              : '0 0 0 1px rgba(255,255,255,0.06)',
          }}
        >
          {/* Camera-off content */}
          <div className="flex flex-col items-center gap-5">
            <WomanAvatar speaking={isSpeaking} />

            {/* Processing dots */}
            {isProcessing && (
              <div className="flex items-center gap-1.5">
                <span className="w-2 h-2 bg-indigo-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                <span className="w-2 h-2 bg-purple-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                <span className="w-2 h-2 bg-indigo-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
            )}

            {/* Camera-off label */}
            <div className="flex items-center gap-1.5 text-gray-500 text-xs">
              <VideoOffIcon />
              <span>Camera is off</span>
            </div>
          </div>

          {/* AI message caption — synced word-by-word to audio when speaking */}
          {cleanDisplayContent && !isProcessing && (
            <div className="absolute top-4 left-4 right-4">
              <div className="bg-black/60 backdrop-blur-sm rounded-xl px-4 py-3 text-center">
                {streamingContent ? (
                  <>
                    <p className="text-white text-sm leading-relaxed">{cleanDisplayContent}</p>
                    <span className="inline-block w-1 h-4 ml-1 bg-white/70 animate-pulse rounded-sm align-middle" />
                  </>
                ) : (
                  <SyncedSubtitle
                    text={cleanDisplayContent}
                    progress={audioProgress}
                    isSpeaking={isSpeaking}
                  />
                )}
              </div>
            </div>
          )}

          {/* Speaking waveform bar at bottom of tile */}
          {isSpeaking && (
            <div className="absolute bottom-14 left-1/2 -translate-x-1/2 flex items-center gap-1">
              {[1,2,3,4,5,6,7].map((i) => (
                <div
                  key={i}
                  className="w-1 rounded-full bg-green-400"
                  style={{
                    height: `${8 + (i % 3) * 8}px`,
                    animation: `wave${((i % 5) + 1)} 0.8s ease-in-out infinite`,
                    animationDelay: `${i * 80}ms`,
                  }}
                />
              ))}
            </div>
          )}

          {/* Name tag */}
          <div className="absolute bottom-3 left-3 flex items-center gap-2">
            {isSpeaking && (
              <span className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
            )}
            <span className="text-white text-xs font-medium bg-black/60 px-2.5 py-1 rounded-md">
              Alex · AI Interviewer
            </span>
          </div>

          {/* HD badge */}
          <div className="absolute top-3 right-3 text-gray-500 text-[10px] font-bold border border-gray-600 px-1.5 py-0.5 rounded">
            HD
          </div>
        </div>

        {/* ── Self-view tile (candidate) ── */}
        <div
          className="absolute bottom-6 right-6 rounded-xl overflow-hidden flex flex-col items-center justify-center transition-all duration-200"
          style={{
            width: 160,
            height: 110,
            background: '#2d2d2d',
            boxShadow: isListening
              ? '0 0 0 2px #4ade80'
              : isTranscribing
              ? '0 0 0 2px #facc15'
              : '0 0 0 1px rgba(255,255,255,0.08)',
          }}
        >
          {isListening ? (
            <div className="flex flex-col items-center gap-2">
              <div className="flex items-end gap-0.5 h-8">
                {[1,2,3,4,5,6,7,8].map((i) => (
                  <div
                    key={i}
                    className="w-1 rounded-full bg-green-400"
                    style={{
                      height: `${6 + (i % 4) * 6}px`,
                      animation: `wave${((i % 5) + 1)} 0.6s ease-in-out infinite`,
                      animationDelay: `${i * 60}ms`,
                    }}
                  />
                ))}
              </div>
              <span className="text-green-400 text-[10px] font-medium">Recording...</span>
            </div>
          ) : isTranscribing ? (
            <div className="flex flex-col items-center gap-2">
              <div className="flex items-center gap-1">
                <span className="w-1.5 h-1.5 bg-yellow-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                <span className="w-1.5 h-1.5 bg-yellow-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                <span className="w-1.5 h-1.5 bg-yellow-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
              <span className="text-yellow-400 text-[10px] font-medium">Transcribing...</span>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-2">
              <div className="w-10 h-10 rounded-full bg-[#4a4a6a] flex items-center justify-center text-white text-sm font-semibold">
                {candidateName ? candidateName[0].toUpperCase() : 'Y'}
              </div>
              <span className="text-gray-400 text-[10px]">{candidateName || 'You'} (You)</span>
            </div>
          )}

          <div className="absolute bottom-1.5 left-2">
            <span className="text-gray-400 text-[9px]">{candidateName || 'You'}</span>
          </div>
        </div>

        {/* Status bubble — transcribing */}
        {isTranscribing && (
          <div className="absolute bottom-28 left-1/2 -translate-x-1/2 bg-black/70 rounded-xl px-4 py-2 max-w-sm text-center">
            <p className="text-yellow-300 text-xs">Processing your response...</p>
          </div>
        )}
      </div>

      {/* ── Bottom toolbar ── */}
      <div className="flex-shrink-0 bg-[#242424] border-t border-gray-800 py-4 px-6">
        <div className="flex items-center justify-center gap-4">

          {/* Mic / unmute button */}
          {isSupported ? (
            <div className="flex flex-col items-center gap-1">
              <button
                onClick={handleMicClick}
                disabled={micDisabled}
                title={isListening ? 'Click to stop' : isSpeaking ? 'Wait for interviewer' : 'Click to speak'}
                className={`
                  flex items-center justify-center w-12 h-12 rounded-full transition-all duration-200
                  ${isListening
                    ? 'bg-red-500 hover:bg-red-600 ring-4 ring-red-400/30'
                    : micDisabled
                    ? 'bg-[#3a3a3a] text-gray-500 cursor-not-allowed'
                    : 'bg-[#3a3a3a] hover:bg-[#4a4a4a] text-white'}
                `}
              >
                <MicIcon muted={!isListening && !micDisabled ? false : micDisabled && !isListening} />
              </button>
              <span className="text-gray-400 text-[10px]">
                {isListening ? 'Stop' : isTranscribing ? 'Reading...' : isSpeaking ? 'Wait...' : isProcessing ? 'Thinking...' : 'Speak'}
              </span>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-1">
              <div className="w-12 h-12 rounded-full bg-[#3a3a3a] flex items-center justify-center text-gray-500">
                <MicIcon muted={true} />
              </div>
              <span className="text-gray-500 text-[10px]">No mic</span>
            </div>
          )}

          {/* Stop Video button (decorative) */}
          <div className="flex flex-col items-center gap-1">
            <button className="flex items-center justify-center w-12 h-12 rounded-full bg-[#3a3a3a] hover:bg-[#4a4a4a] text-white transition-colors">
              <VideoOffIcon />
            </button>
            <span className="text-gray-400 text-[10px]">Stop Video</span>
          </div>

          {/* Skip audio */}
          {isSpeaking && (
            <div className="flex flex-col items-center gap-1">
              <button
                onClick={cancelSpeech}
                className="flex items-center justify-center w-12 h-12 rounded-full bg-[#3a3a3a] hover:bg-[#4a4a4a] text-white transition-colors"
                title="Skip audio"
              >
                <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
                  <path d="M6 18l8.5-6L6 6v12zm2-8.14L11.03 12 8 14.14V9.86zM16 6h2v12h-2z"/>
                </svg>
              </button>
              <span className="text-gray-400 text-[10px]">Skip</span>
            </div>
          )}

          {/* Participants count */}
          <div className="flex flex-col items-center gap-1">
            <button className="flex items-center justify-center w-12 h-12 rounded-full bg-[#3a3a3a] hover:bg-[#4a4a4a] text-white transition-colors">
              <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
                <path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z" />
              </svg>
            </button>
            <span className="text-gray-400 text-[10px]">2</span>
          </div>

          {/* Spacer */}
          <div className="w-8" />

          {/* End Call */}
          {!isComplete && (
            <div className="flex flex-col items-center gap-1">
              <button
                onClick={handleFinishEarly}
                className="flex items-center justify-center px-5 h-12 rounded-full bg-red-500 hover:bg-red-600 text-white font-medium transition-colors gap-2 text-sm"
              >
                <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                  <path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56-.35-.12-.74-.03-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89-6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.11-.56-2.3-.56-3.53 0-.54-.45-.99-.99-.99H4.19C3.65 3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0 .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z" />
                </svg>
                End
              </button>
              <span className="text-gray-400 text-[10px]">End</span>
            </div>
          )}
        </div>

        {/* Speech error */}
        {speechError && (
          <p className="mt-3 text-xs text-red-400 text-center">{speechError}</p>
        )}

        {false && (
          <p className="mt-2 text-xs text-red-400 text-center">{speechError}</p>
        )}
      </div>

      <div ref={messagesEndRef} />

      {showSelfReport && (
        <SelfReportModal candidateName={candidateName} onSubmit={handleSelfReportSubmit} />
      )}

      {/* Waveform keyframes */}
      <style>{`
        @keyframes wave1 { 0%,100%{transform:scaleY(0.4)} 50%{transform:scaleY(1)} }
        @keyframes wave2 { 0%,100%{transform:scaleY(0.7)} 50%{transform:scaleY(0.3)} }
        @keyframes wave3 { 0%,100%{transform:scaleY(1)} 50%{transform:scaleY(0.5)} }
        @keyframes wave4 { 0%,100%{transform:scaleY(0.3)} 50%{transform:scaleY(0.9)} }
        @keyframes wave5 { 0%,100%{transform:scaleY(0.6)} 50%{transform:scaleY(0.2)} }
      `}</style>
    </div>
  );
}
