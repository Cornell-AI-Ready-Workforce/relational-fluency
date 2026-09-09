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
import difflib
import re
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, List, Optional

from .director import Director, DIRECTOR_MAX_SPEAKERS


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


def _norm_speech(text: str) -> str:
    """Lowercase, strip punctuation: comparable across transcriber quirks."""
    return " ".join("".join(c if c.isalnum() or c.isspace() else " "
                            for c in text.lower()).split())


ECHO_WINDOW_SECONDS = 3.0


def _is_echo(user_norm: str, agent_norm: str) -> bool:
    """True when the participant 'turn' is a replay of an agent's recent line.

    With the mic open while agents speak, echo of the playback can come back
    transcribed as participant speech. The test used to be set containment over
    four words or more, and that is far too loose for what it guards. A
    participant who mirrors the character back — "so the decision is already
    made?", "we should push it back" — reuses the character's own vocabulary in
    their own order, scored over 0.8 on containment, and was deleted from the
    record while their voice stayed in user_audio.wav. Mirroring and
    checking-understanding are precisely the behaviours several ESCI items exist
    to observe, so this asks for what playback echo actually looks like rather
    than for vocabulary overlap.

    A SINGLE longest matching block was the first attempt at that and was too
    brittle in the other direction: one word inserted, dropped or mis-heard in
    the middle of a re-transcribed echo — which is what a transcriber does to
    speaker playback more often than not — halves the longest run, scores 0.53,
    and the character's own sentence sails through as participant speech. So
    measure ALL of difflib's matching blocks instead. They are non-overlapping
    and monotonically increasing in both sequences, so order is still enforced;
    requiring each run to be at least two words keeps scattered single-word
    vocabulary overlap (which is exactly what paraphrase produces) from counting.

    Two coverages are then required, and the second is what keeps mirroring
    safe:

    * of the PARTICIPANT utterance — it is almost entirely the agent's words, so
      there is no content of the participant's own in it;
    * of the AGENT line — it is almost the whole line, not a fragment of it.
      Playback echo is a replay of an utterance; a participant checking their
      understanding quotes a PART of it back and adds their own frame
      ("Wait, the decision is already made and there is no budget this cycle?"
      covers 92% of itself but only 63% of the line it quotes). Without this
      second test that turn is suppressed, which is the deletion this guard was
      rewritten to stop.

    Anything under eight words is left alone entirely, because at that length
    echo and paraphrase are indistinguishable.
    """
    if not user_norm or not agent_norm:
        return False
    uw, aw = user_norm.split(), agent_norm.split()
    if len(uw) < 8:
        return False
    covered = sum(
        b.size for b in difflib.SequenceMatcher(None, uw, aw).get_matching_blocks()
        if b.size >= 2
    )
    return covered / len(uw) >= 0.85 and covered / len(aw) >= 0.85


async def _await_transcript(buf: List[str], grace: float,
                            poll: float = 0.15,
                            stop=None, settled=None) -> None:
    """Wait for a turn's transcript to SETTLE, not merely to start arriving.

    The gateway can deliver transcript events after response.done, which is the
    whole reason a grace period exists. Both finalizers used to wait only for
    the buffer to become non-empty, so a transcript that streamed in after
    response.done ended the wait on its first chunk: the turn was recorded as
    "Great," with transcript_missing False, the remaining chunks were left in
    the buffer to be glued onto the front of the character's next turn, and the
    director then routed the room on the fragment. One reply became two turns,
    both of them wrong, and nothing in the record said so.

    So stop on quiescence instead. Quiescence has to be measured two ways that
    the first version of this got wrong:

    * As a DURATION of silence proportional to the grace, not a fixed count of
      polls. Two consecutive 0.15 s polls is ~0.30 s no matter what
      TRANSCRIPT_GRACE_SECONDS says, so a transcript streaming at half-second
      intervals — comfortably inside the 3 s budget the caller asked for — was
      declared settled after its first chunk. The turn was recorded as "Great,"
      with transcript_missing False and the rest of the line appended to a
      buffer the finalizer had already read and cleared. That is worse than the
      misplacement it replaced: the text did not move to the next turn, it
      disappeared.
    * On the CONTENT, not on len(buf). The gateway's authoritative whole-line
      `agent_transcript` is applied as `buf[:] = [text]`, which leaves the
      length unchanged, so a length-only watch cannot see the single most
      important event of the wait and would time its silence from the last
      delta instead — discarding the complete line the gateway supplied.

    An empty buffer never settles — there is nothing to be stable about — so a
    turn whose text truly never arrives still costs the full grace and is still
    marked transcript_missing.

    Quiescence is an INFERENCE that the stream has ended, and it costs what an
    inference costs: the window has to be long enough that a normally-paced
    stream is never cut in half, so a turn whose transcript was already complete
    was still held for ~1.1 s at the default grace. That was paid on every agent
    turn of every encounter, before _finalize_turn/_finalize_member_async send
    assistant_done — and static/v2.html keeps the "still speaking" cue up until
    that frame arrives, so the participant heard a character stop talking and
    then sat through a second of nothing. In a room the floor and the next
    speaker wait on the same signal, so it was paid per speaker per turn.

    `settled` (an asyncio.Event, or None) removes the inference where the
    gateway has already answered the question. It is set when the gateway's own
    end-of-transcript event has landed for THIS turn — the authoritative whole
    line, applied as `buf[:] = [text]` (see _pump / _pump_member) — which says
    the transcript stream is over. There is then nothing to be quiet about and
    the wait returns at once. A gateway that sends only deltas and never that
    event simply never sets it, and pays the full window as before: the fast
    path is opt-in evidence, never an assumption.

    Do NOT try to infer the same thing from the buffer being non-empty on entry.
    The whole line routinely arrives AFTER response.done — that is what the
    grace period is for — so a short settle window on a buffer that already
    holds the first delta returns "Great," and throws the rest of the line away,
    which is B25/R3 exactly.

    `stop` (an asyncio.Event, or None) ends the wait early. The caller sets it
    once a NEW reply has demonstrably begun: from that moment this turn's
    transcript is not coming, and every further second of waiting is a second in
    which the next reply's text can be taken for this one's.
    """
    deadline = time.time() + grace
    # Long enough that a normally-paced stream is never cut in half, short
    # enough that a settled turn is not held for the whole budget.
    quiet = min(grace / 3.0, 1.0)
    last_snapshot = None
    last_change = time.time()
    while True:
        if settled is not None and settled.is_set():
            # The gateway has said the transcript is complete. Waiting longer
            # cannot add anything of this turn's and can only capture the next
            # reply's.
            return
        if stop is not None and stop.is_set():
            return
        if time.time() >= deadline:
            return
        snapshot = tuple(buf)
        if snapshot != last_snapshot:
            last_snapshot = snapshot
            last_change = time.time()
        elif snapshot and time.time() - last_change >= quiet:
            return
        await asyncio.sleep(poll)
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
        # The buffer of a 1:1 turn that has had its response.done but whose
        # transcript may still be arriving. See _take_turn_buffer.
        self._settling_text: Optional[List[str]] = None
        # Index into _settling_text at which LATE deltas start — text that
        # arrived after that turn's response.done and is therefore only
        # provisionally its own. See _transcript_target and _pump's agent_audio.
        self._settling_late_from: Optional[int] = None
        # Set when a new reply demonstrably begins, to end the settling turn's
        # grace wait at that instant instead of letting it run on and consume
        # the new reply's transcript.
        self._settling_stop: Optional[asyncio.Event] = None
        # Set once the gateway's own end-of-transcript event has landed for the
        # settling turn: its transcript is complete and its grace wait has
        # nothing left to wait for. See _await_transcript.
        self._settling_settled: Optional[asyncio.Event] = None
        # The same fact for the turn that is still SPEAKING — the whole line
        # usually arrives before response.done, i.e. before there is a settling
        # turn or a gate to set. _take_turn_buffer carries it across.
        self._agent_line_settled = False
        self._turn_started_at: Optional[float] = None
        self._speaking = False
        # One participant-visible notice per reply for faults the session
        # survived (a discarded corrupt audio chunk). Every one of them is still
        # recorded; see _pump's error branch.
        self._transient_error_notified = False
        # Scene note for a session carried across an interaction boundary (see
        # _continuation_note). Held here rather than concatenated onto the one
        # re-brief at the boundary, because nothing speaks at a boundary: the
        # brief that actually reaches the actor is the NEXT one, and that one
        # rebuilds the instructions from scratch. Cleared once the actor has
        # produced a turn under it.
        self._scene_note = ""
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
        # (ended_at, agent_id, text), last 6. The timestamp is what bounds the
        # echo guard to the window in which playback echo is physically
        # possible; see _is_echo and ECHO_WINDOW_SECONDS.
        self._recent_agent_texts: List[tuple] = []
        self._last_group_speaker: Optional[str] = None
        self._turn_index = 0
        self._pending_direction: Optional[dict] = None
        # Every in-flight group-turn task. A fast second participant turn can
        # spawn a new _run_group_turn while a prior one still holds self._floor,
        # so an interaction switch must cancel ALL of them, not just the most
        # recent, or the floor-holder is orphaned and dead-airs the next segment.
        self._group_turn_tasks = set()
        # Planted triggers fire in order within the current interaction. They
        # are the measurement: each maps to ESCI items, and the participant's
        # response to it is what a rater scores.
        self._trigger_idx = 0
        self._fired: List[str] = []
        self._last_activity = time.time()
        self._turns_this_interaction = 0
        self._interaction_started_at = time.time()
        self._switching = False
        # True for the whole of _enter, i.e. while the sessions behind self.rt
        # are being torn down and rebuilt. _model_to_client must not pump
        # during that window: between _close_room and the replacement session's
        # connect(), self.rt points at a session whose websocket is already
        # gone, and rt.events() then raises RuntimeError("connect() first"),
        # which propagates out of run()'s wait and drops the participant
        # mid-encounter.
        self._transitioning = False
        # Re-entrancy guard for _advance_segment: three concurrent coroutines
        # can reach it at once, and without this a second advance that arrives
        # while one is in flight double-increments and skips a scored beat.
        self._advancing = False
        # Every finalize runs as a task — from the 1:1 pump, from a room
        # member's pump, and from either barge-in path. References are kept for
        # two reasons: so exceptions are retrieved (logged) instead of silently
        # swallowed, and so run()'s teardown can wait out a turn that is still
        # inside its transcript grace before the store is closed underneath it.
        self._finalize_tasks: List[asyncio.Task] = []
        # agent_id -> (transcript buffer, turn state) for every live room pump.
        # The 1:1 barge-in path reaches the in-flight turn through
        # self._agent_text and self._speaking; a room member's equivalents are
        # per-pump locals inside _pump_member, so they are published here for
        # the same reason. _client_to_model has to close the interrupted
        # member's turn, empty its buffer and release the floor from outside
        # the pump that owns them.
        self._member_turns: dict = {}
        self.room: Optional[GroupRoom] = None
        self._pumps: List[asyncio.Task] = []
        self._floor = asyncio.Lock()
        # Set when the room's scribe pump ends under a live encounter. The
        # scribe is the ONLY participant transcript channel in a room, so past
        # that point the runner must stop asserting what the participant said.
        self._scribe_lost = False
        # Serialises every session.update on the 1:1 wire. A brief is a
        # persistent session.update, not a per-response one, so two of them
        # racing does not merge — the later one simply replaces the earlier, and
        # the steering trail then records a stage direction that was withdrawn
        # before it could govern anything.
        self._brief_lock = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def _instructions(self, director_note: str = "") -> str:
        # Reuse the engine's prompt builder so the voice agent and the text
        # agent are the same character, persona knobs, branches, and the
        # director's intent all compose exactly as they do in text mode.
        engine = self.session.engines[self.agent_id]
        # Pass the CURRENT interaction's mode, not the scenario-level mode: a
        # group scenario (e.g. S3) continues as 1:1 series segments, and those
        # segments must not receive the multi-party MEETING framing.
        base = engine._system_prompt(
            self.session.triggered_branches, director_note or None,
            group=self.is_group(),
        )
        voice_rules = (
            "\n\nVOICE: You are in a live spoken conversation. Keep every turn "
            "SHORT: one or two spoken sentences, at most about 25 words, then "
            "stop and let others respond. Make one point per turn, never a "
            "list of points. Never monologue. Never read out JSON, markdown, "
            "or stage directions. The participant speaks English; reply in "
            "English."
        )
        if self.is_group():
            voice_rules += (
                "\nMEETING: Several people share this room. If the participant "
                "addresses someone else by name, stay silent and let them "
                "answer. Do not repeat or rephrase what another person just "
                "said, and do not answer every turn: leave room for quieter "
                "colleagues."
            )
        # The continuation note rides on EVERY brief while it stands, not just
        # the one issued at the interaction boundary. Attached only at the
        # boundary it never reached the actor: no one speaks there, and the
        # next thing to happen is the participant's turn, whose brief
        # (_brief_next_beat, _brief_member) and the closing re-brief from
        # _steer both rebuild the instructions from this method and so replaced
        # the note instead of composing with it. That is why S2's Morgan could
        # still open "the deflection ladder" with a fresh greeting. Appended
        # last so a DIRECTOR NOTE, which the briefs add after this, still has
        # the final word.
        return base + voice_rules + self._scene_note

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

    def _continues_scene(self, interaction: dict) -> bool:
        """True when `interaction` carries on the scene already in progress.

        An interaction that opens a NEW scene says so by carrying an
        ``opening:`` — S1A's hallway run-in with Sam, who was not present for
        the conversation with Riley and should not remember it. An interaction
        with no ``opening:`` is the same conversation moving to its next beat:
        S2's "making the case" then "the deflection ladder" with the same
        Morgan, S3/S4's working session then its close with the same cast. An
        explicit ``continues:`` overrides the inference either way, for a
        scenario that would rather be told than inferred from.
        """
        explicit = interaction.get("continues")
        if explicit is not None:
            return bool(explicit)
        return not str(interaction.get("opening") or "").strip()

    def _continuation_note(self) -> str:
        """Scene note for an actor whose live session is being carried over.

        Only the label goes in: `observe` is the rater's yardstick, not
        something the character may be told."""
        note = (
            "\n\nSCENE: This is the same conversation, still in progress. Do "
            "not greet again, do not restart, and do not recap: carry on from "
            "what has already been said."
        )
        label = str(self._interaction().get("label") or "").strip()
        if label:
            note += f" It has now reached: {label}."
        return note

    def _triggers(self) -> List[dict]:
        return self._interaction().get("triggers", []) or []

    def _trigger_agent(self, trigger: dict) -> Optional[str]:
        """The character a planted beat is written for, if it names one.

        Prefer an explicit 'agent' field (id or name); otherwise infer from a
        cue that opens by naming a character, e.g. "Casey: 'Am I supposed...'".
        Returns an agent id, or None when the beat is not bound to anyone.
        """
        explicit = trigger.get("agent")
        if explicit:
            for a in self._resolve_agents():
                if explicit in (a.id, a.name):
                    return a.id
            return explicit
        head = _norm_speech(trigger.get("cue") or "").split()
        if not head:
            return None
        for a in self._resolve_agents():
            name_tokens = _norm_speech(a.name).split()
            if name_tokens and head[:len(name_tokens)] == name_tokens:
                return a.id
        return None

    def _next_trigger(self) -> Optional[dict]:
        triggers = self._triggers()
        if self._trigger_idx < len(triggers):
            trig = triggers[self._trigger_idx]
            # In a series the triggers belong to specific members (Jordan's
            # beat, then Casey's). Hold a beat written for a later member until
            # the series reaches them, so Jordan does not perform Casey's line
            # in Casey's absence and spend her trigger before her segment.
            if self._interaction_mode() == "one_to_one_series":
                bound = self._trigger_agent(trig)
                if bound and bound != self.agent_id:
                    return None
            return trig
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
        # This is reachable from three concurrent coroutines (a tool_call in a
        # pump, the participant's advance command, and _maybe_advance). The
        # check-and-set below has no await between test and set, so it is an
        # atomic non-blocking mutex: a second advance that arrives while one is
        # in flight becomes a no-op (return True, i.e. "handled") rather than a
        # second increment that would skip a scored interaction and orphan a
        # freshly connected session.
        if self._advancing:
            return True
        self._advancing = True
        # The advance is bookkeeping about which interaction the record is in,
        # and _enter is what makes that true on the wire. If _enter cannot make
        # it true — the gateway refused the replacement session — the
        # bookkeeping has to go back, or every event written afterwards is
        # stamped with an interaction that never opened while the previous
        # character carries on speaking. _enter has already told the
        # participant and the log; here we simply undo the advance and stay put,
        # so the next turn tries again rather than the encounter continuing
        # under a false heading.
        before = (self.segment, self._series_idx, self._trigger_idx,
                  self._turns_this_interaction, self._interaction_started_at)
        try:
            agents = self._resolve_agents()

            # Still characters left in this series (e.g. Jordan then Casey).
            if self._interaction_mode() == "one_to_one_series" and self._series_idx + 1 < len(agents):
                self._series_idx += 1
                self._turns_this_interaction = 0
                self._interaction_started_at = time.time()
                if not await self._enter(agents[self._series_idx],
                                         new_interaction=False):
                    (self.segment, self._series_idx, self._trigger_idx,
                     self._turns_this_interaction,
                     self._interaction_started_at) = before
                return True

            if self.segment + 1 >= len(self.interactions):
                return False

            self.segment += 1
            self._series_idx = 0
            self._trigger_idx = 0
            self._turns_this_interaction = 0
            self._interaction_started_at = time.time()
            if not await self._enter(self._resolve_agents()[0],
                                     new_interaction=True):
                (self.segment, self._series_idx, self._trigger_idx,
                 self._turns_this_interaction,
                 self._interaction_started_at) = before
            return True
        finally:
            self._advancing = False

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
        """Group interaction: one session per character, all listening.

        The room is built into a local and published to self.room only once
        every session in it is connected. Publishing first and opening second
        was a silent way to lose a whole encounter: GroupRoom.open() tears down
        the sessions that did connect and re-raises, and its close() clears
        `sessions` and `scribe`, so a refused connect left self.room pointing at
        a live object with nobody in it. Every liveness test in the runner then
        said "a room exists" — _model_to_client parks on `self.room is not
        None`, hear() fans audio to an empty target list, each turn burns its
        full 45 s floor timeout — while the participant talked to nobody for the
        rest of the session and the encounter still looked complete afterwards.
        A failure now leaves self.room at None, which _enter can see and act on.
        """
        await self._close_room()
        agents = self._resolve_agents()
        room = GroupRoom(
            agents,
            instructions_for=lambda a: self._instructions_for(a),
            voice_for=lambda a: self._voice_for(a),
            tools=[END_SEGMENT_TOOL],
        )
        try:
            await room.open()
        except Exception as exc:  # noqa: BLE001, re-raised for _enter to contain
            # open() has already closed whatever did connect. Record the loss
            # here so a room that never opened is dated in events.jsonl in its
            # own right, rather than being a gap only the caller's error line
            # hints at.
            self.session.store.event(
                "voice_error", where="open_room",
                interaction=self._interaction_id(),
                agents=[a.id for a in agents], message=str(exc),
            )
            raise
        self.room = room
        # A fresh room brings a fresh scribe, so the participant channel is
        # whole again.
        self._scribe_lost = False
        self.session.store.event(
            "group_room_opened", agents=[a.id for a in agents]
        )
        # One pump per character, so a reply is attributed to whoever produced
        # it rather than to whoever happens to hold a shared session.
        for a in agents:
            rt = room.session_for(a.id)
            if rt is not None:
                self._spawn_pump(self._pump_member(a, rt))
        if room.scribe is not None:
            self._spawn_pump(self._pump_scribe(room.scribe))
        # Who speaks first is decided by _open_group_scene, which the caller
        # runs AFTER the segment_start banner has gone out, so the participant
        # is never hearing a character the UI has not introduced yet.

    async def _open_group_scene(self) -> None:
        """Have the lead character open a group scene.

        Context has to land in-scene (docs/scenario-spec-v3.md): a group
        interaction is authored as a meeting already under way, and the
        interaction's `opening:` says where that meeting is found — usually as a
        stage note, occasionally as a quoted line. Nobody used to speak at all
        here, so S3A's team meeting and S4A's working session
        began in total silence and stayed that way until the participant spoke
        — and a participant who freezes produced a silent WAV and an empty
        transcript, i.e. the missing data the study design exists to avoid.

        Opening IS possible on this bridge even though a response can only
        follow committed audio: give_floor pads a buffer holding less than
        300 ms with silence before committing, precisely so a session that has
        heard nothing can still be asked to speak (committing a genuinely
        empty buffer is what kills a session). The brief goes out immediately
        before the floor is granted and never mid-response, which is the same
        ordering _brief_member relies on.
        """
        room = self.room
        if room is None or self._closed:
            return
        agents = self._resolve_agents()
        if not agents:
            return
        lead = agents[0]
        opening = str(self._interaction().get("opening") or "").strip()
        rt = room.session_for(lead.id)
        if not opening or rt is None:
            # Nothing authored to open with: fall back to the old behaviour and
            # let the participant speak first. Recorded, because a scene that
            # opens in silence is a data risk a rater should be able to see.
            self.session.store.event(
                "group_scene_awaits_participant", agent_id=lead.id
            )
            return
        async with self._floor:
            if self.room is not room or self._closed:
                return
            # Three of the four group `opening:` values in the bank are
            # third-person stage notes about how the scene is found ("The
            # meeting is already convened...", "Opens mid-flow, Dan pitching.");
            # only S4B's quotes a line of dialogue. "Play this: <stage note>"
            # invites a speech-to-speech model to read the stage note out, and
            # that narrator voice-over would then BE the recorded first turn of
            # the encounter — a turn a rater has to score. So the direction
            # names which part is to be spoken and forbids describing the scene
            # aloud, rather than trusting one wording to cover both kinds.
            direction = (
                "You speak first and open the scene, in character, in one or "
                "two spoken sentences. Here is where the scene is found — "
                f"{opening} — if that quotes a line of dialogue, say that "
                "line; otherwise begin from that situation in your own words. "
                "Never read the description out, and never narrate or describe "
                "the scene or your own actions: speak only what your character "
                "says to the people in the room."
            )
            instructions = self._instructions_for(lead) + (
                f"\n\nDIRECTOR NOTE (follow precisely, never mention): {direction}"
            )
            await rt.update_instructions(instructions)
            # The opening line is a director instruction like any other, so it
            # belongs in the steering log; it fires no planted trigger, hence
            # the null trigger_id.
            self._pending_direction = {
                "turn": self._turn_index,
                "segment": self.segment,
                "interaction": self._interaction_id(),
                "agent_id": lead.id,
                "agent_name": lead.name,
                "voice": getattr(rt, "voice", None),
                "stage_direction": direction,
                "trigger_id": None,
                "esci": [],
                "probing": False,
                "opening": True,
                "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest()[:16],
                "director_model": (self.director.model if getattr(self, "director", None)
                                   else provenance()["text_model"]),
            }
            self.session.store.event("stage_direction", **self._pending_direction)
            self._response_done.clear()
            granted = await room.give_floor(lead.id)
            if granted is None:
                # give_floor drops a member whose commit failed but leaves
                # `speaking` pointing at it; clear it, or every other member's
                # has_floor test stays False and the room is mute for good.
                room.speaking = None
                self.session.store.event(
                    "group_scene_open_failed", agent_id=lead.id
                )
                # The opening direction was never spoken, so it must not be
                # left pending and paired with whichever turn finalises next.
                self._pending_direction = None
                return
            self.session.store.event("group_scene_opened", agent_id=lead.id)
            # Keep the floor until the opener is done, so the watchdog does not
            # read the opening pause as participant silence and probe over it.
            self._last_activity = time.time()
            try:
                await asyncio.wait_for(self._response_done.wait(), timeout=45)
            except asyncio.TimeoutError:
                self.session.store.event(
                    "group_turn_timeout", agent_id=lead.id, opening=True
                )
                rt.clear_response_state()
            if self.room is room:
                room.speaking = None
            self._last_activity = time.time()

    def _spawn_pump(self, coro) -> None:
        """Start a relay pump, with its exceptions logged rather than lost.

        self._pumps is only ever cancelled, never inspected, so a pump that
        raised used to die into an untracked task and take its character's
        transcript with it, silently. A pump ending is a fact about the record —
        it is where a channel stops — so it is written either way.
        """
        task = asyncio.ensure_future(coro)
        self._pumps.append(task)
        task.add_done_callback(self._on_pump_done)

    def _on_pump_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.session.store.event(
                "voice_error", where="pump", message=str(exc)
            )

    def _respawn_member_pumps(self) -> None:
        """Give every live member of a KEPT room a live relay again.

        _member_turns is the pump's own liveness registry: it publishes itself
        there on entry and removes itself in its finally, so an agent missing
        from it has no pump. A member can lose its pump and keep its session —
        that is what ending a conversation with the end_conversation tool used
        to do — and the room is then carried into the next interaction with a
        character who cannot be heard, while give_floor still reports success
        and their planted beat is still recorded as fired.
        """
        room = self.room
        if room is None:
            return
        for a in room.agents:
            rt = room.session_for(a.id)
            if rt is None or rt.ws is None:
                continue
            if a.id in self._member_turns:
                continue
            self.session.store.event("member_pump_respawned", agent_id=a.id)
            self._spawn_pump(self._pump_member(a, rt))

    def _spawn_group_turn(self, coro) -> None:
        """Run a coroutine that takes the room's floor, tracked so it can be
        cancelled at an interaction change.

        Every in-flight group turn has to be tracked: a single reference would
        be overwritten by a fast second turn, orphaning whichever task holds
        self._floor for the full 45 s timeout. Exceptions are logged rather
        than left for asyncio to report at garbage-collection time.
        """
        task = asyncio.ensure_future(coro)
        self._group_turn_tasks.add(task)
        task.add_done_callback(self._on_group_turn_done)

    def _on_group_turn_done(self, task: asyncio.Task) -> None:
        self._group_turn_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.session.store.event(
                "voice_error", where="group_turn", message=str(exc)
            )

    def _cancel_group_turns(self) -> None:
        """Drop every in-flight group turn.

        A turn holds self._floor while it awaits _response_done; when the
        floor-holder's reply ends in end_conversation (the designed way a group
        interaction advances), that response_done is never processed, so
        without this cancel the task would keep the floor for the full 45s
        timeout and dead-air the first turn of the next interaction.
        Cancelling unwinds its `async with self._floor`, releasing the lock
        immediately.
        """
        for gt in list(getattr(self, "_group_turn_tasks", ()) or ()):
            if not gt.done():
                gt.cancel()
        if hasattr(self, "_group_turn_tasks"):
            self._group_turn_tasks.clear()

    async def _close_room(self) -> None:
        # In-flight group turns go first, for the reason _cancel_group_turns
        # documents: one of them may be holding the floor.
        self._cancel_group_turns()
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
        # Per-member state kept mutable so the finalize task (spawned below) and
        # this loop share it: the async-for keeps consuming late transcript
        # deltas while the task waits them out. `barged_in` is the same kind of
        # latch as `suppressing`: it marks a reply whose turn has already been
        # closed elsewhere, so its response.done must not close it a second
        # time. _client_to_model sets it when the participant interrupts.
        #
        # `finalized` is the same latch for the ordinary path, and it is what
        # _pump's `self._speaking` has always done for the 1:1 pump. A reply the
        # gateway abandons mid-sentence now produces TWO response_dones — the
        # synthetic `interrupted` one that closes the turn, then the real one
        # when the gateway gets round to it — and without a latch the second
        # spawned a second finalize on the same buffer. The two raced: the
        # second read the full buffer, wrote the turn and cleared it, and the
        # first woke on an empty buffer and wrote the same reply again as
        # text="" with transcript_missing True. That flag exists to tell a rater
        # "the audio played but its text was lost", so hanging it on a turn that
        # never happened teaches raters to distrust the one signal that protects
        # them — and _turn_index, _turns_this_interaction and steering_pair all
        # double-counted besides.
        #
        # `settled` is not a latch but this reply's end-of-transcript gate: set
        # when the gateway's authoritative whole line lands, so the finalize can
        # stop waiting instead of inferring completeness from a second of
        # silence. Replaced with a fresh Event at each announce, because the one
        # the previous finalize is holding must not be set by this reply's line.
        state = {"announced": False, "suppressing": False, "barged_in": False,
                 "finalized": False, "settled": asyncio.Event()}
        # Published for the duration of this pump so the barge-in path can
        # reach this member's open turn; dropped in the finally so a dead
        # pump's buffer is never finalized.
        self._member_turns[agent.id] = (buf, state)
        try:
            async for ev in rt.events():
                etype = ev["type"]

                # The bridge fires its own response after speech-plus-silence,
                # commit or not, on every session at once. Only the character
                # holding the floor may be heard; unsolicited responses are
                # cancelled and their events discarded, or the room becomes
                # three people talking over each other.
                has_floor = self.room is None or self.room.speaking == agent.id

                # If this agent gained the floor mid-response, stop suppressing:
                # the rest of the response is legitimately theirs to relay, and
                # the matching response_done must then finalize rather than be
                # swallowed. Keeping the latch would behead the reply and stall
                # the room for the full turn timeout.
                if state["suppressing"] and has_floor:
                    state["suppressing"] = False

                if etype in ("agent_audio", "agent_transcript_delta",
                             "agent_transcript") and not has_floor:
                    if not state["suppressing"]:
                        state["suppressing"] = True
                        self.session.store.event(
                            "unsolicited_response_suppressed", agent_id=agent.id
                        )
                        rt.pending_input = 0
                        try:
                            await rt.cancel_response()
                        except Exception:  # noqa: BLE001
                            pass
                    continue

                if etype == "agent_audio":
                    if not state["announced"]:
                        state["announced"] = True
                        # A new reply has started, so any latch left over from
                        # the previous one is spent: neither may outlive its own
                        # response and swallow this turn's response.done.
                        state["barged_in"] = False
                        state["finalized"] = False
                        state["settled"] = asyncio.Event()
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
                    if not state["announced"] and not buf:
                        # An EMPTY buffer is what makes this delta the start of
                        # a NEW reply rather than the closing one's transcript
                        # arriving late — the same test the agent_transcript
                        # branch below makes, and it belongs here even more,
                        # because a late DELTA is the ordinary case: transcript
                        # deltas arriving after response.done are the whole
                        # reason a grace period exists.
                        #
                        # Announcing on late text cleared `finalized` (and
                        # `barged_in`), so the real response.done behind the
                        # bridge's synthetic one spawned a SECOND finalize on
                        # the buffer the first was still grace-waiting on. The
                        # two raced, and the reply was written as two
                        # assistant_turns, the second empty and flagged
                        # transcript_missing — a flag that tells a rater "the
                        # audio played but its text was lost", hung on a turn
                        # that never happened. Reproduced from: audio, delta,
                        # response_done(interrupted), delta, response_done.
                        #
                        # It also stranded `announced` True and left this
                        # reply's `settled` gate in place, so the NEXT reply
                        # was never announced to the page and its finalize
                        # found an already-set gate and returned without
                        # waiting, recording that turn as its first fragment.
                        # P5.
                        state["announced"] = True
                        state["barged_in"] = False   # see agent_audio above
                        state["finalized"] = False   # see agent_audio above
                        state["settled"] = asyncio.Event()   # see agent_audio above
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

                elif etype == "agent_transcript":
                    # The gateway's own end-of-transcript event, carrying the
                    # WHOLE line rather than a delta. It was being dropped on
                    # the floor while the finalizer guessed at completeness from
                    # a stream of fragments; take it as authoritative and
                    # replace the buffer, so a turn whose deltas arrived
                    # piecemeal after response.done is recorded in full instead
                    # of as its first fragment.
                    if not state["announced"] and not buf:
                        # Announce here too, exactly as the two branches above
                        # do. A reply delivered ONLY as a whole-line transcript
                        # (no deltas, no audio) was otherwise never announced:
                        # the client drops assistant text that arrives before
                        # assistant_started, so the character appeared to say
                        # nothing, and _finalize_member_async skips its grace
                        # wait entirely when announced is False — leaving the
                        # record and the screen disagreeing about whether this
                        # character spoke.
                        #
                        # An EMPTY buffer is what makes this a new reply rather
                        # than the closing one's transcript arriving late. The
                        # R6 case this branch exists for — a reply delivered
                        # only as a whole line, no deltas and no audio — has
                        # nothing in the buffer; a finalize that is still
                        # grace-waiting has its turn's text sitting in it.
                        # Clearing the finalize latch on late text let the real
                        # response.done behind it spawn a SECOND finalize on
                        # that same buffer, and the reply was written twice, the
                        # second copy empty and falsely flagged
                        # transcript_missing. That is P5 by another route, and
                        # reproducible from: audio, delta,
                        # response_done(interrupted), agent_transcript,
                        # response_done. Announcing on late text ALSO stranded
                        # `announced` True and this reply's `settled` gate in
                        # place, so the next reply went unannounced and its
                        # finalize returned on the stale gate without waiting
                        # for its own transcript.
                        #
                        # Late text keeps the OUTSTANDING finalize's gate on
                        # purpose, so setting it below hands that finalize the
                        # whole line at once instead of leaving it to time out
                        # on silence.
                        state["announced"] = True
                        state["barged_in"] = False   # see agent_audio above
                        state["finalized"] = False   # see agent_audio above
                        state["settled"] = asyncio.Event()   # see agent_audio above
                        await self._send({
                            "type": "assistant_started",
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                        })
                    buf[:] = [ev["text"]]
                    # The gateway has declared this reply's transcript complete,
                    # so a finalize already grace-waiting on it can stop now
                    # instead of inferring the same thing from a second of
                    # silence. In a room that second was charged per speaker per
                    # turn: the floor and the next speaker wait on the same
                    # signal. See _await_transcript.
                    state["settled"].set()

                elif etype == "user_transcript":
                    # Member sessions hear the other characters too, so their
                    # input transcription mixes agent speech into the "user"
                    # channel. The scribe pump owns the participant transcript.
                    continue

                elif etype == "response_done":
                    if state["suppressing"]:
                        state["suppressing"] = False
                        buf.clear()
                        continue
                    if state["barged_in"]:
                        # The participant talked over this reply and
                        # _client_to_model already closed the turn, flagged,
                        # with whatever text had arrived. If the gateway does
                        # emit response.done for the cancelled response after
                        # all, writing the turn again would put the same words
                        # in the record twice. buf is deliberately NOT cleared
                        # here: the finalize task spawned at the barge-in owns
                        # it and is still collecting the late deltas.
                        state["barged_in"] = False
                        # Still release the floor if this member holds it, so
                        # the swallow can never be the reason a turn waits out
                        # its 45 s timeout.
                        if self.room is None or self.room.speaking == agent.id:
                            self._response_done.set()
                        continue
                    if state["finalized"]:
                        # A SECOND response_done for a reply whose turn is
                        # already being written. The gateway abandoning a reply
                        # mid-sentence (an `error` frame, a stall, the socket
                        # going away) makes voice/realtime.py close the turn out
                        # with a synthetic `interrupted` response_done, and the
                        # real response.done for the same reply can still arrive
                        # behind it — the bridge's own `_done_ids` cannot dedupe
                        # the pair because the synthetic one carries no response
                        # id. Spawning a second finalize on the shared buffer
                        # recorded one reply as two assistant_turns, the second
                        # empty and flagged transcript_missing. Swallow it, and
                        # do not clear buf: the finalize spawned below still
                        # owns it and is collecting the late deltas.
                        state["finalized"] = False
                        # Release the floor anyway, for the same reason the
                        # barge-in swallow above does.
                        if self.room is None or self.room.speaking == agent.id:
                            self._response_done.set()
                        continue
                    # Finalise OFF the pump: the gateway can deliver transcript
                    # deltas after response.done, and sleeping here would stop
                    # the async-for that fills buf. The task shares buf/state and
                    # observes those late deltas while the loop keeps consuming.
                    announced_now = state["announced"]
                    state["announced"] = False
                    state["finalized"] = True
                    # Tracked like the 1:1 finalizes, so run()'s teardown can
                    # wait for a turn that is still settling instead of closing
                    # the store out from under it and dropping the last line of
                    # the encounter.
                    self._spawn_finalize(
                        self._finalize_member_async(
                            agent, buf, announced_now,
                            # R15: the bridge sets this on the response_done it
                            # synthesises for a reply the gateway abandoned
                            # mid-sentence. Dropping it here wrote a truncated
                            # delivery into the record as a complete one, and
                            # left the grace wait spending its full budget on a
                            # transcript that a dead session will never send.
                            interrupted=bool(ev.get("interrupted")),
                            settled=state["settled"],
                        )
                    )

                elif etype == "tool_call":
                    # END_SEGMENT_TOOL is on every room session; an actor ending
                    # a group conversation must advance the encounter, not be
                    # dropped. Advance off-pump so _close_room cancelling this
                    # very pump cannot interrupt the advance mid-flight.
                    self.session.store.event(
                        "tool_call", name=ev.get("name"), segment=self.segment,
                        agent_id=agent.id,
                    )
                    asyncio.ensure_future(self._advance_from_tool())
                    # Keep relaying. Ending the pump here killed this character
                    # for the rest of a KEPT room: S4's working session and its
                    # close share a cast, so _enter takes the keep_room branch,
                    # re-briefs the same sessions and spawns no new pumps — and
                    # whoever called end_conversation was then mute for the whole
                    # next interaction while give_floor still reported success,
                    # so their planted beat was recorded as fired with nobody to
                    # speak it. The advance is already off-pump, which is all the
                    # comment above actually requires, and _close_room cancels
                    # this task explicitly when the room really is torn down.
                    continue

                elif etype == "error":
                    self.session.store.event(
                        "voice_error", where=f"room:{agent.id}", message=ev["message"]
                    )
        except asyncio.CancelledError:
            return
        finally:
            # Only if this pump is still the registered one: a room rebuilt for
            # the next interaction starts a fresh pump for the same agent id,
            # and a late teardown of the old one must not unregister the new
            # one's buffer and leave barge-in with nothing to close.
            if self._member_turns.get(agent.id) is not None and \
                    self._member_turns[agent.id][0] is buf:
                del self._member_turns[agent.id]

    async def _finalize_member_async(self, agent, buf: List[str], announced: bool,
                                     *, interrupted: bool = False,
                                     settled=None) -> None:
        """Grace-wait for a member's transcript, then close the turn.

        Runs as its own task so the pump's async-for keeps advancing and can
        deliver the late transcript deltas this loop is waiting for.

        `interrupted` mirrors _finalize_turn's and has the same two sources: the
        participant talking over this reply (_client_to_model cancels it), and
        the gateway abandoning it mid-sentence, which arrives on the bridge's
        synthetic response_done. Neither kind of reply will emit the transcript
        events a completed one does (see RealtimeVoiceSession.cancel_response),
        so the full grace would be spent waiting for text that is not coming —
        and every second of it is a second the room's floor stays held. Take
        what has arrived, briefly, and record the turn as truncated.

        `settled` is this reply's end-of-transcript gate, held in the pump's
        per-member state: once the gateway's whole-line event has landed there
        is nothing left to wait for, and holding the floor to infer that from a
        second of silence delayed the room's next speaker on every single turn.

        The budget comes from TRANSCRIPT_GRACE_SECONDS, the same place
        _finalize_turn reads it. It used to be a hardcoded 2.5 s, which quietly
        broke the teardown wait in run(): that wait is bounded by
        TRANSCRIPT_GRACE_SECONDS + 1 s, so any deployment lowering the grace
        below 1.5 s bounded the wait BELOW the thing it was waiting for and
        dropped a room's last turn again — the exact failure the wait exists to
        prevent, back under a non-default setting and with the comment there
        still claiming otherwise.
        """
        if announced:
            grace = float(os.getenv("TRANSCRIPT_GRACE_SECONDS", "3"))
            await _await_transcript(
                buf, min(grace, 1.0) if interrupted else grace, settled=settled,
            )
        text = "".join(buf).strip()
        buf.clear()
        if not announced and not text:
            # Nothing at all came back: release the floor quietly rather than
            # writing a blank turn — but ONLY if this agent actually holds the
            # floor, or a non-floor member's empty auto-fired response would
            # release the routed speaker mid-utterance.
            self.session.store.event(
                "empty_response", agent_id=agent.id, segment=self.segment
            )
            # A direction written FOR this character produced no speech at all.
            # Left pending it would be attached to whichever member finalises
            # next — a follow-up speaker is given the floor without a re-brief —
            # and encounter_record keys the pairing on the ACTOR, dropping
            # direction.agent_id, so the rater packet would show one character's
            # line carrying another character's beat and ESCI items with nothing
            # on the page to reveal the swap. The beat itself stands as fired:
            # the brief was delivered, which is the bar Contract 8 sets; only
            # the pairing is void.
            pending = self._pending_direction or {}
            if pending.get("agent_id") == agent.id:
                self.session.store.event(
                    "stage_direction_unperformed",
                    trigger_id=pending.get("trigger_id"),
                    interaction=pending.get("interaction"),
                    segment=self.segment,
                    agent_id=agent.id,
                    reason="empty_response",
                )
                self._pending_direction = None
            if self.room is None or self.room.speaking == agent.id:
                self._response_done.set()
            return
        await self._finalize_member(agent, text, interrupted=interrupted)

    async def _advance_from_tool(self) -> None:
        """Advance the encounter from a room member's end_conversation call."""
        try:
            if not await self._advance_segment():
                await self._send({"type": "encounter_complete"})
        except Exception as exc:  # noqa: BLE001
            self.session.store.event(
                "voice_error", where="advance_from_tool", message=str(exc)
            )

    async def _pump_scribe(self, rt) -> None:
        """Relay the scribe's participant transcripts; swallow everything else.

        The scribe only ever hears the participant, so its input transcription
        is the clean user channel. The bridge auto-fires a response on any
        session after speech + silence, scribe included; those responses are
        cancelled unheard.

        Because it is the ONLY participant channel in a room — _pump_member
        deliberately discards its own user_transcript events — this pump ending
        is the end of the participant transcript for the rest of the encounter.
        It used to end without a word: an `error` event matched no branch, the
        generator finished, and the coroutine returned normally, so not even a
        done-callback would have seen anything. The encounter carried on
        recording perfect participant audio with no transcript, and every
        subsequent turn's steering_pair repeated the last utterance the scribe
        managed to hear as though the participant had just said it again. So the
        error is written, the end of the channel is dated, and _scribe_lost stops
        the record from asserting words nobody spoke.
        """
        cancelled = False
        try:
            async for ev in rt.events():
                etype = ev.get("type")
                if etype == "user_transcript":
                    await self._record_user_turn(ev["text"])
                elif etype in ("agent_audio", "agent_transcript_delta",
                               "agent_transcript"):
                    try:
                        await rt.cancel_response()
                    except Exception:  # noqa: BLE001
                        pass
                elif etype == "response_done":
                    rt.clear_response_state()
                elif etype == "error":
                    self.session.store.event(
                        "voice_error", where="scribe", message=ev["message"]
                    )
        except asyncio.CancelledError:
            cancelled = True
            return
        finally:
            # A cancel is _close_room tearing the room down deliberately, which
            # is not a lost channel; anything else is the socket going away
            # under a live encounter, which is.
            if not cancelled and not self._closed:
                self._scribe_lost = True
                self.session.store.event(
                    "scribe_pump_ended", segment=self.segment,
                    interaction=self._interaction_id(),
                )
                await self._send({
                    "type": "error",
                    "message": "The transcription channel was lost.",
                })

    async def _finalize_member(self, agent, text: str,
                               *, interrupted: bool = False) -> None:
        """Close one character's turn in a group room."""
        text = _clean_agent_text(text)
        await self._finalize_member_inner(agent, text, interrupted=interrupted)

    async def _finalize_member_inner(self, agent, text: str,
                                     *, interrupted: bool = False) -> None:
        """Close one character's turn in a group room.

        This was lost in a refactor once, and the symptom was total: every pump
        died with AttributeError at its first response.done, silently, so no
        reply ever reached the participant and every routed turn timed out.

        `interrupted` marks a turn the participant talked over, exactly as
        _finalize_turn marks it in a 1:1 encounter.
        """
        # Read the slot ONCE. It used to be read here for the `opening` test and
        # again below for the pairing, with an await between, so a brief that
        # landed in between paired this line with somebody else's direction.
        direction = self._pending_direction
        # A direction belongs to the character it was written for. The follow-up
        # speakers in _run_group_turn take the floor without a re-brief, so a
        # slot left standing by the owner's empty or lost reply would otherwise
        # be paired with the next member to finish — and encounter_record keys
        # the pairing on the actor and drops direction.agent_id, so the rater
        # packet would show Priya's line carrying Dan's beat and Dan's ESCI
        # items with nothing on the page to reveal it. An unowned beat (no
        # agent_id) belongs to whoever speaks, as it always did.
        if direction and direction.get("agent_id") not in (None, agent.id):
            self.session.store.event(
                "steering_pair_unmatched",
                trigger_id=direction.get("trigger_id"),
                direction_agent_id=direction.get("agent_id"),
                actor_agent_id=agent.id,
                segment=self.segment,
            )
            direction = None
        self._turn_index += 1
        # _maybe_advance's pacing gate counts conversational exchanges, and it
        # was calibrated on turns the participant prompted. The room now opens
        # itself (see _open_group_scene), and that opening turn is prompted by
        # nobody, so counting it brought every group interaction's automatic
        # advance one exchange early. A probe reply is deliberately still
        # counted: when a participant has gone quiet the probes are the only
        # thing that moves the gate at all, and excluding them would wedge that
        # encounter in its first interaction for good.
        if not (direction or {}).get("opening"):
            self._turns_this_interaction += 1
        # The actor has now spoken under the continuation note, which says "do
        # not greet again" — a one-off instruction, not a standing one.
        self._scene_note = ""

        if text:
            self.session.append_agent(agent.id, text)
            self._recent_agent_texts = (
                self._recent_agent_texts + [(time.time(), agent.id, text)]
            )[-6:]
            await self.session.broadcast({
                "type": "transcript", "role": "assistant",
                "agent_id": agent.id, "text": text,
            })
        self._last_group_speaker = agent.id
        if not text:
            self.session.store.event(
                "transcript_missing", agent_id=agent.id, segment=self.segment
            )
        # Once the scribe is gone there is no participant channel, and repeating
        # the last utterance it managed to hear would make the record assert
        # that the participant said a specific sentence immediately before turns
        # they said nothing before. Say the channel was lost instead.
        self.session.store.event(
            "steering_pair",
            direction=direction,
            actor={"agent_id": agent.id, "text": text,
                   "voice": getattr(agent, "voice_id", None),
                   "transcript_missing": not text,
                   # Recording the turn is only half of it: a barge-in turn is
                   # a fragment of what the actor was briefed to say, and a
                   # rater comparing the direction to the line has to be able
                   # to tell a truncated delivery from a bad one.
                   "interrupted": interrupted},
            participant=None if self._scribe_lost else self._last_user_text,
            participant_channel="lost" if self._scribe_lost else "ok",
        )
        # Only the slot this turn actually consumed is cleared: a direction
        # belonging to a character who has not spoken yet keeps waiting for them.
        if direction is not None and direction is self._pending_direction:
            self._pending_direction = None
        self.session.store.event(
            "assistant_turn", agent_id=agent.id, text=text,
            segment=self.segment, transcript_missing=not text,
            interrupted=interrupted,
        )
        await self._send({"type": "assistant_done", "agent_id": agent.id})
        # Measure the idle window from the end of this reply, not the
        # participant's last utterance, so the watchdog does not probe the
        # instant the agent stops talking.
        self._last_activity = time.time()
        # Only the floor holder finishing releases the turn: a non-floor
        # member completing must not wake _run_group_turn and hand the floor
        # onward while the routed speaker is still talking.
        if self.room is None or self.room.speaking == agent.id:
            self._response_done.set()

    def agent_order(self) -> List[str]:
        return [a.id for a in self._resolve_agents()]

    async def _record_user_turn(self, text: str) -> None:
        if not text:
            return
        now = time.time()
        norm = _norm_speech(text)
        # The bridge can deliver the same utterance twice (append + commit)
        # within moments; record it once. Keep the window tight (the
        # double-delivery timescale) so a participant who genuinely repeats
        # themselves seconds later is not silently dropped.
        if norm and norm == self._last_user_norm and now - self._last_user_at < 2:
            return
        # Echo guard: an agent's line played over speakers can come back
        # transcribed as participant speech (Chrome's AEC does not cancel
        # WebAudio playback). Real playback echo arrives inside one buffer, not
        # five turns later, so only lines that finished moments ago are
        # candidates; the unbounded last-six list let a character's line from a
        # minute earlier delete a participant turn that happened to resemble it.
        echo_of = None
        for at, aid, atext in self._recent_agent_texts:
            if now - at > ECHO_WINDOW_SECONDS:
                continue
            if _is_echo(norm, _norm_speech(atext)):
                echo_of = aid
                break
        if echo_of is not None:
            # Suspected, and recorded as such — but NOT as a `user_turn`.
            #
            # A previous revision wrote both events, reasoning that a dropped
            # turn and a turn that never happened are indistinguishable
            # afterwards. The audit trail half of that is right and is kept:
            # `echo_dropped` carries the verbatim text and the character it
            # matched, events.jsonl is append-only, and the participant's own
            # microphone channel is in user_audio.wav, so a retranscribe pass or
            # a human can still adjudicate every one of these.
            #
            # The `user_turn` half was not right. `echo_suspected`/`echo_of` do
            # not survive contact with anything downstream: encounter_record
            # copies only t/role/text into the transcript, so by the time a
            # record exists the flag is gone and the line reads as
            # `role=participant`. rater_packet then shows the AI character's own
            # sentence to a human rater as something the participant said,
            # scoring.load_transcript gives it a U-index for the LLM judge, and
            # app._count_user_turns counts it towards the encounter being
            # non-empty. A turn withheld here is a gap that names itself; an
            # echo written as participant speech is a fabricated turn that
            # nothing downstream can detect. Between a recorded gap and a silent
            # fabrication this study takes the gap.
            #
            # See needs_elsewhere: if these are ever to reach a rater, the flag
            # has to be carried through encounter_record and rendered as a note.
            self.session.store.event(
                "echo_dropped", matches=echo_of, text=text,
                channel="voice", script_mismatch=_script_mismatch(text),
            )
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

    async def _switch_character(self, agent):
        """Start a fresh realtime session as `agent`, or None if the gateway refused.

        Re-briefing the existing session does not work: the conversation history
        keeps the model anchored to whoever it has been playing, and it will
        answer as that character no matter what the new instructions say, in
        testing, "Sam" opened with "I'm Riley, Sam's not here."

        A new session is also the right model of the scenario. The hallway
        run-in with Sam is a different scene; Sam was not present for the
        conversation with Riley and should not remember it.

        That reasoning covers a change of character or a change of scene ONLY.
        When the same character carries on the same conversation into the next
        interaction (S2's Morgan across "making the case" and "the deflection
        ladder") _enter keeps the live session instead of calling this, because
        the session is the only place the first half of that conversation
        exists — see _continues_scene.

        Returns the connected session, or None when the connect was refused. The
        caller MUST NOT become `agent` on None: the old session is still live and
        still playing the old character, and a runner that had already adopted
        the new identity would record that character's voice into the new
        character's WAV, write their lines as assistant_turn under the new id,
        and pair them with the new character's stage directions — a corrupted
        encounter that looks complete, not a lost one. The instructions and voice
        are built through the _for helpers so nothing has to be mutated on the
        runner before we know the connect succeeded.
        """
        old = self.rt
        self._switching = True
        new_rt = RealtimeVoiceSession(
            instructions=self._instructions_for(agent),
            voice=self._voice_for(agent),
            tools=[END_SEGMENT_TOOL],
        )
        try:
            await new_rt.connect()
        except Exception as exc:  # noqa: BLE001
            # Connect failed: do NOT adopt a dead session whose _send() silently
            # no-ops (ws is None) — that would wedge the encounter with the agent
            # never replying again. Keep the old session live instead.
            self._switching = False
            await new_rt.close()
            self.session.store.event(
                "voice_error", where="switch_character",
                agent_id=agent.id, message=str(exc),
            )
            return None
        self.session.store.event(
            "realtime_session_switched", agent_id=agent.id, agent_name=agent.name
        )
        if old is not None:
            await old.close()   # ends the old pump; the outer loop picks up the new session
        return new_rt

    async def _enter(self, agent, *, new_interaction: bool) -> bool:
        """Bring the encounter into `agent`'s part of the scenario.

        Returns True once the runner is actually playing `agent`, False when the
        replacement gateway session could not be opened — in which case nothing
        about who the runner is has changed, no segment_start has been emitted,
        and the caller rolls its own advance back.

        The identity is adopted at the END, with a live session already behind
        it. It used to be adopted first, at the top of this method, and never
        undone: a refused connect (a 429, a revoked key, a gateway blip at an
        interaction boundary — the mid-encounter failures that only become
        possible once real credentials exist) left the PREVIOUS character's
        session live and speaking while the runner, the browser and the record
        had all already become the new one. Every remaining turn was then
        written as assistant_turn under the new agent id, its audio appended to
        the new character's WAV, and its line paired with the new character's
        stage directions. A rater scoring "how did the participant handle Sam"
        would be reading Riley's words, with nothing in the transcript, the WAVs
        or the researcher view to say so. That is not a lost encounter but a
        corrupted one that looks complete, which is the one failure this
        instrument cannot absorb — so the rule is now that the runner never
        names a character it does not have on the wire.
        """
        # A new interaction is NOT automatically a new scene. The old rule
        # (`changed = agent.id != self.agent_id or new_interaction`) opened a
        # fresh, memoryless gateway session at every interaction boundary, so
        # S2's Morgan re-greeted the participant at the start of "the
        # deflection ladder" and restarted the ladder at rung 1 having never
        # heard the case they spent interaction 1 making — and no history can
        # be replayed into a fresh session on this bridge (a text conversation
        # item closes the socket, see docs/migration-plan.md).
        new_scene = new_interaction and not self._continues_scene(self._interaction())
        changed = agent.id != self.agent_id or new_scene
        prev_agent, prev_id = self.agent, self.agent_id
        prev_scene_note = self._scene_note
        # A new scene inherits nothing: clear any note left from an earlier
        # boundary before the two continuation branches below decide whether
        # this one deserves a fresh one.
        self._scene_note = ""
        opened_room = False
        failure: Optional[BaseException] = None
        # Hold _model_to_client off self.rt for the whole swap: mid-transition
        # it points at a session that has just been closed or is not connected
        # yet, and pumping either raises RuntimeError("connect() first") out of
        # run() and drops the participant.
        self._transitioning = True
        try:
            if self.is_group():
                wanted = [a.id for a in self._resolve_agents()]
                have = [a.id for a in (self.room.agents if self.room else [])]
                # Same cast carrying on the same scene (S4's working session
                # then its close): keep the room. Rebuilding it would replace
                # Dan, Priya and Chris with people who never attended the
                # session they are being asked to close, so "the pilot framing
                # was Chris's" would have no referent for anyone in the room.
                keep_room = (
                    self.room is not None
                    and not new_scene
                    and set(wanted) == set(have)
                    and all(self.room.session_for(a) is not None for a in wanted)
                )
                if keep_room:
                    # Re-briefing here is not the mid-stream session.update that
                    # mutes a room member: an interaction change happens between
                    # turns, and rebrief() cancels any reply still in flight and
                    # leaves the floor ungranted before the update goes out.
                    self._cancel_group_turns()
                    # Set on the runner, not concatenated here: this rebrief is
                    # not the brief anyone speaks under. _brief_member rebuilds
                    # a member's instructions from _instructions() before the
                    # first reply of the new interaction, and would drop a note
                    # that lived only in this lambda.
                    self._scene_note = self._continuation_note()
                    await self.room.rebrief(
                        instructions_for=lambda a: self._instructions_for(a)
                    )
                    # A kept room keeps its sessions, but not necessarily its
                    # relay pumps: a member whose own end_conversation ended its
                    # pump would be mute for the whole of the next interaction
                    # while give_floor still reported success. Re-spawn anyone
                    # whose relay is gone but whose socket is not.
                    self._respawn_member_pumps()
                    self.session.store.event(
                        "group_room_kept", agents=wanted,
                        interaction=self._interaction_id(),
                    )
                else:
                    old = self.rt
                    await self._open_room()
                    opened_room = True
                    # Close a previous NON-room (1:1) session before adopting a
                    # room member. Otherwise _model_to_client stays blocked
                    # forever in the old session's pump (its async-for never
                    # ends), and the orphaned gateway websocket leaks. Closing
                    # old ends that pump; set _switching first so
                    # _model_to_client resumes its loop into the room-sleep
                    # branch (where per-character _pump_member does the pumping)
                    # instead of exiting entirely.
                    if old is not None and (
                        self.room is None or old not in self.room.sessions.values()
                    ):
                        self._switching = True
                        await old.close()
                # Never fall back to `old` here: it may be the session just
                # closed above, and adopting a dead socket would silence the
                # character for the rest of the interaction. A room with no
                # session for this character is a real failure, so it aborts the
                # boundary rather than being announced and then sat through in
                # silence.
                new_rt = self.room.session_for(agent.id) if self.room else None
                if new_rt is None:
                    raise RuntimeError(f"no room session for {agent.id}")
            elif not (
                # Only a live 1:1 session for this same character in this same
                # scene can be carried over. A room is never carried into a 1:1
                # interaction (_close_room kills every member session), and a
                # session whose socket has already gone has to be rebuilt.
                not changed
                and self.room is None
                and self.rt is not None
                and self.rt.ws is not None
            ):
                await self._close_room()
                new_rt = await self._switch_character(agent)
                if new_rt is None:
                    raise RuntimeError(
                        f"gateway refused a session for {agent.id}"
                    )
            else:
                new_rt = self.rt
                new_rt.voice = self._voice_for(agent)
                if new_interaction:
                    # Keep the live session and re-brief it, so the actor still
                    # remembers interaction 1. Safe: an interaction only ends
                    # after the actor's turn is finished (end_conversation, the
                    # pacing gate in _maybe_advance, or the participant choosing
                    # to move on), and cancel_response makes that certain before
                    # the update goes out. This is the same between-turns
                    # update_instructions _steer already performs after every
                    # 1:1 turn. The prohibition on a MID-STREAM re-brief, which
                    # silently mutes this bridge, is unchanged.
                    await new_rt.cancel_response()
                    # Same reason as the group branch above: the note has to
                    # survive the re-brief that carries the participant's next
                    # turn, so it goes on the runner and _instructions() picks
                    # it up until the actor has spoken under it.
                    self._scene_note = self._continuation_note()
                    # Under the brief lock, like every other session.update on
                    # this wire, so a steer landing at the same moment cannot
                    # replace the scene note with an unnoted brief.
                    async with self._brief_lock:
                        await new_rt.update_instructions(
                            self._instructions_for(agent)
                        )
                    self.session.store.event(
                        "realtime_session_continued",
                        agent_id=agent.id, agent_name=agent.name,
                        interaction=self._interaction_id(),
                    )
            # Everything above worked, so the runner may now BE this character:
            # there is a connected session behind the name. Done inside the try,
            # before the finally clears _transitioning, so _model_to_client can
            # never observe the new identity against the old session.
            self.agent, self.agent_id = agent, agent.id
            self.rt = new_rt
            self.vad.reset()
            self._speaking = False
            self._agent_text = []
            # The previous character's settling window ends with the character:
            # nothing arriving on the NEW session can belong to a turn the old
            # one was still finishing. The finalize task keeps the list it was
            # handed, so its turn is still written; it just stops being a target.
            self._settling_text = None
            self._settling_late_from = None
            self._settling_stop = None
        except Exception as exc:  # noqa: BLE001, contained below
            failure = exc
        finally:
            self._transitioning = False
        if failure is not None:
            # Nothing above committed the new identity, so the previous
            # character still owns the wire, the record and the UI. Put the
            # scene note back and say plainly what happened: a voice_error
            # naming the interaction that failed to open, and a
            # segment_start_aborted so the record shows an announced boundary
            # that never happened rather than a boundary that silently did.
            self._scene_note = prev_scene_note
            self.agent, self.agent_id = prev_agent, prev_id
            self.session.store.event(
                "voice_error", where="enter",
                interaction=self._interaction_id(),
                wanted=agent.id, kept=prev_id, message=str(failure),
            )
            self.session.store.event(
                "segment_start_aborted",
                interaction=self._interaction_id(),
                segment=self.segment, wanted=agent.id, kept=prev_id,
            )
            await self._send({
                "type": "error",
                "message": "The connection to the next part of the "
                           "conversation was lost.",
            })
            if self.room is None and (self.rt is None or self.rt.ws is None):
                # A room was torn down to make way for one that never opened, so
                # there is nothing left that can speak. Ending deliberately puts
                # the failure on the participant's screen and unwinds run(),
                # instead of leaving them talking into a live socket with
                # nobody behind it for the rest of the session.
                self.rt = None
                self._closed = True
            return False
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
        if opened_room:
            # A freshly built room is a fresh scene, and somebody has to open
            # it. Spawned, not awaited: _open_group_scene holds the floor until
            # the opener finishes, and _enter's callers (a pump's tool_call, the
            # participant's advance command) must not block on that.
            self._spawn_group_turn(self._open_group_scene())
        return True

    async def run(self) -> None:
        try:
            # Open the gateway sessions INSIDE the try, so a partial group-open
            # failure (one of several connects raises) still reaches the finally
            # that closes whatever did connect, instead of leaking live sockets.
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
            if self.is_group():
                # Only after the banner: the participant must see who is in the
                # room before one of them starts talking. Spawned so the relay
                # tasks below start immediately.
                self._spawn_group_turn(self._open_group_scene())
            # Record what served this encounter, the audit trail has to say
            # which gateway and which models produced the data.
            self.session.store.event(
                "realtime_session_started", model=self.rt.model, **provenance()
            )
            # FIRST_COMPLETED + cancel, not gather: the watchdog loops on
            # _closed and _client_to_model's return paths do not set it, so a
            # plain gather blocks forever once the participant disconnects and
            # this finally would never run. As soon as any coroutine finishes
            # (encounter complete, or the client hung up), mark closed and
            # cancel the rest so cleanup actually happens.
            tasks = [
                asyncio.ensure_future(self._client_to_model()),
                asyncio.ensure_future(self._model_to_client()),
                asyncio.ensure_future(self._silence_watchdog()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            self._closed = True
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    raise t.exception()
        finally:
            self._closed = True
            # Let the turns that are still settling finish writing before the
            # store goes away. A finalize task sleeps out its transcript grace
            # BEFORE it writes steering_pair and assistant_turn, and app.py drops
            # the session the moment run() returns, closing events.jsonl; every
            # event landing after that is discarded in silence. The turn this
            # cost was always the LAST agent turn — the one a rater most needs —
            # and it went missing from the transcript while its audio stayed in
            # the WAV and its stage direction was left paired with nothing. The
            # wait is bounded by the same TRANSCRIPT_GRACE_SECONDS that both
            # finalizers spend, plus a second of slack, so a wedged task cannot
            # hold the socket open.
            #
            # The wait is also the first await in this finally, and a cancelled
            # run() re-delivers CancelledError at it — which skipped
            # _close_room() and rt.close() entirely and leaked every gateway
            # socket the encounter held. Cleanup runs either way now; the
            # cancellation is re-raised afterwards so the caller still sees it.
            cancelled_while_waiting = False
            if self._finalize_tasks:
                grace = float(os.getenv("TRANSCRIPT_GRACE_SECONDS", "3"))
                try:
                    await asyncio.wait(
                        list(self._finalize_tasks), timeout=grace + 1.0
                    )
                except asyncio.CancelledError:
                    cancelled_while_waiting = True
            await self._close_room()
            if self.rt:
                await self.rt.close()
            if cancelled_while_waiting:
                raise asyncio.CancelledError

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
                    if self.room is not None and self.room.speaking:
                        # A real meeting yields to an interjection: stop the
                        # current speaker's stream so the participant is not
                        # talked over.
                        speaking_id = self.room.speaking
                        speaker = self.room.session_for(speaking_id)
                        if speaker is not None:
                            try:
                                await speaker.cancel_response()
                            except Exception:  # noqa: BLE001
                                pass
                        # Cancelling is only the first half, and for a long
                        # time it was the only half here. A cancelled response
                        # may never produce a response.done, and the member
                        # pump finalises only on response.done, so the fragment
                        # this character had already spoken — audio the
                        # participant heard, and which is already in
                        # assistant_audio_<agent>.wav — was left out of the
                        # transcript, its stage direction paired with nothing,
                        # and its text still sitting in the pump's buffer to be
                        # glued onto the front of that character's next turn.
                        # Meanwhile nothing set _response_done, so the room's
                        # floor stayed held for the full 45 s and no one could
                        # answer the participant at all. Do what the 1:1 branch
                        # below does, through the same finalize path: close the
                        # turn with whatever text arrived, flag it interrupted,
                        # empty the buffer, and release the floor.
                        entry = self._member_turns.get(speaking_id)
                        agent = next(
                            (a for a in self._resolve_agents()
                             if a.id == speaking_id), None
                        )
                        if entry is not None and agent is not None:
                            buf, state = entry
                            announced_now = state["announced"]
                            # Cleared BEFORE spawning, and latched, for the
                            # same reason the 1:1 branch clears _speaking: a
                            # late response.done for the cancelled reply must
                            # find the turn already closed rather than write it
                            # a second time.
                            state["announced"] = False
                            state["barged_in"] = True
                            self._spawn_finalize(
                                self._finalize_member_async(
                                    agent, buf, announced_now, interrupted=True,
                                    settled=state.get("settled"),
                                )
                            )
                        else:
                            # No live pump for the floor holder (its session
                            # died, or the room was rebuilt underneath us).
                            # There is no turn to write, but the floor still
                            # has to come back or the room goes quiet.
                            self._response_done.set()
                        await self._send({"type": "assistant_interrupted"})
                    elif self._speaking:
                        # Barge-in: stop the agent's remaining audio, but RECORD
                        # the turn as far as it got. The words already spoken
                        # were heard by the participant and are what they are
                        # now talking over, so discarding them left a rater
                        # reading a participant reply to a line that is not in
                        # the transcript, and left this turn's stage direction
                        # paired with nothing. The agent's voice is in the
                        # assistant WAV either way, so a dropped transcript is a
                        # disagreement between the two halves of the record.
                        #
                        # This branch was unreachable in 1:1 until recently: the
                        # page muted capture on assistant_started and the
                        # worklet dropped the samples, so nothing reached the
                        # VAD while _speaking. Capture is continuous now (see
                        # static/pcm-worklet.js), so it fires on every real
                        # interruption — which is the overlap behaviour S1 and
                        # S2 exist to score.
                        await self.rt.cancel_response()
                        # Clear _speaking and hand the finalize THIS reply's
                        # buffer and direction, then detach both, exactly as the
                        # pump's own response_done path does: a late
                        # response.done for the cancelled reply then finds the
                        # turn already closed and cannot write it twice, and the
                        # next reply starts on a buffer of its own so its deltas
                        # cannot be read as the tail of this one.
                        self._speaking = False
                        buf, stop, settled = self._take_turn_buffer()
                        self._spawn_finalize(self._finalize_turn(
                            self.agent_id, self.agent, self.rt,
                            buf, self._take_direction(),
                            interrupted=True, stop=stop, settled=settled,
                        ))
                        await self._send({"type": "assistant_interrupted"})

                if self.room is not None:
                    await self.room.hear(pcm)
                else:
                    await self.rt.send_audio(pcm)

                if mark == "turn_ended":
                    self._turn_started_at = time.time()
                    if self.is_group() and self.room is not None:
                        # Tracked (see _spawn_group_turn) so an interaction
                        # switch can cancel whichever turn holds self._floor.
                        self._spawn_group_turn(self._run_group_turn())
                    else:
                        # Brief first, then decide whether to commit. The bridge
                        # auto-fires a reply after speech + silence, usually
                        # within a second of our own turn detection, and
                        # committing on top of that yields two replies, both
                        # spoken and both transcribed — so the commit is what
                        # the wait below guards, and only the commit.
                        #
                        # An earlier revision waited BEFORE briefing, so that a
                        # beat could not be recorded as fired when the reply was
                        # already in flight. That was wrong, and measurably so:
                        # over five turns of S1A interaction 2 with the bridge
                        # auto-firing, it fired 0 of 3 planted beats where the
                        # previous behaviour fired 3, and because _maybe_advance
                        # returns early while a beat remains it also disarmed
                        # the auto-advance for every 1:1 form. update_instructions
                        # is a persistent session.update, not a per-response
                        # one, so a brief issued during an auto-fired reply is
                        # not lost — it governs the NEXT reply. The honest
                        # record of that is a beat marked as applying late,
                        # which _brief_next_beat writes, rather than no beat at
                        # all.
                        await self._brief_next_beat(probing=False)
                        deadline = time.time() + float(os.getenv("AUTOFIRE_WAIT", "1.5"))
                        while time.time() < deadline and not self.rt.autofire_active:
                            await asyncio.sleep(0.05)
                        if self.rt.autofire_active:
                            self.session.store.event(
                                "autofire_adopted", agent_id=self.agent_id,
                                # The direction just issued reaches the actor one
                                # reply late. Recorded so a rater comparing a
                                # direction to the line it produced can see that
                                # this one governed the following turn.
                                direction_applies_next_turn=bool(self._pending_direction),
                            )
                        else:
                            await self.rt.commit_turn()
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001, surfaced in the session log
            self.session.store.event("voice_error", where="client_to_model", message=str(exc))

    # ── model -> participant ───────────────────────────────────────────────
    async def _model_to_client(self) -> None:
        """Relay model events, following the session across interaction changes.

        A change of character or scene gets a *new* realtime session (see
        _switch_character), so when one ends this loop picks up the next one. A
        same-character continuation keeps its session, and this loop simply
        never leaves _pump.
        """
        # When self.rt was last seen unusable, so a session that never comes
        # back ends this relay cleanly instead of spinning here forever.
        not_ready_since: Optional[float] = None
        while not self._closed:
            if self.room is not None or self._transitioning:
                # Group interactions are pumped per character by _pump_member.
                # A transition is likewise not ours: between _close_room (which
                # closes every member session, leaving ws None) and the
                # replacement session's connect(), self.rt points at a dead or
                # half-built session, and rt.events() raises
                # RuntimeError("connect() first") — which would propagate out of
                # run()'s asyncio.wait, trip the finally, and drop the
                # participant mid-encounter.
                not_ready_since = None
                await asyncio.sleep(0.5)
                continue
            rt = self.rt
            if rt is None:
                return
            if rt.ws is None:
                # Belt and braces for the same race, in case a swap happens
                # without _transitioning covering it: wait the session out
                # rather than pumping a socket that is not there.
                now = time.time()
                not_ready_since = not_ready_since or now
                if now - not_ready_since > 10.0:
                    self.session.store.event(
                        "voice_error", where="model_to_client",
                        message="realtime session never reconnected",
                    )
                    # Tell the participant before going. Returning here ends
                    # run()'s asyncio.wait cleanly, with no exception for
                    # app.py's handler to turn into an error frame and no
                    # encounter_complete either, so the page would simply stop
                    # answering someone who is still talking to it. The old
                    # behaviour at least raised RuntimeError("connect() first")
                    # and put something on screen.
                    await self._send({
                        "type": "error",
                        "message": "The connection to the conversation was lost.",
                    })
                    return
                await asyncio.sleep(0.1)
                continue
            not_ready_since = None
            await self._pump(rt)
            if not self._switching:
                return
            self._switching = False

    async def _pump(self, rt) -> None:
        async for ev in rt.events():
            etype = ev["type"]

            if etype == "agent_audio":
                # Audio is the one event that can only belong to a reply that is
                # speaking NOW, so it is what ends the previous turn's settling
                # window and takes back any text that was only provisionally
                # that turn's (see _transcript_target). Both halves matter: the
                # text moves to the turn it belongs to, and the settling turn
                # stops waiting, so it can no longer consume the rest of this
                # reply's transcript — and it is written FIRST, in the order the
                # two replies actually happened, which record.json sorts on.
                late = self._end_settling() if not self._speaking else []
                await self._begin_agent_turn()
                if late:
                    self._agent_text.extend(late)
                    # The participant's screen already showed this text under
                    # the previous speaker (it was relayed as it arrived), so
                    # say plainly that the record and the screen disagree here
                    # rather than leaving the move invisible.
                    self.session.store.event(
                        "transcript_reattributed", agent_id=self.agent_id,
                        segment=self.segment, text="".join(late),
                    )
                self.session.store.append_assistant_audio(ev["pcm"], agent_id=self.agent_id)
                await self._send_bytes(ev["pcm"])

            elif etype == "agent_transcript_delta":
                # Transcript deltas usually arrive before the first audio chunk.
                # The client buffers them into the turn opened by
                # assistant_started, so that has to be sent first or the text is
                # dropped and the agent appears to say nothing.
                #
                # That same ordering is why a delta arriving while the previous
                # turn is settling cannot be assumed to be the previous turn's:
                # it is just as likely to be the NEXT reply opening. See
                # _transcript_target — such a delta is parked provisionally and
                # the agent_audio branch above decides which turn it belongs to.
                target = await self._transcript_target()
                target.append(ev["text"])
                await self._send({
                    "type": "assistant_text_delta",
                    "text": ev["text"],
                    "agent_id": self.agent_id,
                })

            elif etype == "agent_transcript":
                # The gateway's own end-of-transcript event carries the WHOLE
                # line. It was being ignored while the finalizer inferred
                # completeness from a stream of deltas; take it as authoritative
                # and replace the buffer, so a turn whose text arrived piecemeal
                # after response.done is recorded whole rather than as its first
                # fragment.
                target = await self._transcript_target(whole_line=True)
                target[:] = [ev["text"]]
                # This event IS the end of the transcript stream, so say so on
                # whichever turn owns the buffer it just landed in. A finalize
                # already waiting on that turn can stop now instead of inferring
                # the same fact from a second of silence; see _await_transcript.
                if target is self._settling_text:
                    if self._settling_settled is not None:
                        self._settling_settled.set()
                else:
                    self._agent_line_settled = True

            elif etype == "user_transcript":
                # Gemini Live transcribes the participant for us, no separate
                # STT service. _record_user_turn forwards it to the client and
                # researcher views, and drops duplicates and playback echo.
                await self._record_user_turn(ev["text"])

            elif etype == "response_done":
                # A reply boundary, and the one that fires even when EVERY audio
                # frame of the reply was corrupt (in which case no turn was ever
                # begun and _begin_agent_turn never ran). Rearming here is what
                # makes the transient-error notice above "one per reply" rather
                # than one per encounter.
                self._transient_error_notified = False
                # Do not finalise here. The gateway can deliver transcript
                # events AFTER response.done, so reading the buffer now yields
                # an empty turn, audio with no text, which is unscoreable.
                # Snapshot the speaker's identity at spawn time: if the
                # participant advances during the grace wait, self.agent_id/rt
                # change and the closing turn would be recorded under the NEXT
                # character.
                if not self._speaking:
                    # The gateway repeats response.done for a reply that this
                    # runner has already closed (a barge-in finalises early).
                    # Without this the duplicate would wait out the whole grace
                    # on a fresh, empty buffer and log a phantom turn.
                    continue
                self._speaking = False
                # The buffer and the pending direction go WITH the turn, and
                # leave the runner. Sharing one buffer across replies is how a
                # grace wait came to consume the NEXT reply's transcript and
                # record it as this turn's line; sharing the direction slot is
                # how turn N's line came to be paired with turn N+1's stage
                # direction while turn N+1 was logged as unsteered. Both are
                # snapshots now, like agent_id/agent/rt beside them, and so is
                # the settling gate the buffer comes with.
                buf, stop, settled = self._take_turn_buffer()
                self._spawn_finalize(self._finalize_turn(
                    self.agent_id, self.agent, self.rt,
                    buf, self._take_direction(), stop=stop, settled=settled,
                    # R15: the bridge sets this on the response_done it
                    # synthesises for a reply the gateway abandoned mid-sentence
                    # (a stall, an `error` frame, or the socket going away). It
                    # was being dropped here, so a truncated delivery was
                    # written to the record as a complete one and a rater
                    # comparing the stage direction to the line had nothing to
                    # tell a cut-off line from a bad one. It also clamps the
                    # grace to 1 s, which is the whole budget worth spending on
                    # a transcript that a dead session will never send.
                    interrupted=bool(ev.get("interrupted")),
                ))

            elif etype == "tool_call":
                self.session.store.event(
                    "tool_call", name=ev.get("name"), segment=self.segment
                )
                if not await self._advance_segment():
                    await self._send({"type": "encounter_complete"})
                    return

            elif etype == "error":
                # Every error is recorded, always: the per-occurrence row is how
                # an analyst sees how much audio a bad stream cost.
                self.session.store.event("voice_error", where="model", message=ev["message"])
                if ev.get("transient"):
                    # A fault the session survived — a discarded audio chunk,
                    # not a lost turn (see voice/realtime.py). Audio deltas
                    # arrive every few tens of milliseconds, so relaying one of
                    # these per chunk filled the participant's transcript with
                    # dozens of identical "Something went wrong" lines
                    # mid-conversation. Tell them once per reply: enough to
                    # explain the glitch they just heard, not enough to bury the
                    # conversation or to hide a REAL error frame behind a wall
                    # of noise.
                    if self._transient_error_notified:
                        continue
                    self._transient_error_notified = True
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

    def _on_finalize_done(self, task: asyncio.Task) -> None:
        """Retrieve a finalize task's result so its exceptions are not lost.

        _finalize_turn's finally block chains _steer -> _maybe_advance ->
        _advance_segment -> _switch_character; a gateway hiccup in there would
        otherwise raise into an untracked task and vanish. Log it instead. The
        room's _finalize_member_async runs through here for the same reason,
        since it writes the transcript and releases the floor."""
        try:
            self._finalize_tasks.remove(task)
        except ValueError:
            pass
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.session.store.event(
                "voice_error", where="finalize_turn", message=str(exc)
            )

    async def _finalize_turn(self, agent_id: str, agent, rt,
                             buf: List[str], direction: Optional[dict],
                             *, interrupted: bool = False, stop=None,
                             settled=None) -> None:
        """Close out an agent turn once its transcript has settled.

        response.done can arrive before the transcript events that belong to the
        same reply. Finalising immediately produced turns with audio and no
        text, which are unscoreable and, because the old code skipped empty
        turns, vanished from the record entirely. So wait briefly for text, and
        if it truly never comes, still record the turn and mark it, so a gap is
        visible to verify_record instead of silently absent.

        agent_id/agent/rt, `buf` and `direction` are ALL snapshotted at spawn
        time. Reading self.* at completion attributed this turn to the NEXT
        character if the participant advanced during the grace wait, let the
        grace loop consume the next reply's transcript as though it were this
        one's, and paired this line with the next turn's stage direction while
        that next turn logged itself as unsteered. The caller detaches both the
        buffer and the direction slot from the runner before spawning, so this
        turn can only ever see its own.

        `interrupted` marks a reply that was cut off rather than finished, and
        it now has two sources. The participant talking over it is one:
        _client_to_model cancels the response and passes the flag. The gateway
        abandoning it mid-sentence — a stall, an `error` frame, a dead socket —
        is the other, and arrives on the synthetic response_done the bridge
        emits for exactly that (see server/voice/realtime.py); _pump carries it
        through. Either way the turn still happened, so it is recorded with
        whatever it managed to say and flagged, rather than left out of the
        transcript while its audio stays in the WAV. A rater comparing a stage
        direction to the line it produced has to be able to tell a truncated
        delivery from a bad one.

        `stop` is this turn's settling gate, handed over by _take_turn_buffer.
        _pump sets it the moment a new reply's audio arrives, which ends the
        grace early: waiting past that point cannot recover this turn's
        transcript and can only capture the next reply's.

        `settled` comes from the same place and is the opposite signal: the
        gateway's own end-of-transcript event has landed, so the text is
        complete and the grace has nothing left to wait out. Without it every
        turn paid ~1.1 s inferring from silence what the gateway had already
        said outright, and the participant sat through that second before
        assistant_done cleared the "still speaking" cue.
        """
        # Whether this reply is a turn at all is settled at the spawn site,
        # which latches _speaking down before handing the buffer over, exactly
        # as _pump_member latches state["announced"]. A guard here on shared
        # runner state cannot tell a duplicate response.done for a closed reply
        # from a genuinely new reply that started during a grace wait, and it
        # used to silently discard the second one.
        grace = float(os.getenv("TRANSCRIPT_GRACE_SECONDS", "3"))
        text = ""   # bound before the try, so the finally can always report
        try:
            if interrupted:
                # A cancelled response may never emit the transcript events a
                # completed one does (see RealtimeVoiceSession.cancel_response),
                # so the full grace would usually be spent waiting for text that
                # is not coming — and every second of it widens the window in
                # which this turn's closing re-brief can collide with the next
                # turn's. Take what has arrived, briefly.
                grace = min(grace, 1.0)
            await _await_transcript(buf, grace, stop=stop, settled=settled)

            text = _clean_agent_text("".join(buf))
            buf.clear()
            # See _instructions: the note is spent once the actor has spoken
            # under it, and the _steer() re-brief in this method's finally is
            # the first brief that should go out without it.
            self._scene_note = ""
            # Measure the idle window from the end of the agent's reply, not the
            # participant's last utterance, so the watchdog does not probe the
            # instant the agent stops talking.
            self._last_activity = time.time()
            missing = not text

            self._turn_index += 1
            self._turns_this_interaction += 1

            if text:
                self.session.append_agent(agent_id, text)
                self._recent_agent_texts = (
                    self._recent_agent_texts + [(time.time(), agent_id, text)]
                )[-6:]
                await self.session.broadcast({
                    "type": "transcript",
                    "role": "assistant",
                    "agent_id": agent_id,
                    "text": text,
                })
            else:
                self.session.store.event(
                    "transcript_missing", agent_id=agent_id, segment=self.segment
                )

            # A direction belongs to the character it was written for. In 1:1
            # that is normally the speaker, but a refused character switch or a
            # brief issued for the incoming character can leave a mismatch, and
            # encounter_record keys the pairing on the ACTOR and drops
            # direction.agent_id — so a mismatch would reach the rater packet as
            # this actor's line carrying somebody else's beat and ESCI items.
            if direction and direction.get("agent_id") not in (None, agent_id):
                self.session.store.event(
                    "steering_pair_unmatched",
                    trigger_id=direction.get("trigger_id"),
                    direction_agent_id=direction.get("agent_id"),
                    actor_agent_id=agent_id,
                    segment=self.segment,
                )
                direction = None
            self.session.store.event(
                "steering_pair",
                direction=direction,
                actor={
                    "agent_id": agent_id,
                    "text": text,
                    "voice": getattr(rt, "voice", None),
                    "transcript_missing": missing,
                    # Recording the turn is only half of it: a barge-in turn is
                    # a fragment of what the actor was briefed to say, and a
                    # rater comparing the direction to the line has to be able
                    # to tell a truncated delivery from a bad one.
                    "interrupted": interrupted,
                },
                participant=self._last_user_text,
            )

            latency = (
                round(time.time() - self._turn_started_at, 3)
                if self._turn_started_at else None
            )
            self.session.store.event(
                "assistant_turn", agent_id=agent_id, text=text,
                latency_s=latency, segment=self.segment, transcript_missing=missing,
                interrupted=interrupted,
            )
            await self._send({"type": "assistant_done", "agent_id": agent_id})
        finally:
            # This turn has stopped settling, so any transcript arriving from
            # here on belongs to whatever speaks next.
            if self._settling_text is buf:
                self._settling_text = None
                self._settling_late_from = None
                self._settling_stop = None
                self._settling_settled = None
            # Text that landed in the buffer between the read above and here is
            # in a list nobody will ever read again. Silently dropping it is how
            # the tail of a slow transcript disappeared with nothing in the
            # record marking the loss, so write it down: the turn is already
            # recorded, and this says what did not make it into it.
            if buf:
                self.session.store.event(
                    "transcript_late", agent_id=agent_id, segment=self.segment,
                    text="".join(buf), recorded_text=text,
                )
                buf.clear()
            # Released only now, so a group's next speaker cannot start while
            # this turn is still settling.
            self._response_done.set()
            # Not while the encounter is being torn down: run() waits these
            # tasks out so the turn is written, and steering or advancing on the
            # way out would re-brief and re-connect against a session that is
            # already closing.
            if not self._closed and not self.is_group():
                await self._steer()
                await self._maybe_advance()

    def _take_turn_buffer(self):
        """Detach the closing 1:1 turn's transcript buffer and its settling gates.

        Returns (buf, stop, settled). The finalize task keeps the list it is
        handed; the runner gets a fresh one for whatever speaks next. This is the same
        per-reply fencing _pump_member has always had with its per-pump `buf`,
        and its absence here is what let one shared list carry two replies'
        words into a single recorded turn.

        The detached buffer stays reachable as _settling_text, because the
        gateway can deliver a reply's transcript AFTER its response.done — that
        is the whole reason the grace period exists — and those late deltas
        belong to the turn that is settling, not to the next one. Audio is what
        marks a genuinely new reply; text alone, arriving while the previous
        turn is still settling, does not — and _pump's agent_audio branch is
        where that rule is now actually applied, by setting `stop` and taking
        the provisional late text back. Before it did, this docstring described
        an intent the code did not implement: EVERY transcript event reached the
        settling buffer while _speaking was False, so a new reply's opening
        sentence — deltas arrive before audio, as _pump says a few lines up —
        was recorded as the PREVIOUS turn's line.
        """
        buf, self._agent_text = self._agent_text, []
        self._settling_text = buf
        self._settling_late_from = None
        self._settling_stop = asyncio.Event()
        # Carried across from the speaking turn: the gateway's whole-line event
        # normally arrives BEFORE response.done, which is before this gate
        # exists, and a transcript already declared complete must not then be
        # waited out for a full quiet window (see _await_transcript).
        self._settling_settled = asyncio.Event()
        if self._agent_line_settled:
            self._settling_settled.set()
        self._agent_line_settled = False
        return buf, self._settling_stop, self._settling_settled

    def _take_direction(self) -> Optional[dict]:
        """Detach the pending stage direction for the turn now closing."""
        direction, self._pending_direction = self._pending_direction, None
        return direction

    def _spawn_finalize(self, coro) -> None:
        """Run a finalize off the pump, tracked so run() can wait it out and
        _on_finalize_done can retrieve its exceptions."""
        task = asyncio.ensure_future(coro)
        self._finalize_tasks.append(task)
        task.add_done_callback(self._on_finalize_done)

    async def _transcript_target(self, *, whole_line: bool = False) -> List[str]:
        """Which buffer this transcript event belongs to.

        A reply whose transcript is still arriving after its own response.done
        is not a new turn, and treating it as one split one reply into two
        recorded turns and announced the speaker twice. While a turn is settling
        its text keeps going to that turn; anything else opens (and announces) a
        new one.

        But "while a turn is settling" cannot be the whole rule, because the
        next reply's deltas arrive during exactly that window and are, in
        isolation, indistinguishable from this one's late ones. So text that
        lands here after response.done is only PROVISIONALLY the settling turn's:
        _settling_late_from remembers where it starts, and _pump's agent_audio
        branch — audio being the one event that can only belong to a live reply
        — hands it back to the new turn if a new reply turns out to be what
        produced it. Without that, reply B's opening sentence was recorded as
        reply A's line, reply B kept only its tail, and neither was flagged.

        The gateway's whole-line `agent_transcript` is not provisional: it is
        the end of a transcript stream, so it settles the buffer rather than
        adding to it (see _pump), and any provisional marker into the old
        contents goes with it.
        """
        if not self._speaking and self._settling_text is not None:
            if whole_line:
                self._settling_late_from = None
            elif self._settling_late_from is None:
                self._settling_late_from = len(self._settling_text)
            return self._settling_text
        await self._begin_agent_turn()
        return self._agent_text

    def _end_settling(self) -> List[str]:
        """Close the settling turn's window and give back its provisional text.

        Called when a new reply demonstrably starts. Returns the late deltas
        that were parked in the settling buffer on the assumption they were its
        own; they belong to the reply now starting instead. Synchronous on
        purpose — it runs between the arrival of the new reply's first audio
        chunk and anything that could await, so no third event can slip in
        between the decision and the move.
        """
        buf = self._settling_text
        if buf is None:
            return []
        late: List[str] = []
        if self._settling_late_from is not None:
            late = buf[self._settling_late_from:]
            del buf[self._settling_late_from:]
        if self._settling_stop is not None:
            # Ends _await_transcript now. The settling turn's transcript is not
            # coming; anything still to arrive on this stream is the new reply's.
            self._settling_stop.set()
        self._settling_text = None
        self._settling_late_from = None
        self._settling_stop = None
        self._settling_settled = None
        return late

    async def _begin_agent_turn(self) -> None:
        """Announce the speaker once per turn, on the first event of any kind."""
        if self._speaking:
            return
        self._speaking = True
        # A reply starts on a buffer of its own, so nothing a previous turn left
        # behind can be read as part of this one — and on no claim that its
        # transcript is complete, or the first grace wait of this turn would
        # return on the PREVIOUS turn's end-of-transcript event.
        self._agent_text = []
        self._agent_line_settled = False
        await self._send({
            "type": "assistant_started",
            "agent_id": self.agent_id,
            "agent_name": self.agent.name,
        })

    async def _brief_next_beat(self, *, probing: bool) -> None:
        """Re-brief the actor with the next planted trigger, and record it.

        The beat is spent only once the brief has actually left. It used to be
        spent first — _fire_trigger writes the append-only trigger_fired line,
        appends to _fired and advances _trigger_idx — and only then transmitted,
        so a session.update that raised on a dropped socket left a claim
        standing that the participant had faced a scored beat they never faced.
        verify_record counts trigger_fired (net of retractions) as coverage, and
        that count is what decides whether an encounter reached its scored
        moments, so an inflated one certifies an encounter as scoreable on a
        beat nobody delivered. Firing after the send needs no retraction: there
        is no claim to withdraw.

        Reading the beat, sending it and spending it is ONE unit of work, held
        under _brief_lock — which used to cover only the session.update. This
        method has two callers in different tasks, _client_to_model when the
        participant's turn ends and _silence_watchdog when it does not, and
        nothing serialised the read against the fire. With the brief on the wire
        and the fire after it, both callers could hold the same trigger and both
        fire it: two trigger_fired rows for one beat, at indices N and N+1, the
        id twice in _fired, _trigger_idx jumped by two — so coverage was
        inflated AND beat N+1 was never briefed, never delivered and never
        scored, with nothing in the record to show it had been skipped. The beat
        is therefore re-read inside the lock, where a beat the other caller has
        already spent is visible as the next one.
        """
        # Serialised against _steer's own re-brief as well: two session.updates
        # on one wire do not merge, the later simply replaces the earlier.
        async with self._brief_lock:
            trigger = self._next_trigger()
            if trigger is None:
                return
            direction = self._trigger_instruction(trigger, probing=probing)
            instructions = self._instructions() + (
                f"\n\nDIRECTOR NOTE (follow precisely, never mention): {direction}"
            )
            try:
                await self.rt.update_instructions(instructions)
            except Exception as exc:  # noqa: BLE001
                self.session.store.event(
                    "trigger_brief_failed",
                    trigger_id=trigger["id"],
                    interaction=self._interaction_id(),
                    segment=self.segment,
                    index=self._trigger_idx,
                    agent_id=self.agent_id,
                    probing=probing,
                    message=str(exc),
                )
                return
            self._fire_trigger(trigger, probing=probing)
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
                "director_model": (self.director.model if getattr(self, "director", None)
                                   else provenance()["text_model"]),
            }
            self.session.store.event("stage_direction", **self._pending_direction)

    async def _brief_member(self, agent_id: str, *, probing: bool = False) -> None:
        """Fold the next planted beat into one room member's brief.

        Called from _run_group_turn while the chosen speaker is between
        responses (its auto-fired reply already suppressed), so a group
        interaction actually fires its scored triggers instead of only advancing
        when the participant clicks 'move on'. The trigger is evaluated in the
        member's own context, because in a series a beat is bound to a
        particular character.

        Ownership in a group room is the CALLER's business, not this method's:
        both callers resolve _trigger_agent first, and reach here only with the
        member the beat is written for, with a beat that names nobody, or (in
        _probe_room) with a stand-in because the named character's session has
        died. This method spends whatever beat is next on whoever it is handed,
        so a caller that skips that check plants Dan's line in Priya's mouth.

        `probing` selects the beat's on_silence line instead of its cue, and is
        recorded on the steering pair so a rater can tell a probed response
        apart from a volunteered one. _probe_room passes it.

        The beat is spent only once the brief has actually left this process, for
        the reason _brief_next_beat spells out: a session.update that raised left
        trigger_fired standing for a beat nobody was ever told to perform, and
        the callers' retraction machinery could not help because it is gated on
        the floor grant, which is never reached when the brief itself throws.
        Once the brief HAS landed the beat is fired, and from there the grant
        failure is the callers' retraction to write.

        Serialising the read against the fire is also the CALLER's business here,
        for the reason _brief_next_beat had to take that pair under _brief_lock:
        two tasks that read _next_trigger() before either fires it both spend the
        same beat, which inflates coverage by one and skips the next beat
        entirely. Both callers today (_run_group_turn, _probe_room) hold
        self._floor across their whole brief-and-grant sequence, which is what
        makes that safe — a caller that reaches here without the floor
        reintroduces the double fire."""
        if self.room is None:
            return
        rt = self.room.session_for(agent_id)
        agent = next((a for a in self._resolve_agents() if a.id == agent_id), None)
        if rt is None or agent is None:
            return
        prev_agent, prev_id = self.agent, self.agent_id
        self.agent, self.agent_id = agent, agent_id
        try:
            trigger = self._next_trigger()
            if trigger is None:
                return
            direction = self._trigger_instruction(trigger, probing=probing)
            instructions = self._instructions() + (
                f"\n\nDIRECTOR NOTE (follow precisely, never mention): {direction}"
            )
            try:
                await rt.update_instructions(instructions)
            except Exception as exc:  # noqa: BLE001
                self.session.store.event(
                    "trigger_brief_failed",
                    trigger_id=trigger["id"],
                    interaction=self._interaction_id(),
                    segment=self.segment,
                    index=self._trigger_idx,
                    agent_id=agent_id,
                    probing=probing,
                    message=str(exc),
                )
                return
            self._fire_trigger(trigger, probing=probing)
            self._pending_direction = {
                "turn": self._turn_index,
                "segment": self.segment,
                "interaction": self._interaction_id(),
                "agent_id": agent_id,
                "agent_name": agent.name,
                "voice": getattr(rt, "voice", None),
                "stage_direction": direction,
                "trigger_id": trigger["id"],
                "esci": trigger.get("esci", []),
                "probing": probing,
                "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest()[:16],
                "director_model": (self.director.model if getattr(self, "director", None)
                               else provenance()["text_model"]),
            }
            self.session.store.event("stage_direction", **self._pending_direction)
        finally:
            self.agent, self.agent_id = prev_agent, prev_id

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
                # The rule that still stands is: never session.update a room
                # member MID-STREAM. A mid-stream re-brief silently mutes the
                # session, which is what made every routed speaker time out.
                # _probe_room is not mid-stream: it takes the same floor lock a
                # normal group turn takes and re-briefs the chosen character
                # immediately before granting them the floor, the ordering
                # _brief_member + give_floor already rely on. Skipping the
                # probe here instead meant a participant who froze in S3/S4
                # produced a silent WAV and an empty transcript — missing data
                # where the study design wants scoreable avoidance, and every
                # on_silence line authored for a group trigger was dead text.
                trigger = self._next_trigger()
                if trigger is None or not trigger.get("on_silence"):
                    continue
                # The idle clock is deliberately NOT reset here. _probe_room
                # can find the floor already held — by the opening turn, or by
                # a group turn still being served — and return having done
                # nothing at all. Resetting first would charge that no-op a
                # full PROBE_AFTER_SECONDS, so a probe that merely collided
                # with an in-flight turn was silently deferred instead of
                # retried on the next tick. _probe_room resets the clock itself
                # once it holds the floor and has a beat to deliver.
                #
                # Tracked, so an interaction change cancels the probe instead of
                # leaving it holding the floor into the next scene.
                self._spawn_group_turn(self._probe_room())
                continue
            trigger = self._next_trigger()
            if trigger is None or not trigger.get("on_silence"):
                continue
            self._last_activity = time.time()
            await self._brief_next_beat(probing=True)
            await self.rt.send_audio(b"\x00" * 3200)
            await self.rt.commit_turn()

    async def _probe_room(self) -> None:
        """Probe a silent participant inside a group room.

        The beat's own character does the probing where the beat names one
        (S3A's t1 is Alex's public challenge, so Alex presses); otherwise the
        floor rotates away from whoever spoke last, so the room does not become
        one person nagging.

        Runs under the same floor lock a normal group turn takes, which is what
        keeps the mid-stream prohibition intact: holding the floor means no
        member has a reply in flight, so the re-brief that carries the
        on_silence line lands between turns and immediately before that member
        is given the floor — the ordering _brief_member and give_floor already
        establish. A session.update to a member MID-response would still
        silently mute it; nothing here does that.
        """
        if self.room is None or self._closed:
            return
        if self._floor.locked():
            return  # a turn is already being served; the room is not silent
        async with self._floor:
            room = self.room
            if room is None or self._closed:
                return
            trigger = self._next_trigger()
            if trigger is None or not trigger.get("on_silence"):
                return
            speaker = self._trigger_agent(trigger)
            if speaker not in room.sessions:
                order = [a for a in self.agent_order() if a in room.sessions]
                if not order:
                    return
                speaker = next(
                    (a for a in order if a != self._last_group_speaker), order[0]
                )
            # Briefing has to come first — a session.update to a member
            # mid-response mutes it, so the beat can only be folded in while
            # the floor is ungranted — but briefing is also what SPENDS the
            # beat: _fire_trigger writes trigger_fired, appends to _fired and
            # advances _trigger_idx. Snapshot that, because the grant below can
            # still fail on a member whose session has died, and verify_record
            # counts trigger_fired events as coverage of the scenario's planted
            # beats. A probe nobody spoke must not be scored as one the
            # participant faced; an overstated record is the failure this
            # instrument cannot tolerate.
            spent_idx, spent_fired = self._trigger_idx, len(self._fired)
            await self._brief_member(speaker, probing=True)
            if self._closed or self.room is not room:
                return
            self._response_done.clear()
            if await room.give_floor(speaker) is None:
                # Same as the scene open: a failed grant leaves `speaking` set
                # to a member that is no longer in the room, which would mute
                # everyone else for the rest of the interaction.
                room.speaking = None
                if self._trigger_idx != spent_idx:
                    # Put the beat back so it is offered again, and retract the
                    # claim that it landed. The trigger_fired line _fire_trigger
                    # already wrote stays in the log — the log is append-only —
                    # so this event carries the same `index` to cancel it out;
                    # anything that counts trigger_fired as coverage has to net
                    # the two.
                    self.session.store.event(
                        "trigger_undelivered",
                        trigger_id=self._fired[-1] if self._fired else None,
                        interaction=self._interaction_id(),
                        segment=self.segment,
                        index=spent_idx,
                        agent_id=speaker,
                        probing=True,
                        reason="floor_grant_failed",
                    )
                    self._trigger_idx = spent_idx
                    del self._fired[spent_fired:]
                    # The direction was never spoken, so it must not be left
                    # pending and paired with whichever turn finalises next.
                    self._pending_direction = None
                return
            self._last_activity = time.time()
            try:
                await asyncio.wait_for(self._response_done.wait(), timeout=45)
            except asyncio.TimeoutError:
                self.session.store.event(
                    "group_turn_timeout", agent_id=speaker, probing=True
                )
                rt = room.session_for(speaker)
                if rt is not None:
                    rt.clear_response_state()
            if self.room is room:
                room.speaking = None
            self._last_activity = time.time()

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
            "director_model": (self.director.model if getattr(self, "director", None)
                               else provenance()["text_model"]),
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
            # Snapshot the room: this task can await up to 45 s, and if the
            # participant advances during that wait _close_room sets self.room =
            # None. Dereferencing self.room afterwards would crash this
            # background task with AttributeError. Bail whenever the room we
            # started with is no longer current.
            room = self.room
            if room is None or self._closed:
                return
            if not room.sessions:
                # Every member's session has been dropped. There is nobody to
                # give the floor to, and waiting anyway would spend 45 s per
                # turn discovering that. Say so instead.
                self.session.store.event(
                    "group_turn_no_members", segment=self.segment,
                    interaction=self._interaction_id(),
                )
                return
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
            if self._closed or self.room is not room:
                return
            fresh = self._last_user_text if self._last_user_text != before else ""

            named_early = self._named_in(fresh)
            first = named_early
            # The director may return an ORDERED multi-speaker sequence (e.g.
            # [A, B, A]); capture it so the follow-up beats play it in order
            # instead of being re-decided by a second route() call. None when the
            # participant addressed someone directly (no director call was made).
            routed_seq = None
            if first is None:
                try:
                    routed = await self.director.route(
                        self.session.shared_history, fresh
                    )
                    candidates = [
                        r.get("agent_id") for r in routed
                        if r.get("agent_id") in room.sessions
                    ]
                    # The director only dedupes CONSECUTIVE speakers against its
                    # static cast, but room.sessions can shrink mid-encounter (a
                    # member's gateway session dropped), so filtering to live
                    # members can make previously non-adjacent duplicates adjacent
                    # (e.g. [A, dead, A] -> [A, A]). Collapse consecutive repeats
                    # so one agent is never handed the floor twice in a row, while
                    # a genuine [A, B, A] rebuttal (separated by B) is preserved.
                    collapsed = []
                    for c in candidates:
                        if not collapsed or collapsed[-1] != c:
                            collapsed.append(c)
                    candidates = collapsed
                    routed_seq = candidates
                    # Anti-dominance: absent a direct address, prefer a
                    # candidate who did not just speak.
                    first = next(
                        (c for c in candidates if c != self._last_group_speaker),
                        candidates[0] if candidates else None,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.session.store.event("director_error", message=str(exc))
            if self._closed or self.room is not room:
                return
            if first is None:
                # No signal at all: rotate the floor instead of always
                # falling back to the cast's first-listed character.
                if self._last_group_speaker in order and len(order) > 1:
                    nxt = (order.index(self._last_group_speaker) + 1) % len(order)
                    first = order[nxt]
                else:
                    first = order[0]
            if first not in room.sessions:
                # Direct address and the rotation both pick from the scenario's
                # cast, not from who is still on the wire, so a name spoken to a
                # character whose session has dropped used to reselect that
                # character every single turn — each one a briefed beat handed to
                # nobody and 45 s of dead air. Rotate to someone who can actually
                # answer, the way _probe_room already does.
                live = [a for a in order if a in room.sessions]
                if not live:
                    self.session.store.event(
                        "group_turn_no_members", segment=self.segment,
                        interaction=self._interaction_id(),
                    )
                    return
                wanted_speaker = first
                first = next(
                    (a for a in live if a != self._last_group_speaker), live[0]
                )
                self.session.store.event(
                    "speaker_unavailable", wanted=wanted_speaker,
                    reassigned_to=first, segment=self.segment,
                )

            # Group mode has no per-turn re-brief otherwise, so its planted
            # (scored) triggers would never fire. Fold the next beat into the
            # chosen speaker's brief now, while they are between responses.
            #
            # But only if the beat is theirs. A planted beat is written for a
            # named character — S4A i1 t2 is Dan restating Chris's idea as his
            # own — and the router picks the speaker from what the participant
            # just said, which has nothing to do with whose beat is next. Hand
            # Dan's beat to Priya and she is told to perform a line her own
            # brief and identity block forbid ("Dan and Chris are other people
            # in this scene, not you"), and the trigger is logged as fired with
            # its ESCI items either way: a beat the instrument never actually
            # staged, recorded as though it had been. _probe_room already binds
            # by name through _trigger_agent; do the same here, and when the
            # routed speaker is not the beat's character leave the beat unspent
            # rather than spending it on someone who cannot perform it. It
            # keeps for the turn its own character takes the floor, or for the
            # silence probe, which routes to that character deliberately. A
            # beat that names nobody is unowned and belongs to whoever speaks.
            pending = self._next_trigger()
            owner = self._trigger_agent(pending) if pending else None
            # Bound on both branches. The retraction below reads these, and it
            # runs whenever the floor grant fails — including on the deferral
            # branch, where no beat was spent. Binding them only inside the
            # else raised UnboundLocalError there, which killed the group turn
            # task and left the room's floor held, so nobody could answer the
            # participant for the rest of the interaction.
            spent_idx, spent_fired = self._trigger_idx, len(self._fired)
            if pending is not None and owner is not None and owner != first:
                self.session.store.event(
                    "trigger_deferred",
                    trigger_id=pending["id"],
                    interaction=self._interaction_id(),
                    segment=self.segment,
                    index=self._trigger_idx,
                    agent_id=owner,
                    routed_to=first,
                    reason="beat_belongs_to_another_character",
                )
            else:
                # _brief_member SPENDS the beat: it writes trigger_fired,
                # appends to _fired and advances _trigger_idx. The grant below
                # can still fail on a member whose session has died, and a beat
                # nobody spoke must not be counted as one the participant
                # faced, so the snapshot above is what puts it back.
                await self._brief_member(first)
            if self._closed or self.room is not room:
                return

            self._response_done.clear()
            granted = await room.give_floor(first)
            if granted is None:
                # A failed grant means nobody was asked to speak, so there is
                # nothing to wait for. Falling through to the wait below spent
                # the full 45 s on every such turn — and give_floor sets
                # `speaking` before it fails and pops the member, so throughout
                # that wait `speaking` named someone no longer in the room, which
                # makes every other member's has_floor test False and their
                # replies get cancelled as unsolicited. The room was provably
                # mute for the whole timeout, once per turn, for as long as the
                # participant kept addressing the character that dropped.
                room.speaking = None
                self.session.store.event(
                    "floor_grant_failed", agent_id=first, segment=self.segment,
                    interaction=self._interaction_id(),
                )
                if pending is not None and self._trigger_idx != spent_idx:
                    # Put the beat back and retract the claim that it landed.
                    # The trigger_fired line already written stays in the
                    # append-only log, so this event carries the same `index` to
                    # cancel it out; everything that counts coverage nets the
                    # two.
                    self.session.store.event(
                        "trigger_undelivered",
                        trigger_id=self._fired[-1] if self._fired else None,
                        interaction=self._interaction_id(),
                        segment=self.segment,
                        index=spent_idx,
                        agent_id=first,
                        probing=False,
                        reason="floor_grant_failed",
                    )
                    self._trigger_idx = spent_idx
                    del self._fired[spent_fired:]
                    # The direction was never spoken, so it must not be left
                    # pending and paired with whichever turn finalises next.
                    self._pending_direction = None
                return
            try:
                await asyncio.wait_for(self._response_done.wait(), timeout=45)
            except asyncio.TimeoutError:
                if self._closed or self.room is not room:
                    return
                rt_dbg = room.session_for(first)
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
            if self._closed or self.room is not room:
                return

            # The transcript arrived with that first commit; a direct address
            # we could not honour up front gets the next turn instead. Only a
            # name from THIS turn counts, a name said last turn is history.
            if self._last_user_text != before:
                fresh = self._last_user_text
            named = self._named_in(fresh)
            followups = []
            if named and named != first:
                # A name spoken THIS turn is a direct address and overrides the
                # director's planned sequence.
                followups.append(named)
            elif routed_seq is not None:
                # Honour the director's ORDERED sequence from the first route()
                # call (e.g. [A, B, A]) instead of discarding it and re-deciding
                # with a second LLM call: play the speakers that followed `first`
                # in the returned order, so an authored back-and-forth (g5's
                # [claire, arjun, claire]) actually plays. Bounded by the
                # director's own max sequence length.
                idx = routed_seq.index(first) if first in routed_seq else -1
                followups = routed_seq[idx + 1:][: DIRECTOR_MAX_SPEAKERS - 1]
            else:
                # Direct-address opener: no director sequence was produced, so ask
                # for a single follow-up to keep the room responsive.
                try:
                    routed = await self.director.route(
                        self.session.shared_history, fresh
                    )
                except Exception as exc:  # noqa: BLE001, never break the room
                    self.session.store.event("director_error", message=str(exc))
                    routed = []
                followups = [
                    r.get("agent_id") for r in routed
                    if r.get("agent_id") in room.sessions
                    and r.get("agent_id") != first
                ][:1]
            if self._closed or self.room is not room:
                return

            self.session.store.event(
                "director_route", speakers=[first] + followups, addressed=named
            )
            for aid in followups:
                if self._closed or self.room is not room or self.vad.speaking:
                    if self.vad.speaking:
                        self.session.store.event(
                            "followup_yielded", agent_id=aid,
                        )
                    break
                self._response_done.clear()
                await room.give_floor(aid)
                try:
                    await asyncio.wait_for(self._response_done.wait(), timeout=45)
                except asyncio.TimeoutError:
                    self.session.store.event("group_turn_timeout", agent_id=aid)
                if self._closed or self.room is not room:
                    return
            room.speaking = None
            await self._steer()
        # A group interaction otherwise has no automatic exit: the 1:1 path
        # reaches _maybe_advance from _finalize_turn, but the group path never
        # did, so S3/S4 stalled in interaction 1 unless an actor happened to
        # call end_conversation or the participant clicked 'move on'. Same
        # turn-and-time gate as 1:1 — _maybe_advance decides.
        #
        # Spawned as a task that is deliberately NOT tracked as a group turn:
        # advancing runs _close_room, which cancels every tracked group turn,
        # and this method is running inside one of them. Advancing inline would
        # cancel the advance halfway through tearing the room down and leak the
        # member sockets.
        if not self._closed:
            asyncio.ensure_future(self._advance_when_spent())

    async def _advance_when_spent(self) -> None:
        """_maybe_advance off the group-turn task, with its errors logged.

        Untracked tasks lose their exceptions, and the chain below reaches
        _enter and the gateway, so a hiccup there must land in the session log
        rather than vanish."""
        try:
            await self._maybe_advance()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.session.store.event(
                "voice_error", where="group_maybe_advance", message=str(exc)
            )

    def _named_in(self, text: str) -> Optional[str]:
        """The character the participant addressed by name, if any.

        Match whole tokens, not substrings: a raw `in` test made 'Dan' fire on
        'abundant' and, because direct address overrides the director, seize the
        floor from whoever should have spoken."""
        if not text:
            return None
        tokens = _norm_speech(text).split()
        for a in self._resolve_agents():
            name_tokens = _norm_speech(a.name).split()
            if not name_tokens:
                continue
            n = len(name_tokens)
            if any(tokens[i:i + n] == name_tokens for i in range(len(tokens) - n + 1)):
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

        This method is also the only code that knows whether a shift reaches the
        actor, so it is what answers set_knob's `delivered`. In a room the answer
        is known before the review even runs and it is always False (see the
        group branch below); in 1:1 it depends on whether a reply is in flight
        when the review comes back, so the shift is written undetermined and
        resolved by the steer_deferred / steer_delivered event that follows it.
        """
        group = self.is_group()
        before = len(self.session.steering_log)
        # False, not None, for a room: nothing in this method re-briefs a room
        # member, so at the moment the controller decides, the shift has reached
        # nobody. It was being recorded exactly like a shift that had landed —
        # in S3 and S4, half the study — so the steering log read as a stimulus
        # history when it was a list of intentions.
        await self.session.auto_steer(delivered=False if group else None)
        if len(self.session.steering_log) == before:
            return  # nothing changed; the current brief still stands
        if group:
            # Room members keep their opening brief; steering shifts are
            # recorded for the log. A blanket re-brief here would have to reach
            # every member at once, including whoever is mid-reply, and a
            # mid-stream session.update silently mutes this bridge. (The probe
            # and the interaction-change re-brief are different: each touches a
            # single member between turns, with no response in flight.)
            #
            # The shifts this review made are already on the record as
            # delivered=False, so the gap is stated where it will be read rather
            # than left to be inferred from the absence of a re-brief. What
            # eventually carries them to a member is the next _brief_member or
            # the interaction-change re-brief, each of which writes its own
            # event; there is no re-brief here to record.
            return
        async with self._brief_lock:
            if self.rt.responding:
                # A steering review can take up to eleven seconds, so it
                # routinely lands in the middle of the NEXT reply. This re-brief
                # is built from _instructions() alone, which never carries a
                # DIRECTOR NOTE, so landing it here replaced the stage direction
                # the record says was delivered — the planted beat's note, still
                # governing the reply in flight — with an unsteered brief, while
                # the stage_direction event stood on disk describing something
                # that was withdrawn before it could govern anything. A
                # session.update mid-response is also the one thing this bridge
                # is documented to swallow the session over. Defer: persona
                # shifts are recorded either way and the next brief carries
                # them.
                self.session.store.event(
                    "steer_deferred", agent_id=self.agent_id,
                    segment=self.segment, reason="response_in_flight",
                )
                return
            instructions = self._instructions()
            if self._pending_direction:
                # A direction that has not been performed yet must survive its
                # own steering pass, or the beat is briefed and then silently
                # un-briefed before the actor ever speaks it.
                instructions += (
                    "\n\nDIRECTOR NOTE (follow precisely, never mention): "
                    + str(self._pending_direction.get("stage_direction") or "")
                )
            await self.rt.update_instructions(instructions)
            # The other half of steer_deferred. Those shifts went out as
            # delivered=None because nothing could know yet; this says they
            # landed, so the two outcomes of a 1:1 review are both stated and an
            # analyst never has to read delivery out of an absence.
            self.session.store.event(
                "steer_delivered", agent_id=self.agent_id,
                segment=self.segment,
                shifts=len(self.session.steering_log) - before,
            )

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
