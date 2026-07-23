import { NextRequest, NextResponse } from 'next/server';

export async function POST(request: NextRequest) {
  try {
    const formData = await request.formData();
    const audio = formData.get('audio') as Blob | null;

    if (!audio) {
      return NextResponse.json({ error: 'No audio provided' }, { status: 400 });
    }

    const elevenlabsForm = new FormData();
    // ElevenLabs requires a filename with a supported extension
    elevenlabsForm.append('file', audio, 'recording.webm');
    elevenlabsForm.append('model_id', 'scribe_v1');

    const response = await fetch('https://api.elevenlabs.io/v1/speech-to-text', {
      method: 'POST',
      headers: {
        'xi-api-key': process.env.ELEVENLABS_API_KEY!,
      },
      body: elevenlabsForm,
    });

    if (!response.ok) {
      const err = await response.text();
      console.error('ElevenLabs STT error:', err);
      return NextResponse.json({ error: 'Transcription failed' }, { status: 500 });
    }

    const data = await response.json();
    return NextResponse.json({ text: data.text ?? '' });
  } catch (error) {
    console.error('STT route error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
