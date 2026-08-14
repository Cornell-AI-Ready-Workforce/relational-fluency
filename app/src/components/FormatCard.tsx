import React from 'react';
import { InterviewFormat } from '@/lib/types';

interface FormatCardProps {
  format: InterviewFormat;
  title: string;
  description: string;
  duration: string;
  icon: React.ReactNode;
  selected: boolean;
  onSelect: (format: InterviewFormat) => void;
}

export default function FormatCard({ format, title, description, duration, icon, selected, onSelect }: FormatCardProps) {
  return (
    <button
      onClick={() => onSelect(format)}
      className={`
        relative text-left p-6 rounded-2xl border-2 transition-all duration-200 cursor-pointer w-full
        ${selected
          ? 'border-purple-500 bg-purple-50 shadow-md'
          : 'border-gray-200 bg-white hover:border-purple-300 hover:shadow-sm'
        }
      `}
    >
      {selected && (
        <div className="absolute top-4 right-4 w-6 h-6 bg-purple-600 rounded-full flex items-center justify-center">
          <svg className="w-3.5 h-3.5 text-white" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
          </svg>
        </div>
      )}
      <div className={`w-12 h-12 rounded-xl flex items-center justify-center mb-4 ${selected ? 'bg-purple-100' : 'bg-gray-100'}`}>
        {icon}
      </div>
      <h3 className={`font-semibold text-lg mb-2 ${selected ? 'text-purple-900' : 'text-gray-900'}`}>
        {title}
      </h3>
      <p className={`text-sm leading-relaxed mb-3 ${selected ? 'text-purple-700' : 'text-gray-500'}`}>
        {description}
      </p>
      <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${selected ? 'bg-purple-200 text-purple-700' : 'bg-gray-100 text-gray-500'}`}>
        {duration}
      </span>
    </button>
  );
}