import { useState, useCallback } from 'react';
import { InterviewFormat, Student } from '@/lib/types';
import AutocompleteInput from '@/components/AutocompleteInput';
import FormatCard from '@/components/FormatCard';

interface InterviewSetupFormProps {
  onStart: (config: { candidateName: string; netId: string; format: InterviewFormat }) => void;
}

export default function InterviewSetupForm({ onStart }: InterviewSetupFormProps) {
  const [candidateName, setCandidateName] = useState('');
  const [netId, setNetId] = useState('');
  const [selectedFormat, setSelectedFormat] = useState<InterviewFormat | null>(null);

  const canStart = candidateName.trim().length > 0 && netId.trim().length > 0 && selectedFormat !== null;

  const handleSelect = useCallback((student: Student) => {
    setCandidateName(student.name);
    setNetId(student.net_id);
  }, []);

  const handleStart = () => {
    if (!canStart) return;
    onStart({
      candidateName: candidateName.trim(),
      netId: netId.trim(),
      format: selectedFormat,
    });
  };

  return (
    <>
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
    </>
  );
}