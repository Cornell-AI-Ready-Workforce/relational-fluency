'use client';

import React from 'react';
import { Message } from '@/lib/types';

interface ChatBubbleProps {
  message: Message;
  isStreaming?: boolean;
}

function formatTimestamp(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function ChatBubble({ message, isStreaming = false }: ChatBubbleProps) {
  const isInterviewer = message.role === 'interviewer';

  // Strip JSON action signals from display
  const displayContent = message.content.replace(/\{"action":\s*"[^"]*"\}/g, '').trim();

  return (
    <div
      className={`flex w-full animate-fade-in ${isInterviewer ? 'justify-start' : 'justify-end'}`}
    >
      <div className={`flex items-end gap-2 max-w-[80%] ${isInterviewer ? 'flex-row' : 'flex-row-reverse'}`}>
        {/* Avatar */}
        <div
          className={`
            flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-white text-xs font-semibold
            ${isInterviewer ? 'bg-indigo-600' : 'bg-violet-500'}
          `}
        >
          {isInterviewer ? 'AI' : 'You'}
        </div>

        {/* Bubble */}
        <div
          className={`
            px-4 py-3 rounded-2xl shadow-sm
            ${isInterviewer
              ? 'bg-white border border-gray-100 rounded-tl-sm text-gray-800'
              : 'bg-indigo-600 rounded-tr-sm text-white'
            }
          `}
        >
          <p className="text-sm leading-relaxed whitespace-pre-wrap">
            {displayContent}
            {isStreaming && (
              <span className="inline-block w-1 h-4 ml-0.5 bg-current animate-pulse rounded-sm" />
            )}
          </p>
          <p
            className={`text-xs mt-1 ${isInterviewer ? 'text-gray-400' : 'text-indigo-200'}`}
          >
            {formatTimestamp(message.timestamp)}
          </p>
        </div>
      </div>
    </div>
  );
}
