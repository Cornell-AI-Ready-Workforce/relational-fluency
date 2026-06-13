"""Deepgram streaming STT wrapper.

Holds a live Deepgram WebSocket for the duration of a session. Callers feed
raw 16-bit PCM mono @ 16kHz into `send_audio` and consume events from
`events()` as an async generator:

  {"type": "partial",  "text": "..."}
  {"type": "final",    "text": "..."}
  {"type": "endpoint"}   # utterance ended (silence detected after final)
  {"type": "speech_started"}
  {"type": "error",   "text": "..."}
  {"type": "closed"}
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, Optional

from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveOptions,
    LiveTranscriptionEvents,
)


def default_options() -> LiveOptions:
    return LiveOptions(
        model="nova-3",
        language="en-US",
        smart_format=True,
        encoding="linear16",
        sample_rate=16000,
        channels=1,
        interim_results=True,
        utterance_end_ms=1200,
        vad_events=True,
        endpointing=300,
    )


class DeepgramStream:
    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.environ["DEEPGRAM_API_KEY"]
        config = DeepgramClientOptions(options={"keepalive": "true"})
        self.client = DeepgramClient(key, config)
        self.connection = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self, options: Optional[LiveOptions] = None) -> None:
        self._loop = asyncio.get_running_loop()
        self.connection = self.client.listen.asyncwebsocket.v("1")

        def _put(event: dict) -> None:
            # Deepgram callbacks may fire from threads — schedule onto our loop.
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self.queue.put_nowait, event)

        async def on_transcript(_self, result, **_kw):
            try:
                alt = result.channel.alternatives[0]
                text = alt.transcript
            except Exception:
                return
            if not text:
                return
            _put({
                "type": "final" if result.is_final else "partial",
                "text": text,
            })

        async def on_utterance_end(_self, *_a, **_kw):
            _put({"type": "endpoint"})

        async def on_speech_started(_self, *_a, **_kw):
            _put({"type": "speech_started"})

        async def on_error(_self, error, **_kw):
            _put({"type": "error", "text": str(error)})

        async def on_close(_self, *_a, **_kw):
            self._closed = True
            _put({"type": "closed"})

        self.connection.on(LiveTranscriptionEvents.Transcript, on_transcript)
        self.connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
        self.connection.on(LiveTranscriptionEvents.SpeechStarted, on_speech_started)
        self.connection.on(LiveTranscriptionEvents.Error, on_error)
        self.connection.on(LiveTranscriptionEvents.Close, on_close)

        ok = await self.connection.start(options or default_options())
        if not ok:
            raise RuntimeError("Deepgram connection failed to start")

    async def send_audio(self, chunk: bytes) -> None:
        if self.connection and not self._closed and chunk:
            await self.connection.send(chunk)

    async def events(self) -> AsyncIterator[dict]:
        while True:
            event = await self.queue.get()
            yield event
            if event["type"] == "closed":
                return

    async def close(self) -> None:
        if self.connection and not self._closed:
            try:
                await self.connection.finish()
            except Exception:
                pass
        self._closed = True
