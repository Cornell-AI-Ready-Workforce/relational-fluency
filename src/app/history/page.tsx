'use client';

import { useState, useEffect } from 'react';
import Link from 'next/link';
import { getInterviews } from '@/lib/db';

interface InterviewRow {
  id: string;
  candidate_name: string;
  format: string;
  created_at: string;
  start_time: number;
  end_time: number;
}

function formatDuration(startTime: number, endTime: number): string {
  if (!startTime || !endTime) return '—';
  const seconds = Math.floor((endTime - startTime) / 1000);
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}

function FormatBadge({ format }: { format: string }) {
  const isStar = format === 'star';
  return (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
        isStar ? 'bg-purple-100 text-purple-700' : 'bg-violet-100 text-violet-700'
      }`}
    >
      {isStar ? 'STAR' : 'Role Play'}
    </span>
  );
}

function SkeletonRow() {
  return (
    <tr className="animate-pulse">
      {[...Array(5)].map((_, i) => (
        <td key={i} className="px-6 py-4">
          <div className="h-4 bg-gray-200 rounded w-3/4" />
        </td>
      ))}
    </tr>
  );
}

export default function HistoryPage() {
  const [interviews, setInterviews] = useState<InterviewRow[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        const data = await getInterviews();
        setInterviews(data as InterviewRow[]);
      } catch (err) {
        console.error('Failed to load interviews:', err);
        setError('Could not load interview history. Please try again.');
      } finally {
        setIsLoading(false);
      }
    }
    load();
  }, []);

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-100 sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-purple-600 rounded-lg flex items-center justify-center">
              <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H7.83l4.88-4.88c.39-.39.39-1.03 0-1.42-.39-.39-1.02-.39-1.41 0l-6.59 6.59c-.39.39-.39 1.02 0 1.41l6.59 6.59c.39.39 1.02.39 1.41 0 .39-.39.39-1.02 0-1.41L7.83 13H19c.55 0 1-.45 1-1s-.45-1-1-1z" />
              </svg>
            </div>
            <h1 className="font-semibold text-gray-900 text-lg">Interview History</h1>
          </div>
          <Link
            href="/"
            className="px-4 py-2 bg-purple-600 text-white rounded-xl text-sm font-medium hover:bg-purple-700 transition-colors"
          >
            New Interview
          </Link>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-10">
        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 rounded-xl px-5 py-4 mb-6 text-sm">
            {error}
          </div>
        )}

        {!isLoading && !error && interviews.length === 0 && (
          <div className="text-center py-24">
            <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mx-auto mb-4">
              <svg className="w-8 h-8 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
            </div>
            <h2 className="text-lg font-semibold text-gray-900 mb-2">No interviews yet</h2>
            <p className="text-gray-500 text-sm mb-6">
              Complete an interview and it will appear here.
            </p>
            <Link
              href="/"
              className="px-6 py-3 bg-purple-600 text-white rounded-xl font-medium hover:bg-purple-700 transition-colors"
            >
              Start Your First Interview
            </Link>
          </div>
        )}

        {(isLoading || interviews.length > 0) && (
          <div className="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-100">
                  <th className="px-6 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
                    Candidate
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
                    Format
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
                    Date
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
                    Duration
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {isLoading
                  ? [...Array(5)].map((_, i) => <SkeletonRow key={i} />)
                  : interviews.map((interview) => (
                      <tr key={interview.id} className="hover:bg-gray-50 transition-colors">
                        <td className="px-6 py-4">
                          <span className="font-medium text-gray-900 text-sm">
                            {interview.candidate_name}
                          </span>
                        </td>
                        <td className="px-6 py-4">
                          <FormatBadge format={interview.format} />
                        </td>
                        <td className="px-6 py-4 text-sm text-gray-500">
                          {new Date(interview.created_at).toLocaleDateString('en-US', {
                            year: 'numeric',
                            month: 'short',
                            day: 'numeric',
                          })}
                        </td>
                        <td className="px-6 py-4 text-sm text-gray-500">
                          {formatDuration(interview.start_time, interview.end_time)}
                        </td>
                        <td className="px-6 py-4">
                          <Link
                            href={`/history/${interview.id}`}
                            className="text-sm text-purple-600 hover:text-purple-800 font-medium"
                          >
                            View Report
                          </Link>
                        </td>
                      </tr>
                    ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  );
}
