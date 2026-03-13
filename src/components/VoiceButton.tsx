'use client';

import React from 'react';

interface VoiceButtonProps {
  isListening: boolean;
  isProcessing: boolean;
  isDisabled: boolean;
  isSpeaking: boolean;
  onStart: () => void;
  onStop: () => void;
  transcript: string;
  interimTranscript: string;
  isSupported: boolean;
}

export function VoiceButton({
  isListening,
  isProcessing,
  isDisabled,
  isSpeaking,
  onStart,
  onStop,
  transcript,
  interimTranscript,
  isSupported,
}: VoiceButtonProps) {
  const handleClick = () => {
    if (isListening) {
      onStop();
    } else {
      onStart();
    }
  };

  const getButtonLabel = () => {
    if (isProcessing) return 'Processing...';
    if (isSpeaking) return 'Interviewer is speaking...';
    if (isListening) return 'Listening... Click to stop';
    return 'Click to speak';
  };

  const getButtonTitle = () => {
    if (!isSupported) return 'Voice input not supported';
    if (isSpeaking) return 'Wait for interviewer to finish';
    if (isProcessing) return 'Processing your response';
    if (isListening) return 'Click to stop recording';
    return 'Click to start speaking';
  };

  return (
    <div className="flex flex-col items-center gap-3">
      {/* Transcript preview — show accumulated finals + live interim */}
      {isListening && (transcript || interimTranscript) && (
        <div className="max-w-md px-4 py-2 bg-white border border-indigo-200 rounded-xl text-sm text-gray-700 text-center">
          {transcript}
          {interimTranscript && (
            <span className="text-gray-400 italic"> {interimTranscript}</span>
          )}
        </div>
      )}

      <div className="relative flex items-center justify-center">
        {/* Pulsing ring when listening */}
        {isListening && (
          <>
            <span className="absolute inline-flex h-20 w-20 rounded-full bg-red-400 opacity-30 animate-ping" />
            <span className="absolute inline-flex h-24 w-24 rounded-full bg-red-300 opacity-20 animate-ping" style={{ animationDelay: '0.2s' }} />
          </>
        )}

        <button
          onClick={handleClick}
          disabled={isDisabled || isProcessing || isSpeaking}
          title={getButtonTitle()}
          className={`
            relative z-10 flex items-center justify-center w-16 h-16 rounded-full
            transition-all duration-200 shadow-lg
            focus:outline-none focus:ring-4 focus:ring-offset-2
            ${isListening
              ? 'bg-red-500 hover:bg-red-600 focus:ring-red-300 scale-110'
              : isProcessing || isSpeaking
              ? 'bg-gray-300 cursor-not-allowed scale-100'
              : isDisabled
              ? 'bg-gray-200 cursor-not-allowed'
              : 'bg-indigo-600 hover:bg-indigo-700 focus:ring-indigo-300 hover:scale-105 active:scale-95'
            }
          `}
        >
          {isProcessing ? (
            <svg
              className="w-7 h-7 text-white animate-spin"
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
            >
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
              />
            </svg>
          ) : isListening ? (
            <svg className="w-7 h-7 text-white" fill="currentColor" viewBox="0 0 24 24">
              <rect x="6" y="4" width="4" height="16" rx="1" />
              <rect x="14" y="4" width="4" height="16" rx="1" />
            </svg>
          ) : isSpeaking ? (
            <svg className="w-7 h-7 text-gray-500" fill="currentColor" viewBox="0 0 24 24">
              <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z" />
            </svg>
          ) : (
            <svg className="w-7 h-7 text-white" fill="currentColor" viewBox="0 0 24 24">
              <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" />
              <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
            </svg>
          )}
        </button>
      </div>

      <p className="text-sm text-gray-500 text-center">{getButtonLabel()}</p>
    </div>
  );
}
