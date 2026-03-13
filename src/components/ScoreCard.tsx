'use client';

import React from 'react';
import { ScoreDimension, SelfReport } from '@/lib/types';

interface ScoreCardProps {
  scores: ScoreDimension[];
  selfReport?: SelfReport;
}

function ScoreBar({ score, maxScore = 5 }: { score: number; maxScore?: number }) {
  const percentage = (score / maxScore) * 100;
  const getBarColor = (s: number) => {
    if (s >= 4.5) return 'bg-emerald-500';
    if (s >= 3.5) return 'bg-indigo-500';
    if (s >= 2.5) return 'bg-yellow-500';
    return 'bg-red-500';
  };

  return (
    <div className="w-full bg-gray-100 rounded-full h-2.5">
      <div
        className={`h-2.5 rounded-full transition-all duration-700 ${getBarColor(score)}`}
        style={{ width: `${percentage}%` }}
      />
    </div>
  );
}

function ScoreLabel({ score }: { score: number }) {
  const getLabel = (s: number) => {
    if (s >= 4.5) return { text: 'Excellent', color: 'text-emerald-600' };
    if (s >= 3.5) return { text: 'Strong', color: 'text-indigo-600' };
    if (s >= 2.5) return { text: 'Developing', color: 'text-yellow-600' };
    return { text: 'Needs Work', color: 'text-red-600' };
  };
  const label = getLabel(score);
  return <span className={`text-xs font-medium ${label.color}`}>{label.text}</span>;
}

export function ScoreCard({ scores, selfReport }: ScoreCardProps) {
  return (
    <div className="space-y-6">
      {scores.map((dimension) => {
        const selfScore = selfReport ? selfReport[dimension.key] : null;

        return (
          <div key={dimension.key} className="bg-white rounded-xl border border-gray-100 p-5 shadow-sm">
            <div className="flex items-center justify-between mb-3">
              <div>
                <h3 className="font-semibold text-gray-900">{dimension.name}</h3>
                <ScoreLabel score={dimension.score} />
              </div>
              <div className="text-right">
                <span className="text-2xl font-bold text-gray-900">{dimension.score}</span>
                <span className="text-gray-400 text-sm">/5</span>
              </div>
            </div>

            <ScoreBar score={dimension.score} />

            {dimension.evidence && (
              <p className="mt-3 text-sm text-gray-600 leading-relaxed">{dimension.evidence}</p>
            )}

            {selfScore !== null && (
              <div className="mt-3 pt-3 border-t border-gray-50">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs text-gray-500">Self-rating</span>
                  <span className="text-xs font-medium text-gray-700">{selfScore}/5</span>
                </div>
                <div className="w-full bg-gray-100 rounded-full h-1.5">
                  <div
                    className="h-1.5 rounded-full bg-violet-400 transition-all duration-700"
                    style={{ width: `${(selfScore / 5) * 100}%` }}
                  />
                </div>
                <div className="flex justify-between mt-1">
                  <span className="text-xs text-gray-400">AI score: {dimension.score}</span>
                  <span className="text-xs text-gray-400">
                    {selfScore > dimension.score
                      ? `+${selfScore - dimension.score} vs AI`
                      : selfScore < dimension.score
                      ? `${selfScore - dimension.score} vs AI`
                      : 'Aligned with AI'}
                  </span>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
