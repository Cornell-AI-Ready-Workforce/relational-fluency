'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { InterviewFormat } from '@/lib/types';
import { supabase } from '@/lib/supabase';

interface Student {
  name: string;
  net_id: string;
}

// ── Autocomplete input ──────────────────────────────────────────────────────

interface AutocompleteInputProps {
  label: string;
  placeholder: string;
  value: string;
  onChange: (value: string) => void;
  onSelect: (student: Student) => void;
  searchField: 'name' | 'net_id';
  onKeyDown?: (e: React.KeyboardEvent) => void;
}

function AutocompleteInput({
  label,
  placeholder,
  value,
  onChange,
  onSelect,
  searchField,
  onKeyDown,
}: AutocompleteInputProps) {
  const [results, setResults] = useState<Student[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const containerRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const search = useCallback(async (query: string) => {
    if (query.trim().length < 1) {
      setResults([]);
      setIsOpen(false);
      return;
    }
    setLoading(true);
    try {
      const { data, error } = await supabase
        .from('students')
        .select('name, net_id')
        .ilike(searchField, `${query}%`)
        .limit(8);
      if (!error && data) {
        setResults(data);
        setIsOpen(data.length > 0);
      }
    } finally {
      setLoading(false);
    }
  }, [searchField]);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(value), 200);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [value, search]);

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (isOpen) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveIndex(i => Math.min(i + 1, results.length - 1));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveIndex(i => Math.max(i - 1, 0));
        return;
      }
      if (e.key === 'Enter' && activeIndex >= 0) {
        e.preventDefault();
        onSelect(results[activeIndex]);
        setIsOpen(false);
        setActiveIndex(-1);
        return;
      }
      if (e.key === 'Escape') {
        setIsOpen(false);
        return;
      }
    }
    onKeyDown?.(e);
  };

  return (
    <div ref={containerRef} className="relative">
      <label className="block text-sm font-medium text-gray-700 mb-2">{label}</label>
      <div className="relative">
        <input
          type="text"
          value={value}
          onChange={e => { onChange(e.target.value); setActiveIndex(-1); }}
          onFocus={() => { if (results.length > 0) setIsOpen(true); }}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          autoComplete="off"
          className="w-full px-4 py-3 rounded-xl border border-gray-200 bg-white text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-purple-500 focus:border-transparent transition-all pr-10"
        />
        {loading && (
          <div className="absolute right-3 top-1/2 -translate-y-1/2">
            <svg className="w-4 h-4 text-gray-400 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
            </svg>
          </div>
        )}
      </div>

      {isOpen && results.length > 0 && (
        <ul className="absolute z-50 mt-1 w-full bg-white border border-gray-200 rounded-xl shadow-lg overflow-hidden">
          {results.map((student, i) => (
            <li
              key={student.net_id}
              onMouseDown={() => { onSelect(student); setIsOpen(false); setActiveIndex(-1); }}
              onMouseEnter={() => setActiveIndex(i)}
              className={`flex items-center justify-between px-4 py-2.5 cursor-pointer text-sm transition-colors ${
                i === activeIndex ? 'bg-purple-50 text-purple-900' : 'text-gray-800 hover:bg-gray-50'
              }`}
            >
              <span className="font-medium">{student.name}</span>
              <span className="text-xs text-gray-400 font-mono">{student.net_id}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Format card ─────────────────────────────────────────────────────────────

interface FormatCardProps {
  format: InterviewFormat;
  title: string;
  description: string;
  duration: string;
  icon: React.ReactNode;
  selected: boolean;
  onSelect: (format: InterviewFormat) => void;
}

function FormatCard({ format, title, description, duration, icon, selected, onSelect }: FormatCardProps) {
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

// ── Home page ────────────────────────────────────────────────────────────────

export default function HomePage() {
  const [candidateName, setCandidateName] = useState('');
  const [netId, setNetId] = useState('');
  const [selectedFormat, setSelectedFormat] = useState<InterviewFormat | null>(null);
  const router = useRouter();

  const canStart = candidateName.trim().length > 0 && netId.trim().length > 0 && selectedFormat !== null;

  const handleSelect = (student: Student) => {
    setCandidateName(student.name);
    setNetId(student.net_id);
  };

  const handleStart = () => {
    if (!canStart) return;
    sessionStorage.setItem('interviewConfig', JSON.stringify({
      candidateName: candidateName.trim(),
      netId: netId.trim(),
      format: selectedFormat,
    }));
    router.push('/interview');
  };

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="border-b border-gray-100 bg-white/80 backdrop-blur-sm sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center gap-3">
          <div className="w-8 h-8 bg-purple-600 rounded-lg flex items-center justify-center">
            <svg className="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24">
              <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" />
              <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
            </svg>
          </div>
          <span className="font-semibold text-gray-900">AI Soft Skills Interviewer</span>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-6 py-16">
        {/* Hero */}
        <div className="text-center mb-14">
          <h1 className="text-4xl font-bold text-gray-900 mb-4 leading-tight">
            Practice Your Teamwork<br />
            <span className="text-purple-600">Soft Skills Interview</span>
          </h1>
          <p className="text-lg text-gray-500 max-w-xl mx-auto leading-relaxed">
            Get personalized feedback on your communication, collaboration, conflict resolution,
            and adaptability in a realistic AI-powered interview session.
          </p>
        </div>

        {/* How it works */}
        <div className="grid grid-cols-3 gap-4 mb-14">
          {[
            { step: '1', label: 'Choose your format', desc: 'STAR behavioral or role-play scenarios' },
            { step: '2', label: 'Answer with voice', desc: 'Speak naturally, AI listens and responds' },
            { step: '3', label: 'Get your report', desc: 'Detailed scorecard and actionable insights' },
          ].map(({ step, label, desc }) => (
            <div key={step} className="text-center">
              <div className="w-10 h-10 bg-purple-600 text-white rounded-full flex items-center justify-center mx-auto mb-3 font-bold text-lg">
                {step}
              </div>
              <p className="font-medium text-gray-800 text-sm mb-1">{label}</p>
              <p className="text-xs text-gray-500">{desc}</p>
            </div>
          ))}
        </div>

        {/* Name + NetID with autocomplete */}
        <div className="mb-8 grid grid-cols-1 sm:grid-cols-2 gap-4">
          <AutocompleteInput
            label="Your name"
            placeholder="Start typing your name..."
            value={candidateName}
            onChange={setCandidateName}
            onSelect={handleSelect}
            searchField="name"
            onKeyDown={(e) => { if (e.key === 'Enter' && canStart) handleStart(); }}
          />
          <AutocompleteInput
            label="Your NetID"
            placeholder="e.g. abc123"
            value={netId}
            onChange={setNetId}
            onSelect={handleSelect}
            searchField="net_id"
            onKeyDown={(e) => { if (e.key === 'Enter' && canStart) handleStart(); }}
          />
        </div>

        {/* Format Selection */}
        <div className="mb-10">
          <label className="block text-sm font-medium text-gray-700 mb-3">
            Choose your interview format
          </label>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <FormatCard
              format="star"
              title="STAR Behavioral Interview"
              description="Answer structured behavioral questions using the Situation, Task, Action, Result framework. Great for practicing storytelling and self-reflection."
              duration="15–20 minutes"
              icon={
                <svg className="w-6 h-6 text-purple-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
                </svg>
              }
              selected={selectedFormat === 'star'}
              onSelect={setSelectedFormat}
            />
            <FormatCard
              format="roleplay"
              title="Role Play Scenarios"
              description="Navigate realistic workplace situations in real-time conversation. The AI plays your colleague or manager — handle conflict, disagreements, and pressure."
              duration="10–15 minutes"
              icon={
                <svg className="w-6 h-6 text-violet-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
              }
              selected={selectedFormat === 'roleplay'}
              onSelect={setSelectedFormat}
            />
          </div>
        </div>

        {/* Start Button */}
        <button
          onClick={handleStart}
          disabled={!canStart}
          className={`
            w-full py-4 rounded-2xl font-semibold text-lg transition-all duration-200
            ${canStart
              ? 'bg-purple-600 hover:bg-purple-700 text-white shadow-lg hover:shadow-xl hover:-translate-y-0.5 active:translate-y-0'
              : 'bg-gray-100 text-gray-400 cursor-not-allowed'
            }
          `}
        >
          {canStart ? `Start Interview as ${candidateName}` : 'Enter your name and NetID to begin'}
        </button>

        <p className="text-center text-xs text-gray-400 mt-4">
          Your interview will be conducted entirely in your browser. Voice is processed locally.
        </p>

        <div className="text-center mt-6">
          <Link
            href="/history"
            className="text-sm text-gray-500 hover:text-purple-600 transition-colors"
          >
            View Past Interviews &rarr;
          </Link>
        </div>
      </main>
    </div>
  );
}
