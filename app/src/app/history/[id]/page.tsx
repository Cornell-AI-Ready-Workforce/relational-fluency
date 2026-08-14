'use client';

import { useState, useEffect } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import { getInterviewWithDetails } from '@/lib/db';
import { ScoreCard } from '@/components/ScoreCard';
import { TranscriptView } from '@/components/TranscriptView';
import { ScoreDimension, SelfReport, Message } from '@/lib/types';

import type { EncounterDetail } from '@/lib/db';

type DetailData = EncounterDetail;

function OverallScoreCircle({ score }: { score: number }) {
  const percentage = (score / 5) * 100;
  const circumference = 2 * Math.PI * 45;
  const strokeDashoffset = circumference - (percentage / 100) * circumference;

  const getColor = (s: number) => {
    if (s >= 4.5) return '#10b981';
    if (s >= 3.5) return '#9333ea';
    if (s >= 2.5) return '#f59e0b';
    return '#ef4444';
  };

  const getLabel = (s: number) => {
    if (s >= 4.5) return 'Exceptional';
    if (s >= 3.5) return 'Strong';
    if (s >= 2.5) return 'Developing';
    return 'Needs Work';
  };

  return (
    <div className="flex flex-col items-center">
      <div className="relative w-32 h-32">
        <svg className="w-32 h-32 -rotate-90" viewBox="0 0 100 100">
          <circle cx="50" cy="50" r="45" fill="none" stroke="#e5e7eb" strokeWidth="8" />
          <circle
            cx="50"
            cy="50"
            r="45"
            fill="none"
            stroke={getColor(score)}
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={strokeDashoffset}
            className="transition-all duration-1000"
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-3xl font-bold text-gray-900">{score.toFixed(1)}</span>
          <span className="text-xs text-gray-400">out of 5</span>
        </div>
      </div>
      <span className="mt-2 text-sm font-semibold" style={{ color: getColor(score) }}>
        {getLabel(score)}
      </span>
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-4xl mx-auto px-6 py-10 space-y-6 animate-pulse">
        <div className="h-8 bg-gray-200 rounded w-1/3" />
        <div className="bg-white rounded-2xl p-6 space-y-4">
          <div className="h-6 bg-gray-200 rounded w-1/2" />
          <div className="h-4 bg-gray-200 rounded w-1/4" />
        </div>
        <div className="bg-white rounded-2xl p-6 space-y-3">
          <div className="h-4 bg-gray-200 rounded" />
          <div className="h-4 bg-gray-200 rounded w-5/6" />
          <div className="h-4 bg-gray-200 rounded w-4/6" />
        </div>
      </div>
    </div>
  );
}

export default function InterviewDetailPage() {
  const params = useParams();
  const id = params?.id as string;

  const [data, setData] = useState<DetailData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    async function load() {
      try {
        const result = await getInterviewWithDetails(id);
        setData(result);
      } catch (err) {
        console.error('Failed to load interview details:', err);
        setError('Could not load interview details.');
      } finally {
        setIsLoading(false);
      }
    }
    load();
  }, [id]);

  if (isLoading) return <LoadingSkeleton />;

  if (error || !data) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center max-w-md">
          <h2 className="text-xl font-semibold text-gray-900 mb-2">Interview Not Found</h2>
          <p className="text-gray-500 text-sm mb-6">{error ?? 'This interview could not be loaded.'}</p>
          <Link
            href="/history"
            className="px-6 py-3 bg-purple-600 text-white rounded-xl font-medium hover:bg-purple-700 transition-colors"
          >
            Back to History
          </Link>
        </div>
      </div>
    );
  }

  const { interview, messages, report, audio, audioUrl } = data;

  const formatLabel = interview.format === 'star' ? 'STAR Behavioral Interview' : 'Role Play Scenarios';
  const formattedDate = new Date(interview.created_at).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });

  // Audio is served via a short-lived signed S3 URL from the API
  const scores = (report?.scores ?? []) as ScoreDimension[];
  const selfReport = report?.self_report as SelfReport | undefined;
  const typedMessages = messages as Message[];

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-100 sticky top-0 z-10">
        <div className="max-w-4xl mx-auto px-6 py-4 flex items-center justify-between">
          <Link
            href="/history"
            className="flex items-center gap-2 text-sm text-gray-600 hover:text-gray-900 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            Back to History
          </Link>
          <Link
            href="/"
            className="px-4 py-2 bg-purple-600 text-white rounded-xl text-sm font-medium hover:bg-purple-700 transition-colors"
          >
            New Interview
          </Link>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-10 space-y-8">
        {/* Report Header */}
        <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
          <div className="flex items-start justify-between gap-6">
            <div>
              <h1 className="text-2xl font-bold text-gray-900">{interview.candidate_name}</h1>
              <div className="flex items-center gap-3 mt-2">
                <span className="inline-flex items-center px-2.5 py-1 bg-purple-100 text-purple-700 text-xs font-medium rounded-full">
                  {formatLabel}
                </span>
                <span className="text-sm text-gray-500">{formattedDate}</span>
              </div>
            </div>
            {report && <OverallScoreCircle score={report.overall_score} />}
          </div>
        </div>

        {/* Audio Player */}
        {audioUrl && (
          <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-3">Interview Recording</h2>
            <audio controls className="w-full" src={audioUrl}>
              Your browser does not support the audio element.
            </audio>
            {audio && (
              <p className="text-xs text-gray-400 mt-2">
                Duration: {Math.floor(audio.duration_seconds / 60)}m {audio.duration_seconds % 60}s
              </p>
            )}
          </div>
        )}

        {/* Summary */}
        {report && (
          <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-3">Assessment Summary</h2>
            <p className="text-gray-700 leading-relaxed">{report.summary}</p>
          </div>
        )}

        {/* Scores */}
        {report && scores.length > 0 && (
          <div>
            <h2 className="text-lg font-semibold text-gray-900 mb-4">Dimension Scores</h2>
            <ScoreCard scores={scores} selfReport={selfReport} />
          </div>
        )}

        {/* Strengths & Areas for Growth */}
        {report && (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
            <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
              <div className="flex items-center gap-2 mb-4">
                <div className="w-8 h-8 bg-emerald-100 rounded-lg flex items-center justify-center">
                  <svg className="w-4 h-4 text-emerald-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                  </svg>
                </div>
                <h2 className="text-base font-semibold text-gray-900">Key Strengths</h2>
              </div>
              <ul className="space-y-3">
                {report.strengths.map((strength, i) => (
                  <li key={i} className="flex gap-3 text-sm text-gray-700">
                    <span className="flex-shrink-0 w-5 h-5 bg-emerald-100 text-emerald-600 rounded-full flex items-center justify-center text-xs font-bold mt-0.5">
                      {i + 1}
                    </span>
                    {strength}
                  </li>
                ))}
              </ul>
            </div>

            <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
              <div className="flex items-center gap-2 mb-4">
                <div className="w-8 h-8 bg-amber-100 rounded-lg flex items-center justify-center">
                  <svg className="w-4 h-4 text-amber-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
                  </svg>
                </div>
                <h2 className="text-base font-semibold text-gray-900">Areas for Growth</h2>
              </div>
              <ul className="space-y-3">
                {report.areas_for_growth.map((area, i) => (
                  <li key={i} className="flex gap-3 text-sm text-gray-700">
                    <span className="flex-shrink-0 w-5 h-5 bg-amber-100 text-amber-600 rounded-full flex items-center justify-center text-xs font-bold mt-0.5">
                      {i + 1}
                    </span>
                    {area}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}

        {/* No report state */}
        {!report && (
          <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6 text-center text-gray-500 text-sm">
            No report was saved for this interview.
          </div>
        )}

        {/* Transcript */}
        {typedMessages.length > 0 && <TranscriptView messages={typedMessages} />}
      </main>
    </div>
  );
}
