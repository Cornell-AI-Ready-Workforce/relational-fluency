'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { InterviewSession, InterviewReport } from '@/lib/types';
import { ScoreCard } from '@/components/ScoreCard';
import { TranscriptView } from '@/components/TranscriptView';
import { saveInterview, saveReport, uploadAudio, saveAudioRecording } from '@/lib/db';
import { getAudioBlob, clearAudioBlob } from '@/lib/audioStore';

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
          <circle
            cx="50"
            cy="50"
            r="45"
            fill="none"
            stroke="#e5e7eb"
            strokeWidth="8"
          />
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

function LoadingState() {
  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center">
      <div className="text-center">
        <div className="w-16 h-16 border-4 border-purple-100 border-t-purple-600 rounded-full animate-spin mx-auto mb-6" />
        <h2 className="text-xl font-semibold text-gray-900 mb-2">Generating your report...</h2>
        <p className="text-gray-500 text-sm">
          Claude is analyzing your interview responses and generating personalized feedback.
        </p>
      </div>
    </div>
  );
}

export default function ReportPage() {
  const router = useRouter();
  const [report, setReport] = useState<InterviewReport | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isSaved, setIsSaved] = useState(false);
  const [savedInterviewId, setSavedInterviewId] = useState<string | null>(null);

  useEffect(() => {
    const sessionStr = sessionStorage.getItem('interviewSession');
    if (!sessionStr) {
      router.push('/');
      return;
    }

    const session: InterviewSession = JSON.parse(sessionStr);
    generateReport(session);
  }, [router]);

  const generateReport = async (session: InterviewSession) => {
    try {
      const response = await fetch('/api/report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(session),
      });

      if (!response.ok) {
        throw new Error('Failed to generate report');
      }

      const reportData: InterviewReport = await response.json();
      setReport(reportData);

      // Persist to Supabase in background
      persistToDatabase(session, reportData);
    } catch (err) {
      console.error('Error generating report:', err);
      setError('Failed to generate report. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const persistToDatabase = async (session: InterviewSession, reportData: InterviewReport) => {
    try {
      const interviewId = await saveInterview(session);
      setSavedInterviewId(interviewId);

      await saveReport(interviewId, reportData);

      // Upload audio if available
      const { blob: audioBlob, duration } = getAudioBlob();
      if (audioBlob) {
        try {
          const storagePath = await uploadAudio(interviewId, audioBlob);
          await saveAudioRecording(interviewId, storagePath, duration);
        } catch (audioErr) {
          console.error('Audio upload failed (non-fatal):', audioErr);
        }
        clearAudioBlob();
      }

      setIsSaved(true);
    } catch (dbErr) {
      console.error('Database save failed (non-fatal):', dbErr);
      // Don't surface this error to the user — report still shows
    }
  };

  const handleDownload = () => {
    if (!report) return;

    const lines: string[] = [
      'AI SOFT SKILLS INTERVIEW REPORT',
      '================================',
      '',
      `Candidate: ${report.candidateName}`,
      `Interview Format: ${report.format === 'star' ? 'STAR Behavioral Interview' : 'Role Play Scenarios'}`,
      `Date: ${new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })}`,
      `Overall Score: ${report.overallScore.toFixed(1)} / 5`,
      `Judge Model: ${report.judgeModel}`,
      '',
      'DIMENSION SCORES',
      '----------------',
    ];

    report.scores.forEach(s => {
      lines.push(`${s.name}: ${s.score}/5`);
      lines.push(`  Evidence: ${s.evidence}`);
      if (s.reasoning) {
        lines.push(`  Reasoning: ${s.reasoning}`);
      }
      lines.push('');
    });

    lines.push('SUMMARY');
    lines.push('-------');
    lines.push(report.summary);
    lines.push('');

    lines.push('STRENGTHS');
    lines.push('---------');
    report.strengths.forEach(s => lines.push(`• ${s}`));
    lines.push('');

    lines.push('AREAS FOR GROWTH');
    lines.push('----------------');
    report.areasForGrowth.forEach(s => lines.push(`• ${s}`));
    lines.push('');

    if (report.selfReport) {
      lines.push('SELF-ASSESSMENT');
      lines.push('---------------');
      lines.push(`Communication: ${report.selfReport.communication}/5`);
      lines.push(`Collaboration: ${report.selfReport.collaboration}/5`);
      lines.push(`Conflict Resolution: ${report.selfReport.conflictResolution}/5`);
      lines.push(`Adaptability: ${report.selfReport.adaptability}/5`);
      lines.push(`Reflection: ${report.selfReport.reflection}`);
      lines.push('');
    }

    lines.push('FULL TRANSCRIPT');
    lines.push('---------------');
    report.transcript
      .filter(m => m.content.trim())
      .forEach(m => {
        const role = m.role === 'interviewer' ? 'Interviewer' : 'Candidate';
        const cleanContent = m.content.replace(/\{"action":\s*"[^"]*"\}/g, '').trim();
        const time = new Date(m.timestamp).toLocaleTimeString();
        lines.push(`[${time}] ${role}:`);
        lines.push(cleanContent);
        lines.push('');
      });

    const blob = new Blob([lines.join('\n')], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `interview-report-${report.candidateName.replace(/\s+/g, '-').toLowerCase()}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleStartNew = () => {
    sessionStorage.removeItem('interviewConfig');
    sessionStorage.removeItem('interviewSession');
    router.push('/');
  };

  if (isLoading) return <LoadingState />;

  if (error) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center max-w-md">
          <div className="w-16 h-16 bg-red-100 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg className="w-8 h-8 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </div>
          <h2 className="text-xl font-semibold text-gray-900 mb-2">Report Generation Failed</h2>
          <p className="text-gray-500 text-sm mb-6">{error}</p>
          <button
            onClick={() => router.push('/')}
            className="px-6 py-3 bg-purple-600 text-white rounded-xl font-medium hover:bg-purple-700 transition-colors"
          >
            Start Over
          </button>
        </div>
      </div>
    );
  }

  if (!report) return null;

  const formatDate = new Date().toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  });

  const formatLabel = report.format === 'star' ? 'STAR Behavioral Interview' : 'Role Play Scenarios';

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-100 sticky top-0 z-10">
        <div className="max-w-4xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-purple-600 rounded-lg flex items-center justify-center">
              <svg className="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24">
                <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" />
                <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
              </svg>
            </div>
            <span className="font-semibold text-gray-900">Interview Report</span>
          </div>
          <div className="flex items-center gap-3">
            {isSaved && (
              <span className="text-xs text-emerald-600 font-medium flex items-center gap-1">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                Saved to database
              </span>
            )}
            <button
              onClick={handleDownload}
              className="flex items-center gap-2 px-4 py-2 border border-gray-200 text-gray-700 rounded-xl text-sm font-medium hover:bg-gray-50 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
              Download
            </button>
            <button
              onClick={handleStartNew}
              className="px-4 py-2 bg-purple-600 text-white rounded-xl text-sm font-medium hover:bg-purple-700 transition-colors"
            >
              New Interview
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-10 space-y-8">
        {/* Report Header */}
        <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
          <div className="flex items-start justify-between gap-6">
            <div>
              <h1 className="text-2xl font-bold text-gray-900">{report.candidateName}</h1>
              <div className="flex items-center gap-3 mt-2">
                <span className="inline-flex items-center px-2.5 py-1 bg-purple-100 text-purple-700 text-xs font-medium rounded-full">
                  {formatLabel}
                </span>
                <span className="text-sm text-gray-500">{formatDate}</span>
              </div>
            </div>
            <OverallScoreCircle score={report.overallScore} />
          </div>
        </div>

        {/* Summary */}
        <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-3">Assessment Summary</h2>
          <p className="text-gray-700 leading-relaxed">{report.summary}</p>
        </div>

        {/* Scores */}
        <div>
          <h2 className="text-lg font-semibold text-gray-900 mb-4">Dimension Scores</h2>
          <ScoreCard scores={report.scores} selfReport={report.selfReport} />
        </div>

        {/* Judge's Reasoning */}
        <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
          <div className="flex items-center justify-between mb-1">
            <h2 className="text-lg font-semibold text-gray-900">Judge&apos;s Reasoning</h2>
            {report.judgeModel && (
              <span className="text-xs font-mono bg-gray-100 text-gray-500 px-2.5 py-1 rounded-full">
                {report.judgeModel}
              </span>
            )}
          </div>
          <p className="text-sm text-gray-400 mb-5">
            Step-by-step chain-of-thought used to arrive at each score
          </p>
          <div className="space-y-4">
            {report.scores.map((dim) => (
              <details key={dim.key} className="group rounded-xl border border-gray-100 overflow-hidden">
                <summary className="flex items-center justify-between px-4 py-3 cursor-pointer select-none bg-gray-50 hover:bg-gray-100 transition-colors list-none">
                  <span className="font-medium text-gray-800 text-sm">{dim.name}</span>
                  <div className="flex items-center gap-3">
                    <span className="text-xs font-semibold text-purple-600">{dim.score}/5</span>
                    <svg className="w-4 h-4 text-gray-400 transition-transform group-open:rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </div>
                </summary>
                <div className="px-4 py-3 text-sm text-gray-600 leading-relaxed border-t border-gray-100 whitespace-pre-wrap">
                  {dim.reasoning}
                </div>
              </details>
            ))}
          </div>
        </div>

        {/* Strengths & Areas for Growth */}
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
              {report.areasForGrowth.map((area, i) => (
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

        {/* Self-report comparison (STAR only) */}
        {report.selfReport && (
          <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-2">Self-Assessment vs AI Evaluation</h2>
            <p className="text-sm text-gray-500 mb-5">How you rated yourself compared to the AI&apos;s assessment</p>
            <div className="space-y-4">
              {report.scores.map((dim) => {
                const selfScore = report.selfReport![dim.key];
                const diff = selfScore - dim.score;
                return (
                  <div key={dim.key} className="grid grid-cols-3 items-center gap-4">
                    <span className="text-sm font-medium text-gray-700">{dim.name}</span>
                    <div className="flex items-center gap-2">
                      <div className="flex-1 bg-gray-100 rounded-full h-2">
                        <div
                          className="h-2 rounded-full bg-violet-400"
                          style={{ width: `${(selfScore / 5) * 100}%` }}
                        />
                      </div>
                      <span className="text-xs text-gray-500 w-6 text-right">{selfScore}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="flex-1 bg-gray-100 rounded-full h-2">
                        <div
                          className="h-2 rounded-full bg-purple-500"
                          style={{ width: `${(dim.score / 5) * 100}%` }}
                        />
                      </div>
                      <span className="text-xs text-gray-500 w-6 text-right">{dim.score}</span>
                      {diff !== 0 && (
                        <span className={`text-xs font-medium ${diff > 0 ? 'text-amber-500' : 'text-emerald-500'}`}>
                          {diff > 0 ? `+${diff}` : diff}
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
              <div className="flex gap-4 text-xs text-gray-400 mt-2">
                <span className="flex items-center gap-1.5">
                  <span className="w-3 h-2 bg-violet-400 rounded-full inline-block" />
                  Your self-rating
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="w-3 h-2 bg-purple-500 rounded-full inline-block" />
                  AI evaluation
                </span>
              </div>
            </div>

            {report.selfReport.reflection && (
              <div className="mt-5 pt-5 border-t border-gray-100">
                <p className="text-sm font-medium text-gray-700 mb-2">Your personal reflection</p>
                <p className="text-sm text-gray-600 italic leading-relaxed">
                  &ldquo;{report.selfReport.reflection}&rdquo;
                </p>
              </div>
            )}
          </div>
        )}

        {/* Transcript */}
        <TranscriptView messages={report.transcript} />

        {/* Footer actions */}
        <div className="flex justify-center gap-4 pb-10 flex-wrap">
          <button
            onClick={handleDownload}
            className="flex items-center gap-2 px-6 py-3 border border-gray-200 text-gray-700 rounded-xl font-medium hover:bg-gray-50 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
            </svg>
            Download Report
          </button>
          {savedInterviewId && (
            <Link
              href="/history"
              className="flex items-center gap-2 px-6 py-3 border border-gray-200 text-gray-700 rounded-xl font-medium hover:bg-gray-50 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H7.83l4.88-4.88c.39-.39.39-1.03 0-1.42-.39-.39-1.02-.39-1.41 0l-6.59 6.59c-.39.39-.39 1.02 0 1.41l6.59 6.59c.39.39 1.02.39 1.41 0 .39-.39.39-1.02 0-1.41L7.83 13H19c.55 0 1-.45 1-1s-.45-1-1-1z" />
              </svg>
              View All Interviews
            </Link>
          )}
          <button
            onClick={handleStartNew}
            className="px-6 py-3 bg-purple-600 text-white rounded-xl font-medium hover:bg-purple-700 transition-colors"
          >
            Start New Interview
          </button>
        </div>
      </main>
    </div>
  );
}
