import React from 'react';

export type InterviewFormat = 'star' | 'roleplay';

export interface FormatCardProps {
  format: InterviewFormat;
  title: string;
  description: string;
  duration: string;
  icon: React.ReactNode;
  selected: boolean;
  onSelect: (format: InterviewFormat) => void;
}

export interface Message {
  role: 'interviewer' | 'candidate';
  content: string;
  timestamp: number;
}

export interface InterviewSession {
  format: InterviewFormat;
  candidateName: string;
  netId: string;
  messages: Message[];
  selfReport?: SelfReport;
  startTime: number;
  endTime?: number;
}

export interface SelfReport {
  communication: number;
  collaboration: number;
  conflictResolution: number;
  adaptability: number;
  reflection: string;
}

export interface ScoreDimension {
  name: string;
  key: keyof Omit<SelfReport, 'reflection'>;
  score: number;
  evidence: string;
  reasoning: string;
}

export interface InterviewReport {
  candidateName: string;
  format: InterviewFormat;
  scores: ScoreDimension[];
  overallScore: number;
  summary: string;
  strengths: string[];
  areasForGrowth: string[];
  transcript: Message[];
  selfReport?: SelfReport;
  judgeModel: string;
}
