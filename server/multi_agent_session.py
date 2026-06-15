"""Multi-agent (group) voice session orchestrator.

Per user turn:
  1. Deepgram emits final + UtteranceEnd → user_text
  2. Director picks N speakers + optional per-speaker intent
  3. For each speaker in order:
       - Stream Claude reply through that agent's AgentEngine
       - Pipe through ElevenLabs with the agent's voice_id
       - Write audio to that agent's WAV file
       - Send transcript + active-speaker events to participant + researcher

Mic is muted for the entire multi-speaker turn (one or more agents) until all
agents have finished. v1 policy — no barge-in.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, List, Optional

from .session import Session
from .voice.stt import DeepgramStream
from .voice.tts import ElevenLabsStream

if TYPE_CHECKING:
    from fastapi import WebSocket


# Cap on how many agent utterances one user turn can trigger (initial routed
# speakers + agent-to-agent hand-offs). Set high so Arjun and Claire can really
# get into it back and forth without the participant having to intervene.
MAX_AGENT_TURNS_PER_USER = 8


def _strip_self_label(text: str, labels: List[str]) -> str:
    """Drop a leading self-referential speaker label the model sometimes emits by
    mimicking the transcript format (e.g. 'Claire Donovan:' or 'Claire -').
    Without this the agent's own name gets spoken aloud and shown in the caption."""
    for nm in sorted(labels, key=len, reverse=True):
        if not nm:
            continue
        m = re.match(r"^\s*\[?" + re.escape(nm) + r"\]?\s*[:\-–—]\s*", text, re.IGNORECASE)
        if m:
            return text[m.end():]
    return text


class MultiAgentVoiceSessionRunner:
    def __init__(self, session: Session, ws: "WebSocket"):
        assert session.is_group, "Use VoiceSessionRunner for single-agent"
        assert session.director is not None
        self.session = session
        self.ws = ws
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
                    pass
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
                        "type": "user_transcript", "text": event["text"], "final": False,
                    })
            elif etype == "final":
                if self.assistant_speaking.is_set():
                    continue
                self._pending_finals.append(event["text"])
                await self._send_safe({
                    "type": "user_transcript", "text": event["text"], "final": True,
                })
                # Primary turn-end: ~0.7s of silence (endpointing → speech_final).
                if event.get("speech_final"):
                    self._maybe_start_turn()
            elif etype == "endpoint":
                # Backup turn-end: UtteranceEnd at 1s, in case speech_final missed.
                self._maybe_start_turn()
            elif etype == "speech_started":
                await self._send_safe({"type": "speech_started"})
            elif etype == "error":
                self.session.store.event("voice_error", where="stt", message=event.get("text", ""))
            elif etype == "closed":
                return

    def _maybe_start_turn(self) -> None:
        """Kick off an agent turn from the buffered user finals, unless agents
        are already speaking or there's nothing buffered. Idempotent: once fired
        it clears the buffer, so the speech_final and UtteranceEnd triggers can't
        double-fire the same utterance."""
        if self.assistant_speaking.is_set() or not self._pending_finals:
            return
        user_text = " ".join(self._pending_finals).strip()
        self._pending_finals = []
        if user_text:
            self._turn_task = asyncio.create_task(self._run_turn(user_text))

    async def _run_turn(self, user_text: str) -> None:
        self.assistant_speaking.set()
        t_user = time.time()
        self.session.append_user(user_text)
        self.session.store.event("user_turn", text=user_text, channel="voice", mode="group")
        await self.session.broadcast({"type": "transcript", "role": "user", "text": user_text})

        try:
            # 1) Director picks speakers
            t_director_start = time.time()
            speakers = await self.session.director.route(
                self.session.shared_history, user_text
            )
            lat_director = round(time.time() - t_director_start, 3)
            self.session.store.event(
                "director_routed",
                speakers=[s["agent_id"] for s in speakers],
                intents={s["agent_id"]: s.get("intent") for s in speakers if s.get("intent")},
                latency_s=lat_director,
            )
            await self._send_safe({
                "type": "director_routed",
                "speakers": speakers,
                "latency_s": lat_director,
            })

            if not speakers:
                # Genuine silence — no agent responds. Re-open mic.
                await self._send_safe({"type": "silence", "rationale": "director chose silence"})
                await self.session.broadcast({"type": "state", **self.session.snapshot()})
                return

            # 2) Each speaker takes their turn sequentially
            for spk in speakers:
                if self._closed:
                    break
                await self._speak_one(spk, t_user_start=t_user)

            # 3) Agent-to-agent hand-offs: if the agent who just spoke explicitly
            # asked another agent, let that agent answer — without the human
            # having to re-prompt. Iterates on explicit hand-offs only, capped.
            last_speaker = speakers[-1]["agent_id"] if speakers else None
            agent_turns = len(speakers)
            while (not self._closed and last_speaker is not None
                   and agent_turns < MAX_AGENT_TURNS_PER_USER):
                follow = await self.session.director.route_continuation(
                    self.session.shared_history, last_speaker
                )
                if not follow:
                    break  # no explicit hand-off — floor returns to the human
                spk = follow[0]
                self.session.store.event(
                    "director_routed",
                    speakers=[spk["agent_id"]],
                    intents=({spk["agent_id"]: spk["intent"]} if spk.get("intent") else {}),
                    continuation=True,
                )
                await self._send_safe({
                    "type": "director_routed", "speakers": [spk], "continuation": True,
                })
                await self._speak_one(spk, t_user_start=t_user)
                last_speaker = spk["agent_id"]
                agent_turns += 1

            await self.session.broadcast({"type": "state", **self.session.snapshot()})
            # Auto steering runs off the critical path: any gear shift takes
            # effect on the next turn (system prompts compose fresh per turn).
            self._steer_task = asyncio.create_task(self.session.auto_steer())
        except Exception as e:
            self.session.store.event("voice_error", where="turn", message=str(e))
            await self._send_safe({"type": "error", "message": str(e)})
        finally:
            self.assistant_speaking.clear()

    async def _speak_one(self, spk: dict, t_user_start: float) -> None:
        agent_id = spk["agent_id"]
        intent = spk.get("intent")
        agent = self.session.scenario.agent(agent_id)
        engine = self.session.engines[agent_id]

        t_start = time.time()
        await self._send_safe({
            "type": "assistant_started",
            "agent_id": agent_id,
            "agent_name": agent.name,
            "director_intent": intent,
        })

        text_for_tts: asyncio.Queue = asyncio.Queue()
        full_text: List[str] = []
        t_first_token: List[float] = []

        # Labels to strip if the model prefixes its own line with them.
        labels = [agent.name]
        name_parts = (agent.name or "").split()
        if len(name_parts) > 1:
            labels.append(name_parts[0])
        max_head = max((len(l) for l in labels), default=0) + 4

        async def claude_producer():
            head = ""
            head_done = False

            async def emit(text: str):
                if not text:
                    return
                full_text.append(text)
                await text_for_tts.put(text)
                await self._send_safe({
                    "type": "assistant_text_delta",
                    "text": text,
                    "agent_id": agent_id,
                })

            try:
                async for delta in engine.stream_reply(
                    self.session.shared_history,
                    self.session.triggered_branches,
                    self.session.name_lookup,
                    director_intent=intent,
                ):
                    if not t_first_token:
                        t_first_token.append(time.time())
                    if not head_done:
                        # Buffer just long enough to detect/strip a leading
                        # self-name label before any audio or caption goes out.
                        head += delta
                        if len(head) < max_head and "\n" not in head:
                            continue
                        head_done = True
                        await emit(_strip_self_label(head, labels))
                        head = ""
                        continue
                    await emit(delta)
                if not head_done:  # short reply that never hit the buffer threshold
                    await emit(_strip_self_label(head, labels))
            finally:
                await text_for_tts.put(None)

        async def tts_text_iter():
            while True:
                item = await text_for_tts.get()
                if item is None:
                    return
                yield item

        t_first_audio: List[float] = []
        producer = asyncio.create_task(claude_producer())
        try:
            async with ElevenLabsStream(voice_id=agent.voice_id) as tts:
                async for pcm in tts.synthesize(tts_text_iter()):
                    if not t_first_audio:
                        t_first_audio.append(time.time())
                    self.session.store.append_assistant_audio(pcm, agent_id=agent_id)
                    await self._send_bytes_safe(pcm)
        finally:
            await producer

        assistant_text = "".join(full_text)
        self.session.append_agent(agent_id, assistant_text)
        lat_first_tok = round((t_first_token[0] - t_start) if t_first_token else 0, 3)
        lat_first_aud = round((t_first_audio[0] - t_start) if t_first_audio else 0, 3)
        lat_total = round(time.time() - t_start, 3)
        lat_user_to_agent = round((t_first_audio[0] - t_user_start) if t_first_audio else 0, 3)

        await self._send_safe({
            "type": "assistant_done",
            "text": assistant_text,
            "agent_id": agent_id,
            "latency_to_first_token_s": lat_first_tok,
            "latency_to_first_audio_s": lat_first_aud,
            "latency_total_s": lat_total,
            "latency_from_user_s": lat_user_to_agent,
        })
        self.session.store.event(
            "assistant_turn",
            channel="voice",
            mode="group",
            agent_id=agent_id,
            text=assistant_text,
            model=engine.model,
            persona=self.session.personas[agent_id].snapshot(),
            director_intent=intent,
            live_notes=list(engine.live_notes),
            triggered_branches=[b.id for b in self.session.triggered_branches],
            latency_to_first_token_s=lat_first_tok,
            latency_to_first_audio_s=lat_first_aud,
            latency_from_user_s=lat_user_to_agent,
            latency_total_s=lat_total,
        )
        await self.session.broadcast({
            "type": "transcript",
            "role": "assistant",
            "agent_id": agent_id,
            "text": assistant_text,
            "latency_from_user_s": lat_user_to_agent,
            "latency_total_s": lat_total,
        })

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
