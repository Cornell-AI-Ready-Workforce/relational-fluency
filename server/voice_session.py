"""Single-agent voice session orchestrator.

For multi-agent group scenarios, see multi_agent_session.py.

Flow per turn:
  Browser audio → Deepgram STT → on UtteranceEnd → AgentEngine.stream_reply
  → ElevenLabs TTS → back to browser
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, AsyncIterator, List, Optional

from .session import Session
from .voice.stt import DeepgramStream
from .voice.tts import ElevenLabsStream

if TYPE_CHECKING:
    from fastapi import WebSocket


class VoiceSessionRunner:
    """Runs a single-agent voice session. While the assistant is speaking, the
    mic is muted (v1 policy — no barge-in)."""

    def __init__(self, session: Session, ws: "WebSocket"):
        assert not session.is_group, "Use MultiAgentVoiceSessionRunner for groups"
        self.session = session
        self.ws = ws
        self.agent_id = session.scenario.cast[0].id
        self.stt: Optional[DeepgramStream] = None
        self.assistant_speaking = asyncio.Event()
        self.assistant_speaking.clear()
        self._pending_finals: List[str] = []
        self._turn_task: Optional[asyncio.Task] = None
        self._steer_task: Optional[asyncio.Task] = None
        self._closed = False

    async def run(self) -> None:
        self.stt = DeepgramStream()
        await self.stt.start()
        self.session.store.event("voice_stt_started")

        try:
            await asyncio.gather(
                self._client_to_stt(),
                self._stt_to_orchestrator(),
            )
        finally:
            self._closed = True
            if self._turn_task and not self._turn_task.done():
                self._turn_task.cancel()
                try:
                    await self._turn_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self.stt:
                await self.stt.close()

    async def _client_to_stt(self) -> None:
        from fastapi import WebSocketDisconnect
        try:
            while not self._closed:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if "bytes" in msg and msg["bytes"] is not None:
                    self.session.store.append_user_audio(msg["bytes"])
                    if not self.assistant_speaking.is_set():
                        await self.stt.send_audio(msg["bytes"])
                elif "text" in msg and msg["text"] is not None:
                    pass  # reserved for future control messages
        except WebSocketDisconnect:
            return
        except Exception as e:
            self.session.store.event("voice_error", where="client_to_stt", message=str(e))

    async def _stt_to_orchestrator(self) -> None:
        async for event in self.stt.events():
            etype = event["type"]
            if etype == "partial":
                if not self.assistant_speaking.is_set():
                    await self._send_safe({
                        "type": "user_transcript",
                        "text": event["text"],
                        "final": False,
                    })
            elif etype == "final":
                if self.assistant_speaking.is_set():
                    continue
                self._pending_finals.append(event["text"])
                await self._send_safe({
                    "type": "user_transcript",
                    "text": event["text"],
                    "final": True,
                })
            elif etype == "endpoint":
                if self.assistant_speaking.is_set() or not self._pending_finals:
                    continue
                user_text = " ".join(self._pending_finals).strip()
                self._pending_finals = []
                if user_text:
                    self._turn_task = asyncio.create_task(self._run_turn(user_text))
            elif etype == "speech_started":
                await self._send_safe({"type": "speech_started"})
            elif etype == "error":
                self.session.store.event(
                    "voice_error", where="stt", message=event.get("text", "")
                )
            elif etype == "closed":
                return

    async def _run_turn(self, user_text: str) -> None:
        self.assistant_speaking.set()
        t_user = time.time()
        self.session.append_user(user_text)
        self.session.store.event("user_turn", text=user_text, channel="voice")
        await self.session.broadcast({
            "type": "transcript", "role": "user", "text": user_text,
        })
        await self._send_safe({
            "type": "assistant_started",
            "agent_id": self.agent_id,
            "agent_name": self.session.scenario.cast[0].name,
        })

        text_for_tts: asyncio.Queue = asyncio.Queue()
        full_text: List[str] = []
        t_first_token: List[float] = []

        engine = self.session.engines[self.agent_id]

        async def claude_producer():
            try:
                async for delta in engine.stream_reply(
                    self.session.shared_history,
                    self.session.triggered_branches,
                    self.session.name_lookup,
                ):
                    if not t_first_token:
                        t_first_token.append(time.time())
                    full_text.append(delta)
                    await text_for_tts.put(delta)
                    await self._send_safe({
                        "type": "assistant_text_delta",
                        "text": delta,
                        "agent_id": self.agent_id,
                    })
            finally:
                await text_for_tts.put(None)

        async def tts_text_iter():
            while True:
                item = await text_for_tts.get()
                if item is None:
                    return
                yield item

        try:
            t_first_audio: List[float] = []
            producer = asyncio.create_task(claude_producer())
            voice_id = self.session.scenario.cast[0].voice_id
            async with ElevenLabsStream(voice_id=voice_id) as tts:
                async for pcm in tts.synthesize(tts_text_iter()):
                    if not t_first_audio:
                        t_first_audio.append(time.time())
                    self.session.store.append_assistant_audio(pcm, agent_id=self.agent_id)
                    await self._send_bytes_safe(pcm)
            await producer

            assistant_text = "".join(full_text)
            self.session.append_agent(self.agent_id, assistant_text)
            lat_first_tok = round((t_first_token[0] - t_user) if t_first_token else 0, 3)
            lat_first_aud = round((t_first_audio[0] - t_user) if t_first_audio else 0, 3)
            lat_total = round(time.time() - t_user, 3)

            await self._send_safe({
                "type": "assistant_done",
                "text": assistant_text,
                "agent_id": self.agent_id,
                "latency_to_first_token_s": lat_first_tok,
                "latency_to_first_audio_s": lat_first_aud,
                "latency_total_s": lat_total,
            })
            self.session.store.event(
                "assistant_turn",
                channel="voice",
                agent_id=self.agent_id,
                text=assistant_text,
                model=engine.model,
                persona=self.session.personas[self.agent_id].snapshot(),
                live_notes=list(engine.live_notes),
                triggered_branches=[b.id for b in self.session.triggered_branches],
                latency_to_first_token_s=lat_first_tok,
                latency_to_first_audio_s=lat_first_aud,
                latency_total_s=lat_total,
            )
            await self.session.broadcast({
                "type": "transcript",
                "role": "assistant",
                "agent_id": self.agent_id,
                "text": assistant_text,
                "latency_to_first_token_s": lat_first_tok,
                "latency_to_first_audio_s": lat_first_aud,
                "latency_total_s": lat_total,
            })
            await self.session.broadcast({"type": "state", **self.session.snapshot()})
            # Auto steering runs off the critical path: any gear shift takes
            # effect on the next turn (system prompts compose fresh per turn).
            self._steer_task = asyncio.create_task(self.session.auto_steer())
        except Exception as e:
            self.session.store.event("voice_error", where="turn", message=str(e))
            await self._send_safe({"type": "error", "message": str(e)})
        finally:
            self.assistant_speaking.clear()

    async def _send_safe(self, payload: dict) -> None:
        try:
            await self.ws.send_json(payload)
        except Exception:
            self._closed = True

    async def _send_bytes_safe(self, payload: bytes) -> None:
        try:
            await self.ws.send_bytes(payload)
        except Exception:
            self._closed = True
