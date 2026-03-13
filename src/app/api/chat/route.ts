import { NextRequest, NextResponse } from 'next/server';
import Anthropic from '@anthropic-ai/sdk';
import { Message, InterviewFormat } from '@/lib/types';
import { getSystemPrompt } from '@/lib/prompts';

const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

interface ChatRequest {
  messages: Message[];
  format: InterviewFormat;
  candidateName: string;
}

export async function POST(request: NextRequest) {
  try {
    const body: ChatRequest = await request.json();
    const { messages, format, candidateName } = body;

    if (!messages || !format) {
      return NextResponse.json({ error: 'Missing required fields' }, { status: 400 });
    }

    // Convert our Message format to Anthropic's format
    const anthropicMessages: Anthropic.MessageParam[] = messages.map((msg) => ({
      role: msg.role === 'candidate' ? 'user' : 'assistant',
      content: msg.content,
    }));

    // Get the system prompt with candidate name injected
    const basePrompt = getSystemPrompt(format);
    const systemPrompt = `${basePrompt}\n\nThe candidate's name is ${candidateName}. Address them by name occasionally to make the interview feel personal.`;

    const stream = await client.messages.stream({
      model: 'claude-sonnet-4-6',
      max_tokens: 1024,
      system: systemPrompt,
      messages: anthropicMessages,
    });

    // Create a ReadableStream to pipe the response
    const readableStream = new ReadableStream({
      async start(controller) {
        const encoder = new TextEncoder();
        try {
          for await (const chunk of stream) {
            if (
              chunk.type === 'content_block_delta' &&
              chunk.delta.type === 'text_delta'
            ) {
              const text = chunk.delta.text;
              controller.enqueue(encoder.encode(text));
            }
          }
        } catch (error) {
          controller.error(error);
        } finally {
          controller.close();
        }
      },
    });

    return new Response(readableStream, {
      headers: {
        'Content-Type': 'text/plain; charset=utf-8',
        'Transfer-Encoding': 'chunked',
        'Cache-Control': 'no-cache',
      },
    });
  } catch (error) {
    console.error('Chat API error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
