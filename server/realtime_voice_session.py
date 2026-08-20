"""Voice encounters on Gemini Live (speech-to-speech).

Replaces the v1 cascade — Deepgram STT -> text model -> ElevenLabs TTS — with a
single realtime session against the Cornell LiteLLM gateway. The browser
protocol is unchanged, so static/v2.html and participant.html keep working:
PCM16 in over the WebSocket, PCM16 out as binary frames, JSON control events.

This is the "session broker" of the architecture: it relays audio both ways,
lets the director steer the actor between turns, and records audio, transcript,
and the steering log.

Turn-taking lives here because the gateway does not expose Gemini's native VAD
(see server/voice/realtime.py).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, List, Optional

from .voice.realtime import RealtimeVoiceSession, SilenceDetector

if TYPE_CHECKING:
    from fastapi import WebSocket

    from .session import Session


class RealtimeVoiceSessionRunner:
    """One participant, one agent, one live conversation."""

    def __init__(self, session: "Session", ws: "WebSocket"):
        self.session = session
        self.ws = ws
        self.agent = session.scenario.cast[0]
        self.agent_id = self.agent.id
        self.rt: Optional[RealtimeVoiceSession] = None
        self.vad = SilenceDetector()
        self._closed = False
        self._agent_text: List[str] = []
        self._turn_started_at: Optional[float] = None
        self._speaking = False

    # ── lifecycle ──────────────────────────────────────────────────────────
    def _instructions(self, director_note: str = "") -> str:
        # Reuse the engine's prompt builder so the voice agent and the text
        # agent are the same character — persona knobs, branches, and the
        # director's intent all compose exactly as they do in text mode.
        engine = self.session.engines[self.agent_id]
        base = engine._system_prompt(self.session.triggered_branches, director_note or None)
        voice_rules = (
            "\n\nVOICE: You are in a live spoken conversation. Speak naturally and "
            "concisely — one to three sentences per turn. Never read out JSON, "
            "markdown, or stage directions."
        )
        return base + voice_rules

    async def run(self) -> None:
        self.rt = RealtimeVoiceSession(instructions=self._instructions())
        await self.rt.connect()
        self.session.store.event("realtime_session_started", model=self.rt.model)
        try:
            await asyncio.gather(self._client_to_model(), self._model_to_client())
        finally:
            self._closed = True
            if self.rt:
                await self.rt.close()

    # ── participant -> model ───────────────────────────────────────────────
    async def _client_to_model(self) -> None:
        from fastapi import WebSocketDisconnect

        try:
            while not self._closed:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                pcm = msg.get("bytes")
                if not pcm:
                    continue

                # Always record the participant channel, even while the agent
                # speaks — the study needs both sides of the audio.
                self.session.store.append_user_audio(pcm)

                mark = self.vad.feed(pcm)
                if mark == "speech_started":
                    await self._send({"type": "speech_started"})
                    if self._speaking:
                        # Barge-in: drop the agent's remaining audio.
                        await self.rt.cancel_response()
                        self._speaking = False
                        await self._send({"type": "assistant_interrupted"})

                await self.rt.send_audio(pcm)

                if mark == "turn_ended":
                    self._turn_started_at = time.time()
                    await self.rt.commit_turn()
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001 — surfaced in the session log
            self.session.store.event("voice_error", where="client_to_model", message=str(exc))

    # ── model -> participant ───────────────────────────────────────────────
    async def _model_to_client(self) -> None:
        assert self.rt is not None
        async for ev in self.rt.events():
            etype = ev["type"]

            if etype == "agent_audio":
                if not self._speaking:
                    self._speaking = True
                    await self._send({
                        "type": "assistant_started",
                        "agent_id": self.agent_id,
                        "agent_name": self.agent.name,
                    })
                self.session.store.append_assistant_audio(ev["pcm"], agent_id=self.agent_id)
                await self._send_bytes(ev["pcm"])

            elif etype == "agent_transcript_delta":
                self._agent_text.append(ev["text"])
                await self._send({
                    "type": "assistant_text_delta",
                    "text": ev["text"],
                    "agent_id": self.agent_id,
                })

            elif etype == "user_transcript":
                self.session.append_user(ev["text"])
                self.session.store.event("user_turn", text=ev["text"], channel="voice")
                await self.session.broadcast(
                    {"type": "transcript", "role": "user", "text": ev["text"]}
                )

            elif etype == "response_done":
                text = "".join(self._agent_text).strip()
                self._agent_text = []
                self._speaking = False
                if text:
                    self.session.append_agent(self.agent_id, text)
                    await self.session.broadcast({
                        "type": "transcript",
                        "role": "assistant",
                        "agent_id": self.agent_id,
                        "text": text,
                    })
                latency = (
                    round(time.time() - self._turn_started_at, 3)
                    if self._turn_started_at else None
                )
                self.session.store.event(
                    "assistant_turn", agent_id=self.agent_id, text=text, latency_s=latency
                )
                await self._send({"type": "assistant_done", "agent_id": self.agent_id})
                await self._steer()

            elif etype == "tool_call":
                self.session.store.event("tool_call", name=ev.get("name"))
                await self._send({"type": "encounter_complete"})

            elif etype == "error":
                self.session.store.event("voice_error", where="model", message=ev["message"])
                await self._send({"type": "error", "message": ev["message"]})

    # ── director ───────────────────────────────────────────────────────────
    async def _steer(self) -> None:
        """Closed-loop steering between turns.

        The director reviews the transcript and shifts persona knobs; the actor
        is then re-briefed with the updated persona, which is how a stage
        direction reaches a speech-to-speech model that has no separate system
        channel. Steering is one turn behind by construction — the
        participant's words only exist once the model has transcribed them.

        Session.auto_steer() owns the review and swallows its own errors, so a
        steering failure can never break a live encounter.
        """
        before = len(self.session.steering_log)
        await self.session.auto_steer()
        if len(self.session.steering_log) == before:
            return  # nothing changed; the current brief still stands
        await self.rt.update_instructions(self._instructions())

    # ── transport helpers ──────────────────────────────────────────────────
    async def _send(self, payload: dict) -> None:
        try:
            await self.ws.send_json(payload)
        except Exception:  # noqa: BLE001 — client vanished
            self._closed = True

    async def _send_bytes(self, payload: bytes) -> None:
        try:
            await self.ws.send_bytes(payload)
        except Exception:  # noqa: BLE001
            self._closed = True
