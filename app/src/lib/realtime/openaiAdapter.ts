import {
  RealtimeAdapter,
  RealtimeCallbacks,
  RealtimeSessionConfig,
} from './types';

// OpenAI Realtime API adapter (WebRTC).
// The browser fetches an ephemeral client secret from /api/realtime/session
// (the real API key never leaves the server), then connects its microphone
// track directly to the Realtime API. Model events arrive on the
// "oai-events" data channel.

interface SessionGrant {
  clientSecret: string;
  model: string;
  callsUrl: string; // e.g. https://api.openai.com/v1/realtime/calls
}

function getSupportedMimeType(): string {
  const types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
  return types.find((t) => MediaRecorder.isTypeSupported(t)) ?? '';
}

export class OpenAIRealtimeAdapter implements RealtimeAdapter {
  private pc: RTCPeerConnection | null = null;
  private dc: RTCDataChannel | null = null;
  private micStream: MediaStream | null = null;
  private remoteAudioEl: HTMLAudioElement | null = null;

  // Two-sided session recording (participant + agent mixed)
  private recCtx: AudioContext | null = null;
  private recorder: MediaRecorder | null = null;
  private recChunks: Blob[] = [];
  private recStartedAt = 0;

  private callbacks: RealtimeCallbacks | null = null;
  private assistantBuffer = '';

  async connect(config: RealtimeSessionConfig, callbacks: RealtimeCallbacks): Promise<void> {
    this.callbacks = callbacks;

    const grantRes = await fetch('/api/realtime/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ format: config.format, candidateName: config.candidateName }),
    });
    if (!grantRes.ok) {
      const err = await grantRes.json().catch(() => ({}));
      throw new Error(err.error ?? 'Could not create realtime session');
    }
    const grant: SessionGrant = await grantRes.json();

    this.micStream = await navigator.mediaDevices.getUserMedia({ audio: true });

    const pc = new RTCPeerConnection();
    this.pc = pc;

    for (const track of this.micStream.getAudioTracks()) {
      pc.addTrack(track, this.micStream);
    }

    // Agent audio comes back as a remote track
    pc.ontrack = (e) => {
      const remoteStream = e.streams[0] ?? new MediaStream([e.track]);
      if (!this.remoteAudioEl) {
        this.remoteAudioEl = new Audio();
        this.remoteAudioEl.autoplay = true;
      }
      this.remoteAudioEl.srcObject = remoteStream;
      this.startMixedRecording(remoteStream);
    };

    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'failed') {
        this.callbacks?.onError('Connection to the interviewer was lost.');
      }
    };

    const dc = pc.createDataChannel('oai-events');
    this.dc = dc;
    dc.onmessage = (e) => this.handleEvent(e.data);

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    const sdpRes = await fetch(`${grant.callsUrl}?model=${encodeURIComponent(grant.model)}`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${grant.clientSecret}`,
        'Content-Type': 'application/sdp',
      },
      body: offer.sdp,
    });
    if (!sdpRes.ok) {
      const errText = await sdpRes.text();
      throw new Error(`Realtime handshake failed: ${errText.slice(0, 200)}`);
    }
    const answerSdp = await sdpRes.text();
    await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp });

    // Wait for the data channel so the caller knows the session is live
    if (dc.readyState !== 'open') {
      await new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error('Realtime session timed out')), 15000);
        dc.onopen = () => { clearTimeout(timeout); resolve(); };
        dc.onerror = () => { clearTimeout(timeout); reject(new Error('Realtime data channel failed')); };
      });
    }

    // Ask the interviewer to open the conversation
    this.send({ type: 'response.create' });
  }

  private send(event: Record<string, unknown>) {
    if (this.dc?.readyState === 'open') this.dc.send(JSON.stringify(event));
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private handleEvent(raw: string) {
    let event: any;
    try { event = JSON.parse(raw); } catch { return; }
    const cb = this.callbacks;
    if (!cb) return;

    switch (event.type) {
      // ── Participant side ──
      case 'input_audio_buffer.speech_started':
        cb.onUserSpeakingChange(true);
        break;
      case 'input_audio_buffer.speech_stopped':
        cb.onUserSpeakingChange(false);
        break;
      case 'conversation.item.input_audio_transcription.completed':
        if (event.transcript?.trim()) cb.onUserTranscript(event.transcript.trim());
        break;

      // ── Agent side (GA + preview event names) ──
      case 'response.output_audio_transcript.delta':
      case 'response.audio_transcript.delta':
        this.assistantBuffer += event.delta ?? '';
        cb.onAssistantDelta(this.assistantBuffer);
        break;
      case 'response.output_audio_transcript.done':
      case 'response.audio_transcript.done': {
        const text = (event.transcript ?? this.assistantBuffer).trim();
        this.assistantBuffer = '';
        cb.onAssistantDelta('');
        if (text) cb.onAssistantTranscript(text);
        break;
      }
      case 'output_audio_buffer.started':
        cb.onSpeakingChange(true);
        break;
      case 'output_audio_buffer.stopped':
      case 'output_audio_buffer.cleared':
        cb.onSpeakingChange(false);
        break;

      // ── Interview completion via tool call ──
      case 'response.function_call_arguments.done':
        if (event.name === 'complete_interview' || !event.name) {
          // Acknowledge the tool call so the response can settle, then finish.
          this.send({
            type: 'conversation.item.create',
            item: {
              type: 'function_call_output',
              call_id: event.call_id,
              output: JSON.stringify({ status: 'acknowledged' }),
            },
          });
          cb.onComplete();
        }
        break;

      case 'error':
        console.error('Realtime API error:', event.error);
        cb.onError(event.error?.message ?? 'Realtime session error');
        break;
    }
  }

  setMuted(muted: boolean): void {
    this.micStream?.getAudioTracks().forEach((t) => { t.enabled = !muted; });
  }

  cancelResponse(): void {
    this.send({ type: 'response.cancel' });
  }

  private startMixedRecording(remoteStream: MediaStream) {
    if (this.recorder || !this.micStream) return;
    try {
      const ctx = new AudioContext();
      this.recCtx = ctx;
      const dest = ctx.createMediaStreamDestination();
      ctx.createMediaStreamSource(this.micStream).connect(dest);
      ctx.createMediaStreamSource(remoteStream).connect(dest);

      const mimeType = getSupportedMimeType();
      const recorder = new MediaRecorder(dest.stream, mimeType ? { mimeType } : {});
      this.recorder = recorder;
      this.recChunks = [];
      recorder.ondataavailable = (e) => { if (e.data.size > 0) this.recChunks.push(e.data); };
      recorder.start(1000);
      this.recStartedAt = Date.now();
    } catch (err) {
      console.error('Could not start session recording:', err);
    }
  }

  async disconnect(): Promise<{ blob: Blob | null; durationSeconds: number }> {
    const durationSeconds = this.recStartedAt
      ? Math.round((Date.now() - this.recStartedAt) / 1000)
      : 0;

    const blob = await new Promise<Blob | null>((resolve) => {
      const recorder = this.recorder;
      if (!recorder || recorder.state === 'inactive') { resolve(null); return; }
      recorder.onstop = () => {
        resolve(new Blob(this.recChunks, { type: recorder.mimeType || 'audio/webm' }));
      };
      recorder.stop();
    });

    this.dc?.close();
    this.pc?.close();
    this.micStream?.getTracks().forEach((t) => t.stop());
    if (this.remoteAudioEl) this.remoteAudioEl.srcObject = null;
    await this.recCtx?.close().catch(() => {});

    this.pc = null;
    this.dc = null;
    this.micStream = null;
    this.remoteAudioEl = null;
    this.recorder = null;
    this.recCtx = null;
    this.callbacks = null;

    return { blob, durationSeconds };
  }
}
