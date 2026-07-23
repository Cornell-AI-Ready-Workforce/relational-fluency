'use client';

import React, { useState } from 'react';
import { Message } from '@/lib/types';

interface TranscriptViewProps {
  messages: Message[];
}

function formatTimestamp(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function TranscriptView({ messages }: TranscriptViewProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  const displayMessages = messages.filter(m => m.content.trim() !== '');

  return (
    <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between p-5 hover:bg-gray-50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-gray-100 rounded-lg flex items-center justify-center">
            <svg className="w-4 h-4 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
          </div>
          <div className="text-left">
            <h3 className="font-semibold text-gray-900">Full Transcript</h3>
            <p className="text-sm text-gray-500">{displayMessages.length} messages</p>
          </div>
        </div>
        <svg
          className={`w-5 h-5 text-gray-400 transition-transform ${isExpanded ? 'rotate-180' : ''}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {isExpanded && (
        <div className="border-t border-gray-100 max-h-96 overflow-y-auto p-5 space-y-4">
          {displayMessages.map((message, index) => {
            const isInterviewer = message.role === 'interviewer';
            const displayContent = message.content.replace(/\{"action":\s*"[^"]*"\}/g, '').trim();

            return (
              <div key={index} className={`flex gap-3 ${isInterviewer ? '' : 'flex-row-reverse'}`}>
                <div
                  className={`
                    flex-shrink-0 w-7 h-7 rounded-full flex items-center justify-center text-white text-xs font-semibold
                    ${isInterviewer ? 'bg-indigo-600' : 'bg-violet-500'}
                  `}
                >
                  {isInterviewer ? 'AI' : 'You'}
                </div>
                <div className={`flex-1 ${isInterviewer ? '' : 'text-right'}`}>
                  <div className="flex items-center gap-2 mb-1">
                    {!isInterviewer && <span className="flex-1" />}
                    <span className="text-xs font-medium text-gray-700">
                      {isInterviewer ? 'Interviewer' : 'Candidate'}
                    </span>
                    <span className="text-xs text-gray-400">{formatTimestamp(message.timestamp)}</span>
                  </div>
                  <p className="text-sm text-gray-700 leading-relaxed">{displayContent}</p>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
