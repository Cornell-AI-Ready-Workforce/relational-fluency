'use client';

import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { InterviewFormat } from '@/lib/types';
import InterviewSetupForm from '@/components/InterviewSetupForm';

// ── Home page ────────────────────────────────────────────────────────────────

export default function HomePage() {
  const router = useRouter();

  const handleStart = (config: { candidateName: string; netId: string; format: InterviewFormat }) => {
    sessionStorage.setItem('interviewConfig', JSON.stringify(config));
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

        {/* Interview Setup Form */}
        <InterviewSetupForm onStart={handleStart} />

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
