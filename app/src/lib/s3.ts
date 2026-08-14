import { S3Client } from '@aws-sdk/client-s3';

// Server-side only. Credentials come from the AWS default chain:
// locally the CLI profile (~/.aws), on ECS/Fargate the task role —
// no access keys in env files or code.

export const REGION = process.env.AWS_REGION ?? 'us-east-1';
export const BUCKET = process.env.S3_BUCKET ?? '';

export const s3 = new S3Client({ region: REGION });

export function encounterKey(id: string, file: 'record.json' | 'recording.webm'): string {
  return `encounters/${id}/${file}`;
}
