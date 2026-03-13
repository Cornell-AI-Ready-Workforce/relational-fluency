import { supabase } from './supabase';
import { InterviewSession, InterviewReport } from './types';

export async function saveInterview(session: InterviewSession): Promise<string> {
  const { data: interviewData, error: interviewError } = await supabase
    .from('interviews')
    .insert({
      candidate_name: session.candidateName,
      net_id: session.netId ?? null,
      format: session.format,
      start_time: session.startTime,
      end_time: session.endTime ?? null,
    })
    .select('id')
    .single();

  if (interviewError) throw interviewError;
  const interviewId: string = interviewData.id;

  if (session.messages.length > 0) {
    const messagesPayload = session.messages.map((m) => ({
      interview_id: interviewId,
      role: m.role,
      content: m.content,
      timestamp: m.timestamp,
    }));

    const { error: messagesError } = await supabase
      .from('messages')
      .insert(messagesPayload);

    if (messagesError) throw messagesError;
  }

  return interviewId;
}

export async function saveReport(interviewId: string, report: InterviewReport): Promise<void> {
  const { error } = await supabase.from('reports').insert({
    interview_id: interviewId,
    overall_score: report.overallScore,
    summary: report.summary,
    strengths: report.strengths,
    areas_for_growth: report.areasForGrowth,
    scores: report.scores,
    self_report: report.selfReport ?? null,
    judge_model: report.judgeModel,
  });

  if (error) throw error;
}

export async function saveAudioRecording(
  interviewId: string,
  storagePath: string,
  durationSeconds: number
): Promise<void> {
  const { error } = await supabase.from('audio_recordings').insert({
    interview_id: interviewId,
    storage_path: storagePath,
    duration_seconds: durationSeconds,
  });

  if (error) throw error;
}

export async function uploadAudio(interviewId: string, audioBlob: Blob): Promise<string> {
  const path = `${interviewId}/recording.webm`;

  const { error } = await supabase.storage
    .from('interview-audio')
    .upload(path, audioBlob, {
      contentType: audioBlob.type || 'audio/webm',
      upsert: true,
    });

  if (error) throw error;

  return path;
}

export async function getInterviews(): Promise<
  Array<{
    id: string;
    candidate_name: string;
    format: string;
    created_at: string;
    end_time: number;
    start_time: number;
  }>
> {
  const { data, error } = await supabase
    .from('interviews')
    .select('id, candidate_name, net_id, format, created_at, start_time, end_time')
    .order('created_at', { ascending: false });

  if (error) throw error;
  return data ?? [];
}

export async function getInterviewWithDetails(interviewId: string): Promise<{
  interview: { id: string; candidate_name: string; format: string; created_at: string };
  messages: Array<{ role: string; content: string; timestamp: number }>;
  report: {
    overall_score: number;
    summary: string;
    strengths: string[];
    areas_for_growth: string[];
    scores: unknown;
    self_report: unknown;
  } | null;
  audio: { storage_path: string; duration_seconds: number } | null;
}> {
  const [interviewResult, messagesResult, reportResult, audioResult] = await Promise.all([
    supabase
      .from('interviews')
      .select('id, candidate_name, net_id, format, created_at')
      .eq('id', interviewId)
      .single(),
    supabase
      .from('messages')
      .select('role, content, timestamp')
      .eq('interview_id', interviewId)
      .order('timestamp', { ascending: true }),
    supabase
      .from('reports')
      .select('overall_score, summary, strengths, areas_for_growth, scores, self_report, judge_model')
      .eq('interview_id', interviewId)
      .maybeSingle(),
    supabase
      .from('audio_recordings')
      .select('storage_path, duration_seconds')
      .eq('interview_id', interviewId)
      .maybeSingle(),
  ]);

  if (interviewResult.error) throw interviewResult.error;

  return {
    interview: interviewResult.data,
    messages: messagesResult.data ?? [],
    report: reportResult.data ?? null,
    audio: audioResult.data ?? null,
  };
}
