import { NextRequest, NextResponse } from 'next/server';
import { randomUUID } from 'crypto';
import {
  PutObjectCommand,
  GetObjectCommand,
  ListObjectsV2Command,
} from '@aws-sdk/client-s3';
import { s3, BUCKET, encounterKey } from '@/lib/s3';
import { InterviewSession, InterviewReport } from '@/lib/types';

// Study data lives in S3 as one aligned record per encounter:
//   encounters/{id}/record.json     — session + transcript + report + self-report
//   encounters/{id}/recording.webm  — two-sided session audio

export interface EncounterRecord {
  id: string;
  savedAt: string;
  session: InterviewSession;
  report: InterviewReport;
  audio: { key: string; durationSeconds: number } | null;
}

export async function POST(request: NextRequest) {
  try {
    if (!BUCKET) {
      return NextResponse.json({ error: 'S3_BUCKET is not configured' }, { status: 500 });
    }

    const form = await request.formData();
    const recordRaw = form.get('record');
    if (typeof recordRaw !== 'string') {
      return NextResponse.json({ error: 'Missing record payload' }, { status: 400 });
    }
    const { session, report } = JSON.parse(recordRaw) as {
      session: InterviewSession;
      report: InterviewReport;
    };
    if (!session?.messages || !report) {
      return NextResponse.json({ error: 'Malformed record payload' }, { status: 400 });
    }

    const id = randomUUID();

    const audio = form.get('audio');
    const durationSeconds = Number(form.get('durationSeconds') ?? 0);
    let audioMeta: EncounterRecord['audio'] = null;

    if (audio instanceof Blob && audio.size > 0) {
      const audioKey = encounterKey(id, 'recording.webm');
      await s3.send(new PutObjectCommand({
        Bucket: BUCKET,
        Key: audioKey,
        Body: Buffer.from(await audio.arrayBuffer()),
        ContentType: audio.type || 'audio/webm',
      }));
      audioMeta = { key: audioKey, durationSeconds };
    }

    const record: EncounterRecord = {
      id,
      savedAt: new Date().toISOString(),
      session,
      report,
      audio: audioMeta,
    };

    await s3.send(new PutObjectCommand({
      Bucket: BUCKET,
      Key: encounterKey(id, 'record.json'),
      Body: JSON.stringify(record),
      ContentType: 'application/json',
    }));

    return NextResponse.json({ id });
  } catch (error) {
    console.error('Encounter save error:', error);
    return NextResponse.json({ error: 'Could not save encounter' }, { status: 500 });
  }
}

export async function GET() {
  try {
    if (!BUCKET) {
      return NextResponse.json({ error: 'S3_BUCKET is not configured' }, { status: 500 });
    }

    const keys: string[] = [];
    let continuationToken: string | undefined;
    do {
      const page = await s3.send(new ListObjectsV2Command({
        Bucket: BUCKET,
        Prefix: 'encounters/',
        ContinuationToken: continuationToken,
      }));
      for (const obj of page.Contents ?? []) {
        if (obj.Key?.endsWith('/record.json')) keys.push(obj.Key);
      }
      continuationToken = page.IsTruncated ? page.NextContinuationToken : undefined;
    } while (continuationToken);

    const records = await Promise.all(
      keys.map(async (key) => {
        try {
          const obj = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: key }));
          const body = await obj.Body!.transformToString();
          return JSON.parse(body) as EncounterRecord;
        } catch {
          return null;
        }
      })
    );

    const summaries = records
      .filter((r): r is EncounterRecord => r !== null)
      .map((r) => ({
        id: r.id,
        candidateName: r.session.candidateName,
        format: r.session.format,
        savedAt: r.savedAt,
        startTime: r.session.startTime,
        endTime: r.session.endTime ?? 0,
      }))
      .sort((a, b) => (a.savedAt < b.savedAt ? 1 : -1));

    return NextResponse.json({ encounters: summaries });
  } catch (error) {
    console.error('Encounter list error:', error);
    return NextResponse.json({ error: 'Could not list encounters' }, { status: 500 });
  }
}
