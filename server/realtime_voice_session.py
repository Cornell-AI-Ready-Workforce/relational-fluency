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
import re
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Dict, List, Optional

from .director import Director


class _MemberState:
    """Per-character pump state for hold-and-adopt turn taking."""

    def __init__(self) -> None:
        self.mode = "idle"        # idle | holding | held_done | live | discarding
        self.held: list = []      # [("audio", bytes) | ("text", str)]
        self.text: list = []      # live transcript deltas
        self.announced = False
        self.relayed_bytes = 0
        self.last_output_at = 0.0
        self.done_at = 0.0
        self.play_start: Optional[float] = None
        self.play_end: Optional[float] = None
        self.hold_id = None
        self.hold_started_at = 0.0

    def begin_hold(self, response_id=None) -> None:
        self.mode = "holding"
        self.held = []
        self.hold_id = response_id
        self.hold_started_at = time.time()

    def hold(self, ev: dict) -> None:
        if ev["type"] == "agent_audio":
            self.held.append(("audio", ev["pcm"]))
        else:
            self.held.append(("text", ev["text"]))

    def held_seconds(self) -> float:
        return sum(len(c) for k, c in self.held if k == "audio") / 32000.0

    def take_held(self) -> list:
        held, self.held = self.held, []
        return held

    def drop(self, why: str = "") -> None:
        self.held = []
        self.mode = "idle"


def _clean_agent_text(text: str) -> str:
    """Collapse transcript repeats the bridge sometimes delivers.

    The audio plays once, but the output transcript can arrive twice: either
    the whole turn doubled ("It's a slippery slope.It's a slippery slope.")
    or an earlier sentence, or the start of one, re-sent at the end. Raters
    read this text, so drop the copy. Comparison ignores case and
    punctuation, since the two copies often differ by a comma; a character
    genuinely repeating themselves in different words is left alone.
    """
    t = (text or "").strip()
    n = len(t)
    if n < 20:
        return t
    # Whole-turn double, with or without a space at the seam.
    for k in range(min(n - 1, n // 2 + 4), max(0, n // 2 - 5), -1):
        a, b = t[:k].strip(), t[k:].strip()
        if a and _norm_speech(a) == _norm_speech(b):
            return a
    # Trailing sentence or fragment that already appeared earlier in the turn.
    parts = [x for x in re.split(r"(?<=[.!?])\s*", t) if x]
    changed = False
    while len(parts) > 1 and len(parts[-1]) >= 20:
        tail = _norm_speech(parts[-1])
        head = _norm_speech(" ".join(parts[:-1]))
        if tail and tail in head:
            parts.pop()
            changed = True
        else:
            break
    return " ".join(parts) if changed else t


def _script_mismatch(text: str) -> bool:
    """True when the transcript is mostly non-Latin script.

    The gateway transcriber occasionally mis-detects the language and
    transliterates English speech into another script (Devanagari has been
    seen: "रिवर्स टीम" for "Rivera's team"). The model still understood the
    audio; only the caption is wrong. Language hints in session.update are
    accepted and ignored, so this is detected after the fact.
    """
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 6:
        return False
    latin = sum(1 for c in letters if c.isascii())
    return latin / len(letters) < 0.5


def _is_stage_direction(text: str) -> bool:
    """'[Priya remains quiet.]' or '[Silence]': the model narrating instead of
    speaking. Treated as no reply; the native-audio route does this sometimes."""
    t = (text or "").strip()
    return bool(t) and t.startswith(("[", "(", "*")) and t.endswith(("]", ")", "*")) and len(t) < 80


def _norm_speech(text: str) -> str:
    """Lowercase, strip punctuation: comparable across transcriber quirks."""
    return " ".join("".join(c if c.isalnum() or c.isspace() else " "
                            for c in text.lower()).split())


def _is_echo(user_norm: str, agent_norm: str) -> bool:
    """True when the participant 'turn' is mostly an agent's recent line.

    With the mic open while agents speak, echo of the playback can come back
    transcribed as participant speech. Overlap is judged on word containment,
    so partial echoes ('right no not at the moment i think we've covered the
    main points') are caught even when the transcriber adds a word or two.
    """
    if not user_norm or not agent_norm:
        return False
    uw, aw = user_norm.split(), agent_norm.split()
    if len(uw) < 4:
        return False
    aset = set(aw)
    overlap = sum(1 for w in uw if w in aset) / len(uw)
    return overlap >= 0.8
from .llm import provenance
from .group_room import GroupRoom
from .voice.realtime import RealtimeVoiceSession, SilenceDetector, is_openai_realtime, autofire_wait_for_model, accepts_text_items

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
        self._last_user_norm = ""
        self._last_user_at = 0.0
        self._recent_agent_texts: List[tuple] = []   # (agent_id, text), last 6
        self._last_group_speaker: Optional[str] = None
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
        self._member_states: Dict[str, _MemberState] = {}
        self._speech_started_at = 0.0
        self._barged = False
        # Held replies are flushed to the client faster than real time, so the
        # server can finish a turn seconds before the participant has heard
        # it. This clock tracks when audio already sent will finish playing,
        # which is what "heard" has to mean for interruptions.
        self._play_cursor = 0.0
        self._last_played: Optional[dict] = None   # {agent_id, start, end, text}
        self._turns_without_transcript = 0
        self._scribe_pump: Optional[asyncio.Task] = None
        self._floor = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def _instructions(self, director_note: str = "") -> str:
        # Reuse the engine's prompt builder so the voice agent and the text
        # agent are the same character, persona knobs, branches, and the
        # director's intent all compose exactly as they do in text mode.
        engine = self.session.engines[self.agent_id]
        base = engine._system_prompt(self.session.triggered_branches, director_note or None)
        voice_rules = (
            "\n\nVOICE: You are in a live spoken conversation. Keep every turn "
            "The participant speaks English. Always speak English, whatever "
            "language you think you heard. "
            "SHORT: one or two spoken sentences, at most about 25 words, then "
            "stop and let others respond. Make one point per turn, never a "
            "list of points. Never monologue. Never read out JSON, markdown, "
            "or stage directions. Never narrate in brackets like [remains quiet]; "
            "if you have nothing to add, say a short spoken line instead."
        )
        if self.is_group():
            voice_rules += (
                "\nMEETING: Several people share this room. If the participant "
                "addresses someone else by name, stay silent and let them "
                "answer. Do not repeat or rephrase what another person just "
                "said, and do not answer every turn: leave room for quieter "
                "colleagues."
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
        # No end_conversation tool for room members: the group pump never
        # acted on it, group scenes end via the participant's button and the
        # pacing gates, and the native-audio model calls it constantly, which
        # produced empty turns instead of replies (raw bridge log, 2026-09-08).
        self.room = GroupRoom(
            agents,
            instructions_for=lambda a: self._instructions_for(a),
            voice_for=lambda a: self._voice_for(a),
            tools=[],
        )
        await self.room.open()
        self.session.store.event(
            "group_room_opened", agents=[a.id for a in agents]
        )
        # One pump per character, so a reply is attributed to whoever produced
        # it rather than to whoever happens to hold a shared session.
        self._member_states = {a.id: _MemberState() for a in agents}
        for a in agents:
            rt = self.room.session_for(a.id)
            if rt is not None:
                self._pumps.append(asyncio.ensure_future(self._pump_member(a, rt)))
        if self.room.scribe is not None:
            self._scribe_pump = asyncio.ensure_future(self._pump_scribe(self.room.scribe))
            self._pumps.append(self._scribe_pump)
        # No character opens unprompted: on this bridge a response can only
        # follow committed audio, and committing an empty buffer kills the
        # session. The participant speaks first; the scene brief sets that up.
        self.session.store.event("group_scene_awaits_participant", agent_id=agents[0].id)

    async def _close_room(self) -> None:
        for t in self._pumps:
            t.cancel()
        self._pumps = []
        if self.room is not None and os.getenv("RT_DEBUG"):
            # Raw bridge events per member, for diagnosing route behaviour.
            try:
                sdir = self.session.store.dir
                for aid, rt in list(self.room.sessions.items()) + [("scribe", self.room.scribe)]:
                    if rt is None or not rt.debug_log:
                        continue
                    with open(sdir / f"raw_{aid}.jsonl", "w", encoding="utf-8") as fh:
                        for ts, et, raw in rt.debug_log:
                            fh.write(json.dumps({"t": round(ts, 3), "type": et, "raw": raw}) + "\n")
            except Exception:  # noqa: BLE001
                pass
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
        """Relay one character's events, holding replies until the floor is decided.

        The bridge fires its own reply on EVERY member session after the
        participant's speech + silence, before the director has chosen who
        speaks. The old pump discarded those and then asked the chosen member
        for a fresh reply with commit + response.create. That second request
        raced the first: glued text ("We do haveNo there's nothing..."), empty
        replies from a commit of padding silence, and members left wedged
        mid-response (Jordan cut at 1.2 s and mute for the rest of the scene).

        Now each pump HOLDS its member's auto-fired reply (audio + text) in
        memory. When the director grants that member the floor, the held reply
        is played from the start, no second generation. Members not granted
        the floor have their held reply dropped once it completes (or after a
        short grace period so a slow routing decision can still adopt it).
        """
        st = self._member_states[agent.id]
        try:
            async for ev in rt.events():
                etype = ev["type"]
                has_floor = self.room is not None and self.room.speaking == agent.id

                if etype in ("agent_audio", "agent_transcript_delta"):
                    st.last_output_at = time.time()
                    if st.mode == "discarding":
                        continue
                    if st.mode == "held_done":
                        # A new response began before the held one was adopted
                        # or dropped: the held one is stale now.
                        st.drop("superseded")
                    if has_floor:
                        if st.mode == "holding":
                            await self._flush_held(agent, st)
                        elif st.mode == "idle":
                            st.mode = "live"
                            await self._announce(agent, st)
                        await self._relay(agent, st, ev)
                    else:
                        if st.mode == "live":
                            # Floor withdrawn mid-reply (barge-in): stop relaying
                            # and close the turn as interrupted.
                            await self._finish_interrupted(agent, st)
                            st.mode = "discarding"
                            continue
                        rid = ev.get("response_id")
                        if st.mode == "holding" and rid and st.hold_id and rid != st.hold_id:
                            # A second response began while the first was held:
                            # keep the newer one whole rather than interleaving.
                            st.begin_hold(rid)
                        if st.mode == "idle":
                            st.begin_hold(rid)
                        if st.mode == "holding":
                            st.hold(ev)
                            if st.held_seconds() > 40:
                                st.drop("held_too_long")
                                self.session.store.event(
                                    "unsolicited_response_suppressed", agent_id=agent.id
                                )
                                st.mode = "discarding"
                    continue

                if etype == "user_transcript":
                    # On routes where colleagues arrive as text, a member hears
                    # only the participant, so its transcription is a clean
                    # second source (deduped in _record_user_turn). On the
                    # original route members also hear the other characters,
                    # so only the scribe counts there.
                    if accepts_text_items(rt.model):
                        await self._record_user_turn(ev["text"])
                    continue

                if etype == "response_done":
                    rt.pending_input = 0     # the bridge consumed the buffer
                    if st.mode == "live":
                        if has_floor:
                            await self._finish_live(agent, st)
                        else:
                            # Floor withdrawn (barge-in) and the bridge stopped
                            # right away: close as interrupted, not as spoken.
                            await self._finish_interrupted(agent, st)
                        st.mode = "idle"
                    elif st.mode == "holding":
                        st.mode = "held_done"
                        st.done_at = time.time()
                    elif st.mode == "discarding":
                        st.mode = "idle"
                        self.session.store.event(
                            "unsolicited_response_suppressed", agent_id=agent.id
                        )
                    elif st.mode == "held_done":
                        pass
                    else:  # idle: a response that produced nothing at all
                        self.session.store.event(
                            "empty_response", agent_id=agent.id, segment=self.segment
                        )
                        if has_floor:
                            self._response_done.set()
                    continue

                if etype == "error":
                    self.session.store.event(
                        "voice_error", where=f"room:{agent.id}", message=ev["message"]
                    )
        except asyncio.CancelledError:
            return

    async def _announce(self, agent, st) -> None:
        st.announced = True
        st.relayed_bytes = 0
        st.text = []
        st.play_start = None
        st.play_end = None
        await self._send({
            "type": "assistant_started", "agent_id": agent.id, "agent_name": agent.name,
        })

    async def _relay(self, agent, st, ev) -> None:
        if ev["type"] == "agent_audio":
            pcm = ev["pcm"]
            st.relayed_bytes += len(pcm)
            now = time.time()
            start = max(now, self._play_cursor)
            if st.play_start is None:
                st.play_start = start
            self._play_cursor = start + len(pcm) / 32000.0
            st.play_end = self._play_cursor
            self.session.store.append_assistant_audio(pcm, agent_id=agent.id)
            await self._send_bytes(pcm)
            if self.room:
                await self.room.hear(pcm, exclude=agent.id)
        else:
            st.text.append(ev["text"])
            await self._send({
                "type": "assistant_text_delta", "text": ev["text"], "agent_id": agent.id,
            })

    async def _flush_held(self, agent, st) -> None:
        """The member was granted the floor: play what it already said."""
        held = st.take_held()
        st.mode = "live"
        await self._announce(agent, st)
        self.session.store.event(
            "held_reply_adopted", agent_id=agent.id,
            held_seconds=round(len(b"".join(c for k, c in held if k == "audio")) / 32000, 1),
        )
        for kind, chunk in held:
            await self._relay(agent, st, {"type": "agent_audio", "pcm": chunk} if kind == "audio"
                              else {"type": "agent_transcript_delta", "text": chunk})

    async def adopt_member(self, agent_id: str) -> bool:
        """Grant path: adopt a held or completed reply if there is one."""
        st = self._member_states.get(agent_id)
        agent = next((a for a in self._resolve_agents() if a.id == agent_id), None)
        if st is None or agent is None:
            return False
        if st.mode in ("holding", "held_done") and st.hold_started_at < self._speech_started_at:
            # Began before the participant's current utterance: it is a
            # reaction to a colleague's audio, not an answer to the question.
            st.drop("stale")
            if st.mode == "holding":
                st.mode = "discarding"
            self.session.store.event("stale_held_reply_dropped", agent_id=agent_id)
            return False
        if st.mode == "holding":
            await self._flush_held(agent, st)
            return True
        if st.mode == "held_done":
            if time.time() - st.done_at > float(os.getenv("HELD_REPLY_TTL", "12")):
                st.drop("stale")
                self.session.store.event("unsolicited_response_suppressed", agent_id=agent_id)
                return False
            await self._flush_held(agent, st)
            await self._finish_live(agent, st)
            st.mode = "idle"
            return True
        return False

    async def _finish_live(self, agent, st) -> None:
        # Transcript deltas can trail the last audio chunk slightly.
        grace = time.time() + 2.5
        while not st.text and st.announced and time.time() < grace:
            await asyncio.sleep(0.15)
        text = "".join(st.text).strip()
        st.text = []
        st.announced = False
        self._last_played = {
            "agent_id": agent.id, "start": st.play_start, "end": st.play_end, "text": text,
        }
        await self._finalize_member(agent, text)

    def _heard_seconds(self, st) -> float:
        """How much of this turn's audio the participant has actually heard."""
        if st.play_start is None:
            return 0.0
        end = st.play_end if st.play_end is not None else st.play_start
        return max(0.0, min(end, time.time()) - st.play_start)

    async def _finish_interrupted(self, agent, st) -> None:
        """Close a turn the participant cut off, keeping only what was heard.

        Text streams well ahead of audio, so the buffer usually holds the whole
        sentence while only its first seconds were played. Keep roughly the
        words that fit in the relayed audio (about 2.5 words/second), so the
        record does not credit the character with lines nobody heard.
        """
        text = "".join(st.text).strip()
        heard_s = self._heard_seconds(st)
        words = text.split()
        keep = max(1, int(heard_s * 2.5)) if words else 0
        if keep < len(words):
            text = " ".join(words[:keep]) + "…"
        st.text = []
        st.announced = False
        self.session.store.event(
            "assistant_interrupted", agent_id=agent.id, heard_seconds=round(heard_s, 1),
        )
        await self._finalize_member(agent, text, interrupted=True)

    async def _pump_scribe(self, rt) -> None:
        """Relay the scribe's participant transcripts; swallow everything else.

        The scribe only ever hears the participant, so its input transcription
        is the clean user channel. The bridge auto-fires a response on any
        session after speech + silence, scribe included; those responses are
        cancelled unheard.
        """
        try:
            async for ev in rt.events():
                etype = ev.get("type")
                if etype == "user_transcript":
                    await self._record_user_turn(ev["text"])
                elif etype in ("agent_audio", "agent_transcript_delta"):
                    try:
                        await rt.cancel_response()
                    except Exception:  # noqa: BLE001
                        pass
                elif etype == "response_done":
                    rt.clear_response_state()
        except asyncio.CancelledError:
            return

    async def _finalize_member(self, agent, text: str, interrupted: bool = False) -> None:
        """Close one character's turn in a group room."""
        text = _clean_agent_text(text)
        if _is_stage_direction(text):
            self.session.store.event("stage_direction_output", agent_id=agent.id, text=text)
            text = ""
        await self._finalize_member_inner(agent, text, interrupted)

    async def _finalize_member_inner(self, agent, text: str, interrupted: bool = False) -> None:
        """Close one character's turn in a group room.

        This was lost in a refactor once, and the symptom was total: every pump
        died with AttributeError at its first response.done, silently, so no
        reply ever reached the participant and every routed turn timed out.
        """
        self._turn_index += 1
        self._turns_this_interaction += 1

        if text:
            self.session.append_agent(agent.id, text)
            self._recent_agent_texts = (self._recent_agent_texts + [(agent.id, text)])[-6:]
            await self.session.broadcast({
                "type": "transcript", "role": "assistant",
                "agent_id": agent.id, "text": text,
            })
        self._last_group_speaker = agent.id
        if not text:
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
            segment=self.segment, transcript_missing=not text, interrupted=interrupted,
        )
        if self.room is not None and text:
            await self.room.tell(agent.name, text, exclude=agent.id)
        await self._send({"type": "assistant_done", "agent_id": agent.id})
        self._response_done.set()

    def agent_order(self) -> List[str]:
        return [a.id for a in self._resolve_agents()]

    async def _record_user_turn(self, text: str) -> None:
        if not text:
            return
        now = time.time()
        norm = _norm_speech(text)
        # The bridge can deliver the same utterance twice (append + commit);
        # record it once.
        if norm and norm == self._last_user_norm and now - self._last_user_at < 12:
            return
        # Echo guard: an agent's line played over speakers can come back
        # transcribed as participant speech (Chrome's AEC does not cancel
        # WebAudio playback). It must not enter the record or steer routing.
        for aid, atext in self._recent_agent_texts:
            if _is_echo(norm, _norm_speech(atext)):
                self.session.store.event("echo_dropped", matches=aid, text=text)
                return
        self._last_user_norm, self._last_user_at = norm, now
        self._last_user_text = text
        self.session.append_user(text)
        unclear = _script_mismatch(text)
        self.session.store.event(
            "user_turn", text=text, channel="voice", script_mismatch=unclear
        )
        # The research record keeps the raw text (retranscribe repairs it
        # offline); the participant only sees a neutral caption, since a line
        # of foreign script reads as "the app is broken".
        await self._send({
            "type": "user_transcript", "text": text, "final": True, "unclear": unclear,
        })
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
        # The first loop to finish ends the encounter (normally the client
        # socket closing). With a plain gather the watchdog kept looping and
        # the finally never ran: rooms and gateway sessions leaked on every
        # disconnect, and the registry never dropped the session.
        tasks = [
            asyncio.ensure_future(self._client_to_model()),
            asyncio.ensure_future(self._model_to_client()),
            asyncio.ensure_future(self._silence_watchdog()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            for t in done:
                exc = t.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    raise exc
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
                    self._speech_started_at = time.time()
                    self._barged = False
                    if self.room is not None:
                        await self._cancel_stale_holds()
                sustained = (self.room is not None and self.vad.speaking and not self._barged
                             and time.time() - self._speech_started_at
                             >= float(os.getenv("BARGE_IN_MS", "600")) / 1000.0)
                if sustained and not self.room.speaking and time.time() < self._play_cursor:
                    # The server-side turn is over but the participant is still
                    # hearing it: stop the client's playback and note how much
                    # of that line was actually heard.
                    self._barged = True
                    lp = self._last_played or {}
                    heard = 0.0
                    if lp.get("start") is not None:
                        heard = max(0.0, time.time() - lp["start"])
                    total = (lp.get("end") or 0) - (lp.get("start") or 0)
                    words = (lp.get("text") or "").split()
                    heard_words = len(words) if total <= 0 else min(len(words), int(len(words) * heard / total))
                    self.session.store.event(
                        "playback_cut", agent_id=lp.get("agent_id"),
                        heard_seconds=round(heard, 1), total_seconds=round(total, 1),
                        heard_text=" ".join(words[:heard_words]) + ("…" if heard_words < len(words) else ""),
                    )
                    self._play_cursor = time.time()
                    await self._send({"type": "assistant_interrupted"})
                if sustained and self.room.speaking:
                    # A real meeting yields to an interjection, but not to a
                    # "mm-hm": only sustained speech takes the floor. Withdraw
                    # the floor; the speaker's pump closes its turn as
                    # interrupted with only the words that were heard.
                    self._barged = True
                    speaker_id = self.room.speaking
                    self.room.speaking = None
                    speaker = self.room.session_for(speaker_id)
                    if speaker is not None:
                        try:
                            await speaker.cancel_response()
                        except Exception:  # noqa: BLE001
                            pass
                    await self._send({"type": "assistant_interrupted"})
                    self._response_done.set()
                if mark == "speech_started" and self.room is None and self._speaking:
                    # 1:1 barge-in: drop the agent's remaining audio.
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
                        # On the OpenAI route (server VAD off) the scribe only
                        # transcribes a committed buffer; commit it at our turn
                        # end. The Gemini route transcribes on append.
                        scribe = self.room.scribe
                        if scribe is not None and is_openai_realtime(scribe.model):
                            try:
                                await scribe.commit_input()
                            except Exception:  # noqa: BLE001
                                pass
                        asyncio.ensure_future(self._run_group_turn())
                    else:
                        await self._brief_next_beat(probing=False)
                        # The bridge auto-fires a reply after speech + silence,
                        # usually within a second of our own turn detection.
                        # Committing on top of it yields two replies, both
                        # spoken and both transcribed. Give it a moment to
                        # start; commit only if nothing came.
                        deadline = time.time() + float(os.getenv("AUTOFIRE_WAIT", "1.5"))
                        while time.time() < deadline and not self.rt.autofire_active:
                            await asyncio.sleep(0.05)
                        if self.rt.autofire_active:
                            self.session.store.event("autofire_adopted", agent_id=self.agent_id)
                        else:
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
                # STT service. _record_user_turn forwards it to the client and
                # researcher views, and drops duplicates and playback echo.
                await self._record_user_turn(ev["text"])

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

            text = _clean_agent_text("".join(self._agent_text))
            self._agent_text = []
            self._speaking = False
            missing = not text

            self._turn_index += 1
            self._turns_this_interaction += 1

            if text:
                self.session.append_agent(self.agent_id, text)
                self._recent_agent_texts = (self._recent_agent_texts + [(self.agent_id, text)])[-6:]
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

    async def _cancel_stale_holds(self) -> None:
        """The participant started a new utterance: every reply a non-floor
        member is still generating is a reaction to a colleague, now stale.

        Cancel it at the bridge rather than just dropping it. On the
        native-audio route a member whose response is still active when the
        participant speaks never fires a reply to the new turn: the speech is
        absorbed into the running response and the fallback request comes
        back empty. Cancelling frees the model for the participant's turn.
        """
        for aid, st in self._member_states.items():
            if self.room is None or self.room.speaking == aid:
                continue
            if st.mode == "holding":
                st.drop("participant_speaking")
                st.mode = "discarding"
                rt = self.room.session_for(aid)
                if rt is not None:
                    try:
                        await rt.cancel_response()
                    except Exception:  # noqa: BLE001
                        pass
                self.session.store.event("hold_cancelled_participant_speaking", agent_id=aid)
            elif st.mode == "held_done":
                st.drop("participant_speaking")

    async def _grant(self, agent_id: str):
        """Give a character the floor, preferring the reply it already made.

        Sets the floor, adopts a held/finished auto-fired reply if there is
        one, waits briefly for one to begin if not, and only then asks the
        bridge for a fresh reply (the old commit + create path).
        """
        if self.room is None:
            return None
        self.room.speaking = agent_id
        if await self.adopt_member(agent_id):
            return self.room.session_for(agent_id)
        rt = self.room.session_for(agent_id)
        wait = autofire_wait_for_model(rt.model if rt else "")
        deadline = time.time() + wait
        while time.time() < deadline:
            st = self._member_states.get(agent_id)
            if st is not None and st.mode in ("holding", "live"):
                if st.hold_started_at >= self._speech_started_at or st.mode == "live":
                    if st.mode == "holding":
                        await self.adopt_member(agent_id)
                    return self.room.session_for(agent_id)
            if rt is not None and rt.autofire_active and time.time() - rt._last_output_at < 10:
                # response.created arrived; its first audio is on the way.
                deadline = max(deadline, time.time() + 1.0)
            await asyncio.sleep(0.05)
        self.session.store.event("fresh_reply_requested", agent_id=agent_id)
        return await self.room.give_floor(agent_id)

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

            # The transcript arrives while the participant is still speaking,
            # so by turn end their words are usually known. Give the floor to
            # whoever they addressed by name; otherwise let the director pick
            # from what was actually said, falling back to the interaction's
            # lead. This is what makes the room feel responsive rather than the
            # same character answering everything.
            # Route on THIS turn's words, not the previous turn's.
            # _last_user_text is almost never empty mid-conversation, so
            # waiting for it to be non-empty returned immediately with stale
            # text: the participant said "Priya" and the director routed on
            # whatever they had said the turn before. Wait for it to CHANGE.
            before = self._last_user_text
            deadline = time.time() + float(os.getenv("ROUTE_TRANSCRIPT_WAIT", "6"))
            while self._last_user_text == before and time.time() < deadline:
                await asyncio.sleep(0.15)
            fresh = self._last_user_text if self._last_user_text != before else ""
            # Scribe watchdog: the participant spoke (the VAD ended a turn) but
            # no transcript came from anywhere. Twice in a row means the scribe
            # session has gone dead (seen on the native-audio route after ~6
            # turns in production); replace it.
            if fresh:
                self._turns_without_transcript = 0
            else:
                self._turns_without_transcript += 1
                if self._turns_without_transcript >= 2 and self.room is not None and self.room.scribe is not None:
                    self._turns_without_transcript = 0
                    try:
                        new_scribe = await self.room.reopen_scribe()
                        if self._scribe_pump is not None:
                            self._scribe_pump.cancel()
                        self._scribe_pump = asyncio.ensure_future(self._pump_scribe(new_scribe))
                        self._pumps.append(self._scribe_pump)
                        self.session.store.event("scribe_reconnected")
                    except Exception as exc:  # noqa: BLE001
                        self.session.store.event("scribe_reconnect_failed", message=str(exc))

            named_early = self._named_in(fresh)
            first = named_early
            if first is None:
                try:
                    routed = await self.director.route(
                        self.session.shared_history, fresh
                    )
                    candidates = [
                        r.get("agent_id") for r in routed
                        if r.get("agent_id") in self.room.sessions
                    ]
                    # Anti-dominance: absent a direct address, prefer a
                    # candidate who did not just speak. If the director's only
                    # candidate is the character who just spoke, do not let
                    # them answer again: rotate to the next member instead
                    # (in production the director handed Dan four of five
                    # unnamed turns this way).
                    first = next(
                        (c for c in candidates if c != self._last_group_speaker), None,
                    )
                    if first is None and candidates:
                        if self._last_group_speaker in order and len(order) > 1:
                            nxt = (order.index(self._last_group_speaker) + 1) % len(order)
                            first = order[nxt]
                            self.session.store.event(
                                "dominance_rotated", from_agent=candidates[0], to_agent=first,
                            )
                        else:
                            first = candidates[0]
                except Exception as exc:  # noqa: BLE001
                    self.session.store.event("director_error", message=str(exc))
            if first is None:
                # No signal at all: rotate the floor instead of always
                # falling back to the cast's first-listed character.
                if self._last_group_speaker in order and len(order) > 1:
                    nxt = (order.index(self._last_group_speaker) + 1) % len(order)
                    first = order[nxt]
                else:
                    first = order[0]

            self._response_done.clear()
            granted = await self._grant(first)
            try:
                await asyncio.wait_for(self._response_done.wait(), timeout=45)
                if self.room is not None and self.room.speaking == first:
                    self.room.speaking = None
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
            # we could not honour up front gets the next turn instead. Only a
            # name from THIS turn counts, a name said last turn is history.
            if self._last_user_text != before:
                fresh = self._last_user_text
            named = self._named_in(fresh)
            followups = []
            if named and named != first:
                followups.append(named)
            else:
                try:
                    routed = await self.director.route(
                        self.session.shared_history, fresh
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
                if self._closed or self.vad.speaking:
                    if self.vad.speaking:
                        self.session.store.event(
                            "followup_yielded", agent_id=aid,
                        )
                    break
                self._response_done.clear()
                await self._grant(aid)
                try:
                    await asyncio.wait_for(self._response_done.wait(), timeout=45)
                    if self.room is not None and self.room.speaking == aid:
                        self.room.speaking = None
                except asyncio.TimeoutError:
                    self.session.store.event("group_turn_timeout", agent_id=aid)
            self.room.speaking = None
            await self._steer()

    def _named_in(self, text: str) -> Optional[str]:
        """The character the participant addressed by name, if any."""
        if not text:
            return None
        lowered = text.lower()
        last_pos, last_id = -1, None
        for a in self._resolve_agents():
            pos = lowered.rfind(a.name.lower())
            if pos > last_pos:
                last_pos, last_id = pos, a.id
        # People address the target last: "Sorry to cut in, Alex, but I want
        # to hear from Jordan first. Jordan, how are you?" is for Jordan.
        return last_id

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
