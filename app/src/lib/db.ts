import { InterviewSession, InterviewReport, Message, ScoreDimension, SelfReport } from './types';

// Client-side data layer. All persistence goes through the app's API
// routes, which store one aligned record per encounter in S3.

export interface EncounterSummary {
  id: string;
  candidateName: string;
  format: string;
  savedAt: string;
  startTime: number;
  endTime: number;
}

export interface EncounterDetail {
  interview: { id: string; candidate_name: string; format: string; created_at: string };
  messages: Message[];
  report: {
    overall_score: number;
    summary: string;
    strengths: string[];
    areas_for_growth: string[];
    scores: ScoreDimension[];
    self_report: SelfReport | undefined;
  } | null;
  audio: { duration_seconds: number } | null;
  audioUrl: string | null;
}

export async function saveEncounter(
  session: InterviewSession,
  report: InterviewReport,
  audioBlob: Blob | null,
  durationSeconds: number
): Promise<string> {
  const form = new FormData();
  form.append('record', JSON.stringify({ session, report }));
  if (audioBlob) {
    form.append('audio', audioBlob, 'recording.webm');
    form.append('durationSeconds', String(durationSeconds));
  }

  const res = await fetch('/api/encounters', { method: 'POST', body: form });
  if (!res.ok) throw new Error('Failed to save encounter');
  const { id } = await res.json();
  return id;
}

export async function getInterviews(): Promise<EncounterSummary[]> {
  const res = await fetch('/api/encounters');
  if (!res.ok) throw new Error('Failed to load encounters');
  const { encounters } = await res.json();
  return encounters;
}

export async function getInterviewWithDetails(id: string): Promise<EncounterDetail> {
  const res = await fetch(`/api/encounters/${id}`);
  if (!res.ok) throw new Error('Failed to load encounter');
  const { record, audioUrl } = await res.json();

  return {
    interview: {
      id: record.id,
      candidate_name: record.session.candidateName,
      format: record.session.format,
      created_at: record.savedAt,
    },
    messages: record.session.messages,
    report: record.report
      ? {
          overall_score: record.report.overallScore,
          summary: record.report.summary,
          strengths: record.report.strengths,
          areas_for_growth: record.report.areasForGrowth,
          scores: record.report.scores,
          self_report: record.report.selfReport,
        }
      : null,
    audio: record.audio ? { duration_seconds: record.audio.durationSeconds } : null,
    audioUrl,
  };
}
