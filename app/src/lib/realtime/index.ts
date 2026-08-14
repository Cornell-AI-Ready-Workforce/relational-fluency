import { RealtimeAdapter, RealtimeProvider } from './types';
import { OpenAIRealtimeAdapter } from './openaiAdapter';

export * from './types';

export function createRealtimeAdapter(provider: RealtimeProvider = 'openai'): RealtimeAdapter {
  switch (provider) {
    case 'openai':
      return new OpenAIRealtimeAdapter();
    case 'gemini':
      // Planned: gemini-3.1-flash-live-preview via the Live API
      throw new Error('Gemini Live adapter not implemented yet');
  }
}
