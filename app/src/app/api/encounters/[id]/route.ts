import { NextRequest, NextResponse } from 'next/server';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { s3, BUCKET, encounterKey } from '@/lib/s3';
import type { EncounterRecord } from '../route';

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  try {
    if (!BUCKET) {
      return NextResponse.json({ error: 'S3_BUCKET is not configured' }, { status: 500 });
    }

    const { id } = await params;
    if (!/^[0-9a-f-]{36}$/i.test(id)) {
      return NextResponse.json({ error: 'Invalid encounter id' }, { status: 400 });
    }

    let record: EncounterRecord;
    try {
      const obj = await s3.send(new GetObjectCommand({
        Bucket: BUCKET,
        Key: encounterKey(id, 'record.json'),
      }));
      record = JSON.parse(await obj.Body!.transformToString());
    } catch {
      return NextResponse.json({ error: 'Encounter not found' }, { status: 404 });
    }

    // The bucket is private; the recording is served via a short-lived signed URL
    let audioUrl: string | null = null;
    if (record.audio?.key) {
      audioUrl = await getSignedUrl(
        s3,
        new GetObjectCommand({ Bucket: BUCKET, Key: record.audio.key }),
        { expiresIn: 3600 }
      );
    }

    return NextResponse.json({ record, audioUrl });
  } catch (error) {
    console.error('Encounter detail error:', error);
    return NextResponse.json({ error: 'Could not load encounter' }, { status: 500 });
  }
}
