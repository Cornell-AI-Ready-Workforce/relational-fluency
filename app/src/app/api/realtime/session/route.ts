import { NextRequest, NextResponse } from 'next/server';
import { InterviewFormat } from '@/lib/types';
import { getRealtimeInstructions } from '@/lib/prompts';

// Mints an ephemeral client secret for the OpenAI Realtime API so the
// browser can connect over WebRTC without ever seeing the real API key.
//
// OPENAI_BASE_URL lets the same route target a compatible gateway
// (e.g. the Cornell LiteLLM proxy) instead of api.openai.com.

const BASE_URL = (process.env.OPENAI_BASE_URL ?? 'https://api.openai.com').replace(/\/$/, '');
const MODEL = process.env.OPENAI_REALTIME_MODEL ?? 'gpt-realtime';
const VOICE = process.env.OPENAI_REALTIME_VOICE ?? 'marin';

export async function POST(request: NextRequest) {
  try {
    const { format, candidateName } = (await request.json()) as {
      format: InterviewFormat;
      candidateName: string;
    };

    if (!format || !candidateName) {
      return NextResponse.json({ error: 'Missing format or candidateName' }, { status: 400 });
    }
    if (!process.env.OPENAI_API_KEY) {
      return NextResponse.json({ error: 'OPENAI_API_KEY is not configured' }, { status: 500 });
    }

    const response = await fetch(`${BASE_URL}/v1/realtime/client_secrets`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        session: {
          type: 'realtime',
          model: MODEL,
          instructions: getRealtimeInstructions(format, candidateName),
          audio: {
            input: {
              transcription: { model: 'whisper-1' },
            },
            output: { voice: VOICE },
          },
          tools: [
            {
              type: 'function',
              name: 'complete_interview',
              description:
                'Call this once, after you have spoken your closing remarks, to formally end the interview session.',
              parameters: { type: 'object', properties: {}, required: [] },
            },
          ],
        },
      }),
    });

    if (!response.ok) {
      const err = await response.text();
      console.error('Realtime session error:', err);
      return NextResponse.json({ error: 'Could not create realtime session' }, { status: 502 });
    }

    const data = await response.json();
    const clientSecret: string | undefined = data.value ?? data.client_secret?.value;
    if (!clientSecret) {
      console.error('Unexpected client secret payload:', JSON.stringify(data).slice(0, 300));
      return NextResponse.json({ error: 'Malformed realtime session response' }, { status: 502 });
    }

    return NextResponse.json({
      clientSecret,
      model: MODEL,
      callsUrl: `${BASE_URL}/v1/realtime/calls`,
    });
  } catch (error) {
    console.error('Realtime session route error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
