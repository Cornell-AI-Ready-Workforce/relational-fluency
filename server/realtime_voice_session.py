"""Voice encounters on Gemini Live (speech-to-speech).

Replaces the v1 cascade, Deepgram STT -> text model -> ElevenLabs TTS, with a
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
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, List, Optional

from .director import Director
from .llm import provenance
from .group_room import GroupRoom
from .voice.realtime import RealtimeVoiceSession, SilenceDetector

if TYPE_CHECKING:
    from fastapi import WebSocket

    from .session import Session


# Gemini Live voice names, assigned per segment so consecutive characters do
# not sound like the same person.
GEMINI_VOICES = ["Puck", "Charon", "Kore", "Fenrir", "Aoede"]

END_SEGMENT_TOOL = {
    "type": "function",
    "name": "end_conversation",
    "description": (
        "Call this once this conversation has reached its natural end, the "
        "matter has been addressed, or the participant has clearly finished. "
        "Do not mention the tool."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


class RealtimeVoiceSessionRunner:
    """One participant working through a scenario as consecutive 1:1 conversations.

    A scenario's cast is played in order, one character at a time, e.g. S1 is
    the instigating colleague first, then the peer. Each character gets its own
    persona, voice, and brief; the actor signals the end of its conversation
    with a tool call, and the runner re-briefs the session as the next
    character. Characters never share a turn, which is what the study design
    calls for and what keeps each segment cleanly attributable.
    """

    def __init__(self, session: "Session", ws: "WebSocket"):
        self.session = session
        self.ws = ws
        # An encounter is a sequence of interactions, each with its own mode
        # and its own cast slice. Segment indexes interactions, NOT the cast:
        # S2 has one agent across two interactions, and S3's second interaction
        # is a series of two one-on-ones.
        self.segment = 0
        self.cast = list(session.scenario.cast)
        self.interactions = list(getattr(session.scenario, "interactions", []) or [])
        self._series_idx = 0
        self.agent = self._resolve_agents()[0]
        self.agent_id = self.agent.id
        self.rt: Optional[RealtimeVoiceSession] = None
        self.vad = SilenceDetector()
        self._closed = False
        self._agent_text: List[str] = []
        self._turn_started_at: Optional[float] = None
        self._speaking = False
        # Group rooms: several characters share one realtime session, taking
        # turns. Only one can hold the audio stream at a time, so the director
        # picks an order and each speaker is served in sequence.
        self._scenario_is_group = session.is_group
        self.director = getattr(session, "director", None) or (
            Director(session.scenario) if session.is_group else None
        )
        self._response_done = asyncio.Event()
        self._last_user_text = ""
        self._turn_index = 0
        self._pending_direction: Optional[dict] = None
        # Planted triggers fire in order within the current interaction. They
        # are the measurement: each maps to ESCI items, and the participant's
        # response to it is what a rater scores.
        self._trigger_idx = 0
        self._fired: List[str] = []
        self._last_activity = time.time()
        self._turns_this_interaction = 0
        self._interaction_started_at = time.time()
        self._finalizing = False
        self._switching = False
        self.room: Optional[GroupRoom] = None
        self._pumps: List[asyncio.Task] = []
        self._floor = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def _instructions(self, director_note: str = "") -> str:
        # Reuse the engine's prompt builder so the voice agent and the text
        # agent are the same character, persona knobs, branches, and the
        # director's intent all compose exactly as they do in text mode.
        engine = self.session.engines[self.agent_id]
        base = engine._system_prompt(self.session.triggered_branches, director_note or None)
        voice_rules = (
            "\n\nVOICE: You are in a live spoken conversation. Speak naturally and "
            "concisely, one to three sentences per turn. Never read out JSON, "
            "markdown, or stage directions."
        )
        return base + voice_rules

    def is_group(self) -> bool:
        """Group only while the *current* interaction puts several characters in
        the room, S3 opens as a group meeting and continues as one-on-ones."""
        if self.interactions:
            return self._interaction_mode() == "group"
        return self._scenario_is_group

    def _resolve_agents(self) -> List:
        """Characters active in the current interaction, in order. Falls back to
        the whole cast for legacy scenarios that have no interaction list."""
        by_id = {a.id: a for a in self.cast}
        interaction = self._interaction()
        spec = interaction.get("agents") or interaction.get("agent")
        if isinstance(spec, str):
            spec = [spec]
        if not spec:
            return self.cast or []
        resolved = [by_id[a] for a in spec if a in by_id]
        return resolved or self.cast

    def _interaction_mode(self) -> str:
        return self._interaction().get("mode", "group" if len(self.cast) > 1 else "one_to_one")

    def _interaction(self) -> dict:
        if self.segment < len(self.interactions):
            return self.interactions[self.segment]
        return {}

    def _triggers(self) -> List[dict]:
        return self._interaction().get("triggers", []) or []

    def _next_trigger(self) -> Optional[dict]:
        triggers = self._triggers()
        if self._trigger_idx < len(triggers):
            return triggers[self._trigger_idx]
        return None

    def _trigger_instruction(self, trigger: dict, *, probing: bool) -> str:
        """Turn a planted trigger into a stage direction for the actor. The cue
        is what should happen next; on_silence is the probe that keeps a silent
        participant from turning into missing data."""
        if probing and trigger.get("on_silence"):
            return (
                f"The participant has not engaged. Probe now, in character, with the "
                f"substance of: {trigger['on_silence']}"
            )
        return f"Bring about this beat now, in your own words: {trigger['cue']}"

    def _fire_trigger(self, trigger: dict, *, probing: bool) -> str:
        self._fired.append(trigger["id"])
        self.session.store.event(
            "trigger_fired",
            trigger_id=trigger["id"],
            interaction=self._interaction().get("id"),
            segment=self.segment,
            esci=trigger.get("esci", []),
            probing=probing,
            index=self._trigger_idx,
        )
        self._trigger_idx += 1
        return self._trigger_instruction(trigger, probing=probing)

    def _interaction_id(self) -> str:
        return self._interaction().get("id", f"i{self.segment + 1}")

    def _voice(self) -> str:
        """Each character keeps one voice for the whole encounter, so a
        participant hears the same person across interactions."""
        explicit = getattr(self.agent, "voice_id", None) or getattr(self.agent, "realtime_voice", None)
        if explicit:
            return explicit
        idx = next((i for i, a in enumerate(self.cast) if a.id == self.agent_id), 0)
        return GEMINI_VOICES[idx % len(GEMINI_VOICES)]

    async def _advance_segment(self) -> bool:
        """Move to the next beat. Within a one_to_one_series that means the next
        character in the same interaction; otherwise the next interaction.
        Returns False when the encounter is over."""
        agents = self._resolve_agents()

        # Still characters left in this series (e.g. Jordan then Casey).
        if self._interaction_mode() == "one_to_one_series" and self._series_idx + 1 < len(agents):
            self._series_idx += 1
            self._turns_this_interaction = 0
            self._interaction_started_at = time.time()
            await self._enter(agents[self._series_idx], new_interaction=False)
            return True

        if self.segment + 1 >= len(self.interactions):
            return False

        self.segment += 1
        self._series_idx = 0
        self._trigger_idx = 0
        self._turns_this_interaction = 0
        self._interaction_started_at = time.time()
        await self._enter(self._resolve_agents()[0], new_interaction=True)
        return True

    def _next_beat_hint(self) -> Optional[dict]:
        """Who comes next, so the UI can offer a way to move on."""
        agents = self._resolve_agents()
        if self._interaction_mode() == "one_to_one_series" and self._series_idx + 1 < len(agents):
            nxt = agents[self._series_idx + 1]
            return {"agent_id": nxt.id, "agent_name": nxt.name,
                    "label": self._interaction().get("label", "")}
        if self.segment + 1 < len(self.interactions):
            nxt_i = self.interactions[self.segment + 1]
            spec = nxt_i.get("agents") or nxt_i.get("agent")
            spec = [spec] if isinstance(spec, str) else (spec or [])
            by_id = {a.id: a for a in self.cast}
            names = [by_id[a].name for a in spec if a in by_id]
            return {"agent_id": spec[0] if spec else None,
                    "agent_name": " and ".join(names),
                    "label": nxt_i.get("label", "")}
        return None

    async def _announce_opening(self) -> None:
        """The first interaction needs the same scene banner as later ones."""
        if not self.interactions:
            return
        present = self._resolve_agents()
        if self._interaction_mode() == "one_to_one_series":
            present = [self.agent]
        payload = {
            "index": 0,
            "interaction": self._interaction_id(),
            "label": self._interaction().get("label", ""),
            "mode": self._interaction_mode(),
            "agent_id": self.agent_id,
            "agent_name": self.agent.name,
            "new_interaction": True,
            "present": [{"id": a.id, "name": a.name, "role": a.role} for a in present],
            "next": self._next_beat_hint(),
        }
        self.session.store.event("segment_start", **payload)
        await self._send({"type": "segment_start", **payload})

    async def _open_room(self) -> None:
        """Group interaction: one session per character, all listening."""
        await self._close_room()
        agents = self._resolve_agents()
        self.room = GroupRoom(
            agents,
            instructions_for=lambda a: self._instructions_for(a),
            voice_for=lambda a: self._voice_for(a),
            tools=[END_SEGMENT_TOOL],
        )
        await self.room.open()
        self.session.store.event(
            "group_room_opened", agents=[a.id for a in agents]
        )
        # One pump per character, so a reply is attributed to whoever produced
        # it rather than to whoever happens to hold a shared session.
        for a in agents:
            rt = self.room.session_for(a.id)
            if rt is not None:
                self._pumps.append(asyncio.ensure_future(self._pump_member(a, rt)))
        # No character opens unprompted: on this bridge a response can only
        # follow committed audio, and committing an empty buffer kills the
        # session. The participant speaks first; the scene brief sets that up.
        self.session.store.event("group_scene_awaits_participant", agent_id=agents[0].id)

    async def _close_room(self) -> None:
        for t in self._pumps:
            t.cancel()
        self._pumps = []
        if self.room is not None:
            await self.room.close()
            self.room = None

    def _instructions_for(self, agent) -> str:
        prev, self.agent, self.agent_id = self.agent, agent, agent.id
        try:
            return self._instructions()
        finally:
            self.agent, self.agent_id = prev, prev.id

    def _voice_for(self, agent) -> str:
        prev, self.agent, self.agent_id = self.agent, agent, agent.id
        try:
            return self._voice()
        finally:
            self.agent, self.agent_id = prev, prev.id

    async def _pump_member(self, agent, rt) -> None:
        """Relay one character's events, fully self-contained.

        Group pumps must not share turn state: the shared announce/finalize
        machinery attributed one speaker's words to another and merged three
        replies into a single labelled turn. Each pump tracks its own turn.
        """
        buf: List[str] = []
        announced = False
        try:
            async for ev in rt.events():
                etype = ev["type"]
                if etype == "agent_audio":
                    if not announced:
                        announced = True
                        await self._send({
                            "type": "assistant_started",
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                        })
                    self.session.store.append_assistant_audio(ev["pcm"], agent_id=agent.id)
                    await self._send_bytes(ev["pcm"])
                    if self.room:
                        await self.room.hear(ev["pcm"], exclude=agent.id)

                elif etype == "agent_transcript_delta":
                    if not announced:
                        announced = True
                        await self._send({
                            "type": "assistant_started",
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                        })
                    buf.append(ev["text"])
                    await self._send({
                        "type": "assistant_text_delta",
                        "text": ev["text"],
                        "agent_id": agent.id,
                    })

                elif etype == "user_transcript":
                    if self.room and agent.id != self.agent_order()[0]:
                        continue
                    await self._record_user_turn(ev["text"])

                elif etype == "response_done":
                    grace = time.time() + 2.5
                    while not buf and announced and time.time() < grace:
                        await asyncio.sleep(0.15)
                    text = "".join(buf).strip()
                    buf.clear()
                    if not announced and not text:
                        # Nothing at all came back: release the floor quietly
                        # rather than writing a blank turn.
                        self.session.store.event(
                            "empty_response", agent_id=agent.id, segment=self.segment
                        )
                        self._response_done.set()
                        continue
                    announced = False
                    await self._finalize_member(agent, text)

                elif etype == "error":
                    self.session.store.event(
                        "voice_error", where=f"room:{agent.id}", message=ev["message"]
                    )
        except asyncio.CancelledError:
            return

    async def _finalize_member(self, agent, text: str) -> None:
        """Close one character's turn in a group room.

        This was lost in a refactor once, and the symptom was total: every pump
        died with AttributeError at its first response.done, silently, so no
        reply ever reached the participant and every routed turn timed out.
        """
        self._turn_index += 1
        self._turns_this_interaction += 1

        if text:
            self.session.append_agent(agent.id, text)
            await self.session.broadcast({
                "type": "transcript", "role": "assistant",
                "agent_id": agent.id, "text": text,
            })
        else:
            self.session.store.event(
                "transcript_missing", agent_id=agent.id, segment=self.segment
            )
        self.session.store.event(
            "steering_pair",
            direction=self._pending_direction,
            actor={"agent_id": agent.id, "text": text,
                   "voice": getattr(agent, "voice_id", None),
                   "transcript_missing": not text},
            participant=self._last_user_text,
        )
        self._pending_direction = None
        self.session.store.event(
            "assistant_turn", agent_id=agent.id, text=text,
            segment=self.segment, transcript_missing=not text,
        )
        await self._send({"type": "assistant_done", "agent_id": agent.id})
        self._response_done.set()

    def agent_order(self) -> List[str]:
        return [a.id for a in self._resolve_agents()]

    async def _record_user_turn(self, text: str) -> None:
        if not text:
            return
        self._last_user_text = text
        self.session.append_user(text)
        self.session.store.event("user_turn", text=text, channel="voice")
        await self._send({"type": "user_transcript", "text": text, "final": True})
        await self.session.broadcast(
            {"type": "transcript", "role": "user", "text": text}
        )

    async def _switch_character(self, agent) -> None:
        """Start a fresh realtime session as `agent`.

        Re-briefing the existing session does not work: the conversation history
        keeps the model anchored to whoever it has been playing, and it will
        answer as that character no matter what the new instructions say, in
        testing, "Sam" opened with "I'm Riley, Sam's not here."

        A new session is also the right model of the scenario. The hallway
        run-in with Sam is a different scene; Sam was not present for the
        conversation with Riley and should not remember it.
        """
        old = self.rt
        self._switching = True
        self.rt = RealtimeVoiceSession(
            instructions=self._instructions(),
            voice=self._voice(),
            tools=[END_SEGMENT_TOOL],
        )
        await self.rt.connect()
        self.session.store.event(
            "realtime_session_switched", agent_id=agent.id, agent_name=agent.name
        )
        if old is not None:
            await old.close()   # ends the old pump; the outer loop picks up the new session

    async def _enter(self, agent, *, new_interaction: bool) -> None:
        changed = agent.id != self.agent_id or new_interaction
        self.agent = agent
        self.agent_id = agent.id
        self.vad.reset()
        self._speaking = False
        self._agent_text = []
        if self.is_group():
            await self._open_room()
            self.rt = self.room.session_for(self.agent_id) or self.rt
        elif changed:
            await self._close_room()
            await self._switch_character(agent)
        else:
            self.rt.voice = self._voice()
        present = self._resolve_agents()
        if self._interaction_mode() == "one_to_one_series":
            present = [agent]  # a series is one person at a time
        payload = {
            "index": self.segment,
            "interaction": self._interaction_id(),
            "label": self._interaction().get("label", ""),
            "mode": self._interaction_mode(),
            "agent_id": self.agent_id,
            "agent_name": self.agent.name,
            "new_interaction": new_interaction,
            # Who the participant is actually with now, so the UI can show only
            # them, otherwise every character stays on screen and it is unclear
            # who is being spoken to.
            "present": [{"id": a.id, "name": a.name, "role": a.role} for a in present],
            "next": self._next_beat_hint(),
        }
        self.session.store.event("segment_start", **payload)
        await self._send({"type": "segment_start", **payload})

    async def run(self) -> None:
        if self.is_group():
            await self._open_room()
            self.rt = self.room.session_for(self.agent_id) or None
        else:
            self.rt = RealtimeVoiceSession(
                instructions=self._instructions(),
                voice=self._voice(),
                tools=[END_SEGMENT_TOOL],
            )
            await self.rt.connect()
        await self._announce_opening()
        # Record what served this encounter, the audit trail has to say which
        # gateway and which models produced the data.
        self.session.store.event(
            "realtime_session_started", model=self.rt.model, **provenance()
        )
        try:
            await asyncio.gather(
                self._client_to_model(),
                self._model_to_client(),
                self._silence_watchdog(),
            )
        finally:
            self._closed = True
            await self._close_room()
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
                text = msg.get("text")
                if text:
                    await self._handle_client_command(text)
                    continue

                pcm = msg.get("bytes")
                if not pcm:
                    continue

                # Always record the participant channel, even while the agent
                # speaks, the study needs both sides of the audio.
                self.session.store.append_user_audio(pcm)

                mark = self.vad.feed(pcm)
                if mark:
                    self._last_activity = time.time()
                if mark == "speech_started":
                    await self._send({"type": "speech_started"})
                    if self._speaking:
                        # Barge-in: drop the agent's remaining audio.
                        await self.rt.cancel_response()
                        self._speaking = False
                        await self._send({"type": "assistant_interrupted"})

                if self.room is not None:
                    await self.room.hear(pcm)
                else:
                    await self.rt.send_audio(pcm)

                if mark == "turn_ended":
                    self._turn_started_at = time.time()
                    if self.is_group() and self.room is not None:
                        asyncio.ensure_future(self._run_group_turn())
                    else:
                        await self._brief_next_beat(probing=False)
                        await self.rt.commit_turn()
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001, surfaced in the session log
            self.session.store.event("voice_error", where="client_to_model", message=str(exc))

    # ── model -> participant ───────────────────────────────────────────────
    async def _model_to_client(self) -> None:
        """Relay model events, following the session across interaction changes.

        Each interaction gets a *new* realtime session (see _switch_character),
        so when one ends this loop picks up the next one.
        """
        while not self._closed:
            if self.room is not None:
                # Group interactions are pumped per character by _pump_member.
                await asyncio.sleep(0.5)
                continue
            rt = self.rt
            if rt is None:
                return
            await self._pump(rt)
            if not self._switching:
                return
            self._switching = False

    async def _pump(self, rt) -> None:
        async for ev in rt.events():
            etype = ev["type"]

            if etype == "agent_audio":
                await self._begin_agent_turn()
                self.session.store.append_assistant_audio(ev["pcm"], agent_id=self.agent_id)
                await self._send_bytes(ev["pcm"])

            elif etype == "agent_transcript_delta":
                # Transcript deltas usually arrive before the first audio chunk.
                # The client buffers them into the turn opened by
                # assistant_started, so that has to be sent first or the text is
                # dropped and the agent appears to say nothing.
                await self._begin_agent_turn()
                self._agent_text.append(ev["text"])
                await self._send({
                    "type": "assistant_text_delta",
                    "text": ev["text"],
                    "agent_id": self.agent_id,
                })

            elif etype == "user_transcript":
                # Gemini Live transcribes the participant for us, no separate
                # STT service. Send it to the participant's own socket (the
                # transcript panel listens for user_transcript) as well as
                # broadcasting to any researcher view.
                self._last_user_text = ev["text"]
                self.session.append_user(ev["text"])
                self.session.store.event("user_turn", text=ev["text"], channel="voice")
                await self._send({
                    "type": "user_transcript",
                    "text": ev["text"],
                    "final": True,
                })
                await self.session.broadcast(
                    {"type": "transcript", "role": "user", "text": ev["text"]}
                )

            elif etype == "response_done":
                # Do not finalise here. The gateway can deliver transcript
                # events AFTER response.done, so reading the buffer now yields
                # an empty turn, audio with no text, which is unscoreable.
                asyncio.ensure_future(self._finalize_turn())

            elif etype == "tool_call":
                self.session.store.event(
                    "tool_call", name=ev.get("name"), segment=self.segment
                )
                if not await self._advance_segment():
                    await self._send({"type": "encounter_complete"})
                    return

            elif etype == "error":
                self.session.store.event("voice_error", where="model", message=ev["message"])
                await self._send({"type": "error", "message": ev["message"]})

    async def _handle_client_command(self, raw: str) -> None:
        """Control messages from the participant UI."""
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("type") != "advance_interaction":
            return
        # The participant chose to move on. Their judgement about when a
        # conversation is finished is better than a turn counter, so this
        # bypasses the pacing gates, but the beats they skipped are recorded,
        # because an encounter that skipped scored moments must not look
        # complete.
        remaining = [t["id"] for t in self._triggers()[self._trigger_idx:]]
        self.session.store.event(
            "advance_requested",
            interaction=self._interaction_id(),
            turns=self._turns_this_interaction,
            seconds=round(time.time() - self._interaction_started_at, 1),
            skipped_triggers=remaining,
        )
        self._turns_this_interaction = 0
        if not await self._advance_segment():
            await self._send({"type": "encounter_complete"})

    async def _maybe_advance(self) -> None:
        """Move on once this interaction's planted beats are spent.

        The actor's end_conversation tool is the intended signal, but a
        character in the middle of a natural conversation rarely calls it, an
        encounter would then stall in interaction 1 and never reach the
        counterpart, which is where most of the scoring lives. So the runner
        also advances on its own once every trigger has fired and the
        conversation has run a couple more turns past the last one.
        """
        if self._next_trigger() is not None:
            return  # beats remain in this interaction

        # An encounter is meant to run 7-12 minutes across its interactions, so
        # firing the last planted trigger is a floor, not a finish line. Hold
        # the scene open until it has had both enough turns and enough time,
        # otherwise a scenario with one planted beat ends after three exchanges
        # and there is nothing for a rater to score.
        min_turns = int(os.getenv("INTERACTION_MIN_TURNS", "8"))
        min_seconds = float(os.getenv("INTERACTION_MIN_SECONDS", "180"))
        elapsed = time.time() - self._interaction_started_at
        if self._turns_this_interaction < max(min_turns, len(self._triggers()) + 2):
            return
        if elapsed < min_seconds:
            return

        self.session.store.event(
            "interaction_complete",
            interaction=self._interaction_id(),
            turns=self._turns_this_interaction,
            seconds=round(elapsed, 1),
        )
        self._turns_this_interaction = 0
        if not await self._advance_segment():
            await self._send({"type": "encounter_complete"})

    async def _finalize_turn(self) -> None:
        """Close out an agent turn once its transcript has settled.

        response.done can arrive before the transcript events that belong to the
        same reply. Finalising immediately produced turns with audio and no
        text, which are unscoreable and, because the old code skipped empty
        turns, vanished from the record entirely. So wait briefly for text, and
        if it truly never comes, still record the turn and mark it, so a gap is
        visible to verify_record instead of silently absent.
        """
        # A turn exists only if it was announced (first audio or text). The
        # gateway emits response.done more than once per reply, and without this
        # each duplicate would wait out the grace period and then log a phantom
        # empty turn.
        if self._finalizing or not self._speaking:
            return
        self._finalizing = True
        try:
            grace = float(os.getenv("TRANSCRIPT_GRACE_SECONDS", "3"))
            deadline = time.time() + grace
            while not self._agent_text and time.time() < deadline:
                await asyncio.sleep(0.15)

            text = "".join(self._agent_text).strip()
            self._agent_text = []
            self._speaking = False
            missing = not text

            self._turn_index += 1
            self._turns_this_interaction += 1

            if text:
                self.session.append_agent(self.agent_id, text)
                await self.session.broadcast({
                    "type": "transcript",
                    "role": "assistant",
                    "agent_id": self.agent_id,
                    "text": text,
                })
            else:
                self.session.store.event(
                    "transcript_missing", agent_id=self.agent_id, segment=self.segment
                )

            self.session.store.event(
                "steering_pair",
                direction=self._pending_direction,
                actor={
                    "agent_id": self.agent_id,
                    "text": text,
                    "voice": getattr(self.rt, "voice", None),
                    "transcript_missing": missing,
                },
                participant=self._last_user_text,
            )
            self._pending_direction = None

            latency = (
                round(time.time() - self._turn_started_at, 3)
                if self._turn_started_at else None
            )
            self.session.store.event(
                "assistant_turn", agent_id=self.agent_id, text=text,
                latency_s=latency, segment=self.segment, transcript_missing=missing,
            )
            await self._send({"type": "assistant_done", "agent_id": self.agent_id})
        finally:
            self._finalizing = False
            # Released only now, so a group's next speaker cannot start while
            # this turn is still settling.
            self._response_done.set()
            if not self.is_group():
                await self._steer()
                await self._maybe_advance()

    async def _begin_agent_turn(self) -> None:
        """Announce the speaker once per turn, on the first event of any kind."""
        if self._speaking:
            return
        self._speaking = True
        await self._send({
            "type": "assistant_started",
            "agent_id": self.agent_id,
            "agent_name": self.agent.name,
        })

    async def _brief_next_beat(self, *, probing: bool) -> None:
        """Re-brief the actor with the next planted trigger, and record it."""
        trigger = self._next_trigger()
        if trigger is None:
            return
        direction = self._fire_trigger(trigger, probing=probing)
        instructions = self._instructions() + (
            f"\n\nDIRECTOR NOTE (follow precisely, never mention): {direction}"
        )
        await self.rt.update_instructions(instructions)
        self._pending_direction = {
            "turn": self._turn_index,
            "segment": self.segment,
            "interaction": self._interaction_id(),
            "agent_id": self.agent_id,
            "agent_name": self.agent.name,
            "voice": getattr(self.rt, "voice", None),
            "stage_direction": direction,
            "trigger_id": trigger["id"],
            "esci": trigger.get("esci", []),
            "probing": probing,
            "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest()[:16],
            "director_model": provenance()["text_model"],
        }
        self.session.store.event("stage_direction", **self._pending_direction)

    async def _silence_watchdog(self) -> None:
        """If the participant says nothing for a while, prompt the actor to
        probe. Research Note v3: avoidance must become scoreable behaviour, not
        missing data."""
        idle = float(os.getenv("PROBE_AFTER_SECONDS", "12"))
        while not self._closed:
            await asyncio.sleep(idle)
            if self._closed or self._speaking or self.vad.speaking:
                continue
            if time.time() - self._last_activity < idle:
                continue
            if self.is_group():
                # Never session.update a room member outside its own turn: a
                # mid-stream re-brief silently mutes the session (this is what
                # made every routed speaker time out). Probing in rooms is a
                # routing concern, handled when the participant next speaks.
                continue
            trigger = self._next_trigger()
            if trigger is None or not trigger.get("on_silence"):
                continue
            self._last_activity = time.time()
            await self._brief_next_beat(probing=True)
            await self.rt.send_audio(b"\x00" * 3200)
            await self.rt.commit_turn()

    async def _speak_as(self, agent, intent: Optional[str] = None) -> None:
        """Give one character the floor: re-brief the session as them, with
        their own voice, then wait for their reply to finish."""
        self.agent = agent
        self.agent_id = agent.id
        self.rt.voice = getattr(agent, "realtime_voice", None) or GEMINI_VOICES[
            self.cast.index(agent) % len(GEMINI_VOICES)
        ]
        instructions = self._instructions()
        if intent:
            instructions += f"\n\nDIRECTOR NOTE (follow precisely, never mention): {intent}"
        await self.rt.update_instructions(instructions)

        # The steering log is part of the study record: what the director told
        # this actor, verbatim, before it spoke. Logged even when there is no
        # direction, so an unsteered turn is distinguishable from a lost one.
        self._pending_direction = {
            "turn": self._turn_index,
            "segment": self.segment,
            "interaction": self._interaction_id(),
            "agent_id": agent.id,
            "agent_name": agent.name,
            "voice": self.rt.voice,
            "stage_direction": intent,
            "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest()[:16],
            "director_model": provenance()["text_model"],
        }
        self.session.store.event("stage_direction", **self._pending_direction)

        # Wait for the previous character to finish before taking the floor,
        # the gateway allows only one active response per conversation, and a
        # dropped request would silently mute this speaker.
        if self.rt.responding:
            try:
                await asyncio.wait_for(self._response_done.wait(), timeout=20)
            except asyncio.TimeoutError:
                self.session.store.event("group_floor_stall", agent_id=agent.id)
                self.rt.clear_response_state()

        self._response_done.clear()
        # The gateway will not produce a second reply off an already-consumed
        # buffer, so hand it a brief silent frame to commit before asking the
        # next character to speak. Without this, every speaker after the first
        # simply never answers.
        await self.rt.send_audio(b"\x00" * 3200)
        await self.rt.commit_input()
        await self.rt.request_response()
        try:
            await asyncio.wait_for(self._response_done.wait(), timeout=45)
        except asyncio.TimeoutError:
            self.session.store.event("group_turn_timeout", agent_id=agent.id)
            self.rt.clear_response_state()

    async def _run_group_turn(self) -> None:  # noqa: C901
        """One participant turn in a group room: the director picks who speaks
        and in what order, then each character takes the floor in turn. The
        floor lock keeps a fast second participant turn from interleaving
        speakers mid-sequence."""
        if self.room is None:
            return
        # asyncio.Lock queues waiters, so a turn spoken while another is being
        # served waits its turn instead of being dropped.
        async with self._floor:
            order = self.agent_order()

            # Committing a session makes it reply, so the first commit both
            # transcribes the participant and produces the first response. Give
            # that to whoever the participant addressed by name if their words
            # are already known; otherwise the interaction's lead speaks first.
            named_early = self._named_in(self._last_user_text)
            first = named_early or order[0]

            self._response_done.clear()
            granted = await self.room.give_floor(first)
            try:
                await asyncio.wait_for(self._response_done.wait(), timeout=45)
            except asyncio.TimeoutError:
                rt_dbg = self.room.session_for(first)
                log = (rt_dbg.debug_log if rt_dbg else None) or []
                self.session.store.event(
                    "group_turn_timeout", agent_id=first,
                    granted=granted is not None,
                    still_in_room=rt_dbg is not None,
                    ws_open=bool(rt_dbg and rt_dbg.ws is not None),
                    events_seen=len(log),
                    tail=[et for _, et, _ in log[-8:]],
                )
                if rt_dbg is not None:
                    rt_dbg.clear_response_state()

            # The transcript arrived with that first commit; a direct address
            # we could not honour up front gets the next turn instead.
            named = self._named_in(self._last_user_text)
            followups = []
            if named and named != first:
                followups.append(named)
            else:
                try:
                    routed = await self.director.route(
                        self.session.shared_history, self._last_user_text
                    )
                except Exception as exc:  # noqa: BLE001, never break the room
                    self.session.store.event("director_error", message=str(exc))
                    routed = []
                followups = [
                    r.get("agent_id") for r in routed
                    if r.get("agent_id") in self.room.sessions
                    and r.get("agent_id") != first
                ][:1]

            self.session.store.event(
                "director_route", speakers=[first] + followups, addressed=named
            )
            for aid in followups:
                if self._closed:
                    break
                self._response_done.clear()
                await self.room.give_floor(aid)
                try:
                    await asyncio.wait_for(self._response_done.wait(), timeout=45)
                except asyncio.TimeoutError:
                    self.session.store.event("group_turn_timeout", agent_id=aid)
            self.room.speaking = None
            await self._steer()

    def _named_in(self, text: str) -> Optional[str]:
        """The character the participant addressed by name, if any."""
        if not text:
            return None
        lowered = text.lower()
        for a in self._resolve_agents():
            if a.name.lower() in lowered:
                return a.id
        return None

    # ── director ───────────────────────────────────────────────────────────
    async def _steer(self) -> None:
        """Closed-loop steering between turns.

        The director reviews the transcript and shifts persona knobs; the actor
        is then re-briefed with the updated persona, which is how a stage
        direction reaches a speech-to-speech model that has no separate system
        channel. Steering is one turn behind by construction, the
        participant's words only exist once the model has transcribed them.

        Session.auto_steer() owns the review and swallows its own errors, so a
        steering failure can never break a live encounter.
        """
        before = len(self.session.steering_log)
        await self.session.auto_steer()
        if len(self.session.steering_log) == before:
            return  # nothing changed; the current brief still stands
        if self.is_group():
            # Same mid-stream mute risk as the watchdog: room members keep
            # their opening brief; steering shifts are recorded for the log.
            return
        await self.rt.update_instructions(self._instructions())

    # ── transport helpers ──────────────────────────────────────────────────
    async def _send(self, payload: dict) -> None:
        try:
            await self.ws.send_json(payload)
        except Exception:  # noqa: BLE001, client vanished
            self._closed = True

    async def _send_bytes(self, payload: bytes) -> None:
        try:
            await self.ws.send_bytes(payload)
        except Exception:  # noqa: BLE001
            self._closed = True
