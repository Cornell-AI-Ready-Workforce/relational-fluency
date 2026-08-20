# LEGACY — group scenarios only. Single-agent encounters run on Gemini Live
# (server/voice/realtime.py). This cascade is deleted once the multi-agent
# runner is migrated; do not build on it.
"""ElevenLabs streaming TTS via WebSocket multi-stream-input.

Lets us start synthesizing while Claude is still generating — the single
biggest latency win in the cascade pipeline.

Usage:
    async with ElevenLabsStream(voice_id=...) as tts:
        async for pcm_chunk in tts.synthesize(text_chunk_iter):
            await participant_ws.send_bytes(pcm_chunk)

Output is 16-bit PCM mono @ 16000 Hz.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import AsyncIterator, Optional

import websockets


ELEVEN_WS_URL = (
    "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
    "?model_id={model_id}&output_format=pcm_16000"
)


class ElevenLabsStream:
    def __init__(
        self,
        voice_id: Optional[str] = None,
        model_id: str = "eleven_turbo_v2_5",
        api_key: Optional[str] = None,
    ):
        self.voice_id = voice_id or os.environ.get(
            "ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL"
        )
        self.model_id = model_id
        self.api_key = api_key or os.environ["ELEVENLABS_API_KEY"]
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

    async def __aenter__(self) -> "ElevenLabsStream":
        url = ELEVEN_WS_URL.format(voice_id=self.voice_id, model_id=self.model_id)
        self.ws = await websockets.connect(
            url, additional_headers={"xi-api-key": self.api_key}
        )
        await self.ws.send(json.dumps({
            "text": " ",
            "voice_settings": {
                "stability": 0.22,       # low = highly expressive, emotional delivery
                "similarity_boost": 0.8,
                "use_speaker_boost": True,
            },
            "generation_config": {
                "chunk_length_schedule": [50, 90, 120, 200],
            },
            "xi_api_key": self.api_key,
        }))
        return self

    async def __aexit__(self, *exc) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    async def synthesize(self, text_chunks: AsyncIterator[str]) -> AsyncIterator[bytes]:
        assert self.ws is not None, "use as async context manager"

        async def pump_text() -> None:
            try:
                async for chunk in text_chunks:
                    if not chunk:
                        continue
                    await self.ws.send(json.dumps({
                        "text": chunk,
                        "try_trigger_generation": True,
                    }))
            finally:
                await self.ws.send(json.dumps({"text": ""}))

        pumper = asyncio.create_task(pump_text())

        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    yield msg
                    continue
                try:
                    data = json.loads(msg)
                except Exception:
                    continue
                if data.get("audio"):
                    yield base64.b64decode(data["audio"])
                if data.get("isFinal"):
                    break
        finally:
            if not pumper.done():
                pumper.cancel()
                try:
                    await pumper
                except (asyncio.CancelledError, Exception):
                    pass
