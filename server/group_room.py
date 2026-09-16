"""A group interaction: one realtime session per character.

A single session cannot run a room. Through the LiteLLM bridge a conversation
yields exactly one reply per committed turn, so only the first routed speaker
ever answers, and re-briefing one session mid-conversation does not change who
the model thinks it is. The visible symptom is that whoever spoke first answers
everything: address Priya by name and Dan replies on her behalf.

So each character gets its own session, permanently briefed as that character.
Participant audio is fanned out to all of them, so everyone hears the room. When
the director picks a speaker, only that character's session is asked to reply,
and its audio is fed back into the others' input buffers so they hear what was
said. That is what makes calling on someone by name work.

WHAT THE GATEWAY ACTUALLY DOES WITH THAT PLAN
---------------------------------------------
Measured 2026-09-10 against api.ai.it.cornell.edu, with a real three-member room
plus scribe on both families, one synthesized participant utterance, exactly one
member committed:

* `nto.gemini-live-2.5-flash` answers on EVERY open session after speech plus
  silence, committed or not. All three members replied; so did the scribe,
  briefed "Never speak", which the model accepted and then ignored. Committing
  is therefore not a floor on that family and never was — the participant hears
  one voice only because the runner's per-member pump drops the other two.
  Worse, `response.cancel` does not stop a reply that has started (one cancelled
  reply went on to deliver 44 more audio deltas), so the discarded replies are
  generated and billed in full.
* `gpt-realtime-2.1` behaves the same way with the gateway's own turn detection
  left on, and stops when it is switched off. With `turn_detection: null` in the
  session dict the two members that were not committed produced nothing at all —
  no response, no commit, no cost — and the documented append -> commit ->
  response.create contract worked as written, without the
  `input_audio_buffer_commit_empty` error the default path throws on every
  grant.

So the floor is a real mechanism on one family and a downstream filter on the
other. This module says which rather than describing the floor it wishes it had:
the previous give_floor promised that members without the floor "keep the turn
in their (uncommitted) buffer", and neither family has ever done that.

WHERE THE PER-FAMILY ANSWERS COME FROM
--------------------------------------
`REALTIME_FAMILIES` in server/voice/realtime.py, read here through
`capabilities_for()`. One table, one place, no second copy in this file — the
row already carries the voices a family will accept and whether its own turn
detection has to be switched off, and RealtimeVoiceSession puts the second of
those on the wire itself. What the room adds is what it does with the answers:
which voice a character may safely be given, and whether the floor it grants is
a mechanism or a filter.

The voice matters more than it looks. `session.update` is all-or-nothing on this
gateway, so one voice name the family does not recognise takes the character
brief down with it: `voice="Puck"` on gpt-realtime-2.1 comes back
`invalid_value` with NO `session.updated`, and the actor then plays the
gateway's stock assistant instead of the character; an ElevenLabs voice_id is
refused the same way and a Gemini session that gets one is silent for good. Both
reproduced live. The scribe used to be built with a hardcoded "Puck", which is
how a channel briefed "Never speak" came to speak in a gpt room.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Callable, Dict, List, Optional

from .voice import realtime as _realtime
from .voice.realtime import _rms as _frame_rms
from .voice.realtime import (RealtimeVoiceSession, SilenceDetector,
                             capabilities_for, accepts_text_items,
                             is_openai_realtime, relays_colleagues_as_text,
                             grants_via_text_prompt, member_tools_allowed)


def _configured_model() -> str:
    """The model this PROCESS is on, read at call time.

    Not `from .voice.realtime import MODEL`, which is what used to stand here.
    That binds the value this module was imported with, and the runner
    deliberately does not: `realtime_voice_session.realtime_model()` reads the
    same attribute off the bridge module on every call, "rather than off a live
    session", so a model resolved after this module was imported is still the
    one the encounter runs on. A room that froze it disagreed with the runner
    silently, and the disagreement is not cosmetic — reproduced with the bridge
    pointed at gpt-realtime-2.1 after import, the runner cast the characters in
    gpt voices while the room read the gemini-live row, so `floor_is_real` came
    back False and `close_participant_turn` returned on its first line: an
    encounter with a perfect agent transcript and not one word the participant
    said, which is the exact failure the scribe-commit round had just repaired.
    One clock, not two.
    """
    return getattr(_realtime, "MODEL", "") or ""

# The room's own name for the transcription channel, so a lost scribe can be
# reported through the same door as a lost member.
SCRIBE_ID = "scribe"

# 300 ms of silence. Where the gateway's own turn-taking is off, the input
# buffer holds exactly what we appended, and this is what makes a commit legal
# on a session that has heard nothing — committing a genuinely empty buffer is
# an error on gpt and produces no turn at all on Gemini.
_SILENCE_PAD = b"\x00" * 9600

# What a member is told when the floor is handed to it on a family whose row
# says grant_via_text_prompt. Phrased as a cue and not as content: the member
# has already HEARD the participant (or been told what a colleague said, see
# tell()), so this only says "now, you".
_TEXT_GRANT_NUDGE = (
    "(The participant is waiting for you to answer what they just said. "
    "Reply now, in character, one or two sentences.)"
)

# How long an auto-fired reply is presumed to still be in flight. The same
# window, and the same reason, as RealtimeVoiceSession.commit_turn: while the
# gateway is already answering this turn, asking it for a second answer gets a
# second answer, spoken straight onto the end of the first.
_AUTOFIRE_WINDOW_S = 15.0

# How long a granted turn waits to see whether the gateway answers the commit on
# its own before asking it to. AUTOFIRE_WAIT is the runner's existing knob for
# exactly this decision on the 1:1 path (_client_to_model, which records an
# `autofire_adopted` when the wait pays off), and there should be one knob, not
# two. It is a ceiling and not a delay: the wait ends the moment the first delta
# of the reply arrives.
_ADOPT_WAIT_ENV = "AUTOFIRE_WAIT"
_ADOPT_WAIT_DEFAULT = "1.5"

# The voice a session should be opened with when the room has no opinion. Empty
# means "this family's own default", which resolve_voice fills in from the
# table; it is the only value that is right on every family at once.
_FAMILY_DEFAULT_VOICE = ""


class GroupRoom:
    """Holds one live session per character and decides who speaks."""

    def __init__(self, agents: List, instructions_for: Callable[[object], str],
                 voice_for: Callable[[object], str], tools: Optional[list] = None,
                 *, model: Optional[str] = None,
                 on_lost: Optional[Callable[[str, str], None]] = None):
        self.agents = list(agents)
        self._instructions_for = instructions_for
        self._voice_for = voice_for
        self._tools = tools or []
        # THE model this room runs on: the one its capability row is read from
        # AND the one every session in it is opened on. Those used to be two
        # different answers. The comment here claimed "a room and the sessions
        # in it can never disagree about which family they are on" on the
        # strength of both defaulting to REALTIME_MODEL — true only while
        # nobody passed `model`, and the whole point of the parameter is that
        # somebody does. Given model="gpt-realtime-2.1" with the process
        # default left alone, this room picked the gpt row, cast three
        # characters into gpt voices (`alloy`, `ash`, `ballad`) and then handed
        # them to sessions that still opened on nto.gemini-live-2.5-flash,
        # where connect() refuses every one of those voices by name. A group
        # encounter that fails at the top, with a participant sitting there.
        # See _new_session for the other half.
        #
        # `caps` is None when the model belongs to a family the table does not
        # cover: connect() refuses such a model loudly, and until it does the
        # room simply stops second-guessing what it is handed.
        self._model = model or _configured_model()
        self.caps = capabilities_for(self._model)
        self.sessions: Dict[str, RealtimeVoiceSession] = {}
        # The scribe hears ONLY the participant. Member sessions cannot supply
        # the participant transcript: their input buffers also carry the other
        # characters' fanned-out audio, so the bridge's input transcription
        # mixes agent speech into what it labels the user. A dedicated session
        # that never hears an agent gives a clean participant channel.
        self.scribe: Optional[RealtimeVoiceSession] = None
        self.speaking: Optional[str] = None
        # agent id (or SCRIBE_ID) -> why that channel stopped taking audio.
        # hear() used to swallow this entirely; see hear().
        self.lost: Dict[str, str] = {}
        # Called with (channel_id, reason) the first time a channel goes deaf,
        # so the runner can put it in the record. Optional, because the room has
        # no store of its own and must not need one in order to be correct.
        self._on_lost = on_lost
        # agent id -> the voice the caller asked for, where the family would
        # have refused it and the room opened the session on a voice from the
        # family's own roster instead. A study has to be able to see this: it is
        # a character not sounding the way the scenario said it would.
        self.voice_substitutions: Dict[str, str] = {}
        # Floor grants the room declined to ask for, because the gateway was
        # already answering that turn on its own. On an auto-firing family this
        # counts the doubled replies that are no longer being produced.
        self.autofire_grants = 0
        # agent id -> bytes of audio this room has fanned to that member since
        # it last held the floor. The room's own books, kept because nothing on
        # the session can answer this: pending_input counts what was appended
        # and is zeroed by a suppressing pump, and the gateway's own commit
        # consumes the buffer without saying so. What it decides is whether
        # there is any turn for the gateway to answer unprompted — a member who
        # has heard nothing will never auto-fire, so a grant to one should not
        # spend AUTOFIRE_WAIT finding that out (see give_floor).
        self._fanned_since_grant: Dict[str, int] = {}
        # Bytes of participant audio fanned to the scribe since its buffer was
        # last closed. Kept separately from the members' counter above because
        # it answers a different question: not "is there a turn for the gateway
        # to answer" but "is there a turn for it to TRANSCRIBE", and on a family
        # whose own turn detection is switched off nothing closes that buffer
        # unless this room does. See close_participant_turn.
        self._fanned_to_scribe = 0
        # Whether any of those bytes were loud enough to be somebody speaking.
        #
        # The two questions are not the same, and treating them as one is how a
        # guard that reads like a guard stops being one. The participant's page
        # streams CONTINUOUSLY — the worklet sends 20 ms frames of quiet while
        # nobody is talking, and every one of them is fanned to the scribe — so
        # the byte counter above is non-zero within 20 ms of a turn being
        # closed and stays that way for the rest of the encounter. It answers
        # "is a commit legal here" (an empty buffer is
        # `input_audio_buffer_commit_empty` on gpt and no turn at all on
        # Gemini) and nothing more. It cannot answer "was there a turn", and
        # close_participant_turn's promise not to hand the record "a transcript
        # of nothing, attributed to the participant" needs an answer to the
        # second question: a buffer of pure quiet is exactly what a
        # transcription model invents words over, and an invented participant
        # turn is worse than a missing one because nothing downstream can tell
        # it from a real one.
        #
        # Same threshold as the runner's own turn detection, read the same way,
        # so "loud enough to be speech" has one definition in this process
        # rather than two that can drift apart.
        self._scribe_heard_speech = False
        self._speech_rms = SilenceDetector().threshold
        # How many participant turns this room closed on the scribe itself. On
        # the configured family this stays at zero for a whole encounter and
        # that is correct - the gateway's own turn detection closes them. On a
        # family where it is switched off, a zero here at the end of a group
        # encounter means the participant was never transcribed at all.
        self.scribe_commits = 0
        # How many times a scribe was replaced because it stopped transcribing
        # while its socket was still healthy (see reopen_scribe).
        self.scribe_reopens = 0
        # agent id -> what the last rebrief() got back from update_instructions:
        # True acked, False refused or never sent, None unknowable, or the
        # exception. gather(return_exceptions=True) used to drop all of it.
        self.last_rebrief: Dict[str, object] = {}

    # ── what the family can do ─────────────────────────────────────────────
    @property
    def model(self) -> str:
        """The model this room is on, readable rather than private.

        Everything downstream that has to name a model — the record's
        `steer_unacked`, a harness choosing which family it is measuring — was
        asking the process instead, which is the right answer only while the
        room and the process agree. They need not: that is what `model=` is
        for.
        """
        return self._model

    @property
    def steering_is_real(self) -> bool:
        """True when a per-turn direction can actually reach a character here.

        The same shape as `floor_is_real` and read from the same one table, one
        row further along: `honours_session_update`. A mid-session
        `session.update` is the only route a direction has on this bridge, and
        on `nto.gemini-live-2.5-flash` it is inert — three frames sent, zero
        `session.updated` back, no error, and an actor that went on ignoring a
        direction the same frame made a gpt actor obey.

        Asked BEFORE anything is sent, not after, and that is the point of
        having it here. A room on a family that cannot carry a direction sends
        nothing at all for one: no extra frame per turn on the configured
        model, and no `session.update` landing on a member who is mid-reply,
        which on that family is how a member goes silent for good. What the
        caller owes in exchange is to write the direction down as undelivered
        rather than skipping it quietly — see _report_unacked_steering in
        server/realtime_voice_session.py, which is what says so on the record
        and on the researcher's strip.
        """
        return bool(self.caps and self.caps.honours_session_update)

    @property
    def floor_is_real(self) -> bool:
        """True when granting the floor actually silences everybody else.

        Reads `needs_turn_detection_null` from the family's row, because on this
        gateway that key is the only thing that stops a session answering a turn
        it was not given: a family whose row sets it has its own turn detection
        switched off by RealtimeVoiceSession._session_payload, and an
        uncommitted member then produces nothing. A family whose row does not
        answers on every open session, and the floor is the runner's pump
        discarding what it did not ask for — after it has been generated and
        paid for.
        """
        return bool(self.caps and self.caps.needs_turn_detection_null)

    def _voice_for_agent(self, agent) -> str:
        """The voice this character may safely declare.

        A voice the family does not recognise is not a cosmetic problem. The
        gateway refuses the whole `session.update`, so the character brief goes
        with it and the actor plays the gateway's stock assistant (gpt) or
        nothing at all (Gemini) — and connect() now refuses such a voice
        outright, which for a room means no group interaction opens at all.
        Group scenarios still carry ElevenLabs voice_ids from the retired v1
        cascade, so this is not hypothetical.

        A cast keeps distinct voices, so an unrecognised one is replaced by this
        family's voice at the same cast position rather than by the family
        default — that would give every character in the room one voice. The
        substitution is recorded, never silent: a character that does not sound
        the way the scenario cast it is a fact about the encounter.
        """
        requested = self._voice_for(agent)
        caps = self.caps
        if caps is None or not requested or caps.accepts_voice(requested):
            return requested
        idx = next((i for i, a in enumerate(self.agents) if a.id == agent.id), 0)
        self.voice_substitutions[agent.id] = requested
        return caps.voices[idx % len(caps.voices)]

    def _new_session(self, *, instructions: str, voice: str,
                     tools: Optional[list]):
        """One session, on the room's OWN model.

        Every session the room opens used to take RealtimeVoiceSession's
        default model, which is REALTIME_MODEL — so `model=` decided which
        capability row the room read and which voices it cast into, and decided
        nothing whatsoever about the socket that was opened. The two answers
        only ever agreed by coincidence, and where they did not the encounter
        died at connect(), loudly, on a voice from the wrong roster (see
        __init__). Where a voice happened to be legal on both, it would not
        have died: it would have run a whole encounter on a family nobody
        chose, with turn detection and input transcription set from the other
        family's row.

        Settled on the object rather than passed to the constructor, which
        looks like the more obvious spelling and is not available here.
        RealtimeVoiceSession is looked up on the module at call time precisely
        so that it can be substituted — the runner's tests and this project's
        live harnesses hand the room factories of their own — and those
        factories take (instructions, voice, tools). A fourth keyword would
        make the room raise TypeError instead of opening a session, in every
        one of them. Assignment reaches every substitute and the real class
        alike, and the real class reads `self.model` only at connect() time and
        later (`_ws_url`, `resolve_voice`, and `capabilities`, which is a
        property "resolved on demand, not at construction"), so a session
        settled here is the same session the constructor would have built.
        """
        rt = RealtimeVoiceSession(
            instructions=instructions, voice=voice, tools=tools,
        )
        # Only when the room has an opinion. A room built with no model has
        # already defaulted to the process's own, and writing it back would be
        # the same value; a factory that does not carry `model` at all keeps
        # whatever it chose, which is its business and not this room's.
        if self._model:
            rt.model = self._model
            # And re-settle the voice, because RealtimeVoiceSession.__init__
            # ran voice_for_model() against the PROCESS's model a moment ago,
            # which is the wrong family whenever the room has one of its own.
            # Left alone, a room on plain flash whose process is pointed at a
            # gpt model would open every member with the gpt alias of its
            # Gemini voice ("Puck" -> "alloy") and then die at connect() on a
            # voice the gemini roster does not contain. `voice` here has
            # already been checked against self.caps by _voice_for_agent.
            rt.voice = _realtime.voice_for_model(voice, self._model)
        # The family's measured end-of-turn window, settled the same way
        # `model` is and for the same reason (see the gemini row in
        # REALTIME_FAMILIES): a lost participant's 700-1300 ms mid-thought
        # pause must not close their turn on any of the room's sockets — the
        # members', which answer it, or the scribe's, which transcribes it.
        window = _realtime.end_of_turn_for(self._model)
        if window is not None:
            rt.turn_detection = window
        return rt

    def _member_tools(self) -> list:
        """The tools a ROOM MEMBER on this family may be given.

        Empty on native-audio, and this is a real capability loss recorded
        rather than hidden: that route calls end_conversation constantly and
        every call is an empty turn, which is why origin/main (5a45420) took
        the tools away from members there. So END_SEGMENT_TOOL -- our wiring
        that lets an actor end a group conversation and advance the encounter
        -- is NOT available on the deployed route, and a group segment there
        ends the way it did before the tool existed: the director's turn budget
        or the participant leaving.

        Everywhere else the tools stay, because that is where END_SEGMENT_TOOL
        was measured working. This is a per-family answer and not a global one
        precisely so that neither side loses its behaviour.

        The 1:1 actor is untouched: this is a room-member rule.
        """
        model = self._model or _configured_model()
        return self._tools if member_tools_allowed(model) else []

    async def open(self) -> None:
        async def start(agent):
            rt = self._new_session(
                instructions=self._instructions_for(agent),
                voice=self._voice_for_agent(agent),
                tools=self._member_tools(),
            )
            await rt.connect(open_conversation=False)
            self.sessions[agent.id] = rt

        async def start_scribe():
            # The family's own default voice, from the table, rather than the
            # hardcoded "Puck" that used to be here. The scribe never speaks to
            # anyone, so the voice buys it nothing — but a voice name this
            # family refuses would cost it the one thing it does have, its
            # brief, along with the whole session.update. That is exactly what
            # "Puck" did on gpt-realtime-2.1: no session.updated, no brief, and
            # a transcription channel answering the participant out loud.
            #
            # The brief is not what keeps it quiet either, and this says so
            # because the wording reads as though it were: both families
            # accepted "Never speak" and then answered the participant anyway.
            # What keeps the scribe out of the participant's ears is
            # _pump_scribe discarding its audio; what stops the reply being
            # generated at all is the turn_detection: null the table already
            # sends on the family that honours it.
            rt = self._new_session(
                instructions=(
                    "You are a silent transcription channel for an English "
                    "conversation. Never speak. If you must respond, reply "
                    "with a single space."
                ),
                voice=_FAMILY_DEFAULT_VOICE,
                tools=[],
            )
            await rt.connect(open_conversation=False)
            self.scribe = rt

        # gather with return_exceptions so a single failed connect does not
        # leave the siblings that DID connect running unclosed in the
        # background. Every started coroutine runs to completion (success or
        # exception) and registers itself; on any failure close() tears down
        # everything that registered before re-raising the first error.
        results = await asyncio.gather(
            start_scribe(), *(start(a) for a in self.agents),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            await self.close()
            raise errors[0]

    # ── listening ──────────────────────────────────────────────────────────
    async def _went_deaf(self, channel_id: str, rt, why: str) -> None:
        """Record that one channel has stopped taking audio, once."""
        if channel_id in self.lost:
            return
        self.lost[channel_id] = why
        if channel_id == SCRIBE_ID:
            self.scribe = None
        else:
            self.sessions.pop(channel_id, None)
        try:
            await rt.close()
        except Exception:  # noqa: BLE001 - it is already gone; that is the point
            pass
        if self._on_lost is not None:
            try:
                self._on_lost(channel_id, why)
            except Exception:  # noqa: BLE001 - reporting must not kill the turn
                pass

    async def reopen_scribe(self) -> Optional[RealtimeVoiceSession]:
        """Replace a scribe that has stopped transcribing with a fresh session.

        The SECOND way a scribe dies, and the one _went_deaf cannot see.
        _went_deaf catches a scribe whose socket is gone: a raised send, a ws
        that went None, a send_failures that moved. This one is alive, healthy
        on every surface, and has simply stopped emitting transcripts -- seen
        after about six turns on native-audio. Two detectors, one repair; the
        runner's scribe watchdog is the trigger for this shape and _went_deaf
        is the trigger for the other, and both land here.

        Built through _new_session, NOT through RealtimeVoiceSession directly:
        the room may be on a different model than the process, and the scribe
        that used to be constructed here with a hardcoded "Puck" is exactly the
        session that lost its whole brief on gpt-realtime-2.1 -- no
        session.updated, and a transcription channel answering the participant
        out loud. The replacement has to be the same kind of session the
        original was, or a scribe repair becomes a scribe that talks.

        The fan-out bookkeeping is reset with it. _fanned_to_scribe and
        _scribe_heard_speech describe a buffer that no longer exists; carried
        over, they would say the new scribe had already heard the participant
        speak, and close_participant_turn would commit a buffer holding
        nothing.
        """
        old = self.scribe
        self.scribe = None
        if old is not None:
            try:
                await old.close()
            except Exception:  # noqa: BLE001 - the old socket is already lost
                pass
        rt = self._new_session(
            instructions=(
                "You are a silent transcription channel for an English "
                "conversation. Never speak. If you must respond, reply "
                "with a single space."
            ),
            voice=_FAMILY_DEFAULT_VOICE,
            tools=[],
        )
        await rt.connect(open_conversation=False)
        self.scribe = rt
        self._fanned_to_scribe = 0
        self._scribe_heard_speech = False
        # The scribe is no longer lost: a REPAIRED channel that still
        # reads as lost would stop _went_deaf ever reporting the next
        # failure on it (it reports once per channel), and the record
        # would carry one stale reason for a scribe that has since died
        # twice more.
        self.lost.pop(SCRIBE_ID, None)
        self.scribe_reopens += 1
        return rt

    async def hear(self, pcm: bytes, *, exclude: Optional[str] = None) -> None:
        """Everyone in the room hears this audio.

        Participant audio (exclude=None) also reaches the scribe; agent audio
        (exclude=<speaker>) deliberately does not, keeping the scribe's input
        transcription a pure participant channel.

        A channel that has stopped receiving is dropped from the room and named
        in `lost`, instead of being fanned to for the rest of the encounter.
        There are three ways to stop receiving here and this used to see none of
        them. `return_exceptions=True` swallowed the first: a socket the gateway
        tore down raised on send, and the exception went into a list nobody
        read. The other two raise nothing at all, so no exception handling would
        ever have caught them — RealtimeVoiceSession._send returns early when
        the socket is None, and counts-and-drops a ConnectionClosed into
        `send_failures` so that one dead member cannot stop the room hearing.
        Both are the right behaviour for a send. Both also mean a character who
        has been deaf for ten minutes and one who is listening are the same
        object from here unless the socket and that counter are checked, so both
        are checked.

        Dropping the channel is what makes the loss observable to everyone else
        without this class needing a store of its own: session_for() stops
        answering for that member, so the next grant is recorded as a failed
        one, and the runner's boundary check that every wanted member is still
        seated fails and rebuilds the room.
        """
        # Colleague audio (exclude set) is fanned only to members that cannot
        # take text; the others get the line as text via tell() instead,
        # which keeps their turn detection on the participant alone.
        targets = [
            (aid, rt) for aid, rt in self.sessions.items()
            if aid != exclude
            and not (exclude is not None
                     and relays_colleagues_as_text(getattr(rt, "model", "")))
        ]
        if exclude is None and self.scribe is not None:
            targets.append((SCRIBE_ID, self.scribe))
        failures_before = [getattr(rt, "send_failures", 0) for _, rt in targets]
        results = await asyncio.gather(*(
            rt.send_audio(pcm) for _, rt in targets
        ), return_exceptions=True)
        for (channel_id, rt), result, before in zip(
                targets, results, failures_before):
            if not isinstance(result, BaseException):
                if channel_id == SCRIBE_ID:
                    self._fanned_to_scribe += len(pcm)
                    if not self._scribe_heard_speech and pcm:
                        self._scribe_heard_speech = (
                            _frame_rms(pcm) >= self._speech_rms
                        )
                else:
                    self._fanned_since_grant[channel_id] = (
                        self._fanned_since_grant.get(channel_id, 0) + len(pcm)
                    )
            if isinstance(result, BaseException):
                await self._went_deaf(
                    channel_id, rt,
                    f"send_audio raised {type(result).__name__}: {result}",
                )
            elif getattr(rt, "ws", True) is None:
                await self._went_deaf(
                    channel_id, rt,
                    "the websocket is gone, so appending audio silently did "
                    "nothing",
                )
            elif getattr(rt, "send_failures", 0) > before:
                await self._went_deaf(
                    channel_id, rt,
                    "the gateway closed this session's socket: "
                    f"{getattr(rt, 'last_send_error', '') or 'send failed'}",
                )

    async def tell(self, speaker_name: str, text: str, *,
                   exclude: Optional[str] = None) -> None:
        """Give the text-relay members a colleague's FINISHED line, as text.

        The other half of hear()'s family gate. Where a member's row says
        relay_colleagues_as_text, that member is not fanned the colleague's
        audio at all -- on native-audio the fanned audio confused its turn
        detection, and on the gpt route, where server VAD is off, it only
        lands in the member's own input buffer and gets committed as part of
        the member's turn. So the room tells it instead, once, when the line is
        complete.

        A context note, explicitly not a cue to speak: inject_text adds the
        item and asks for nothing. _strip_context_echo in the runner is the
        second line of defence, for the route that parrots it anyway.

        THE COUNTER IS NOT INCIDENTAL. give_floor asks "has this member heard
        anything since its last turn?" (`_fanned_since_grant`) and skips the
        autofire wait when the answer is no, because a session that has heard
        nothing has nothing to answer -- that is the scene-open case. On a
        relay family NOTHING is ever fanned, so without counting a tell() the
        answer would be "no" on every single grant and every grant would take
        the scene-open branch. A told line is something heard.
        """
        if not text:
            return
        note = (f"(Context, not for you to repeat: {speaker_name} just said "
                f"out loud to the group: \"{text}\")")
        targets = [
            (aid, rt) for aid, rt in self.sessions.items()
            if aid != exclude and relays_colleagues_as_text(
                getattr(rt, "model", ""))
        ]
        results = await asyncio.gather(*(
            rt.inject_text(note) for _, rt in targets
        ), return_exceptions=True)
        for (channel_id, _rt), result in zip(targets, results):
            if not isinstance(result, BaseException):
                self._fanned_since_grant[channel_id] = (
                    self._fanned_since_grant.get(channel_id, 0) + len(note)
                )

    # -- the floor ----------------------------------------------------------
    @staticmethod
    def _already_answering(rt) -> bool:
        """True while the gateway is producing this member's reply on its own.

        Read from the session rather than from the family's row, because it is
        the session that knows. On a family whose turn detection is switched off
        the gateway starts nothing we did not ask for, so this never fires and
        every grant runs the documented commit-and-ask path below.
        """
        if not getattr(rt, "autofire_active", False):
            return False
        last = getattr(rt, "_last_output_at", 0.0) or 0.0
        return (time.time() - last) < _AUTOFIRE_WINDOW_S

    @staticmethod
    async def _gateway_answers_on_its_own(rt) -> bool:
        """Wait, briefly, to see whether the commit is answered without asking.

        Both families start a reply off the commit alone (see the
        `conversation_already_has_active_response` branch in
        server/voice/realtime.py), and on Gemini the reply is coming whether or
        not anything was committed. So a response.create sent straight after the
        commit is at best refused — a transient error frame on every single
        group turn, which the participant's page shows as "Something went
        wrong" — and at worst granted, which is the doubled reply: two answers
        in one recorded turn, the first of which never saw the stage direction.

        Ask only if nobody started. This is the same decision _client_to_model
        makes on the 1:1 path, on the same AUTOFIRE_WAIT knob, and it ends the
        instant the first delta arrives rather than spending the ceiling.
        """
        # Per FAMILY, not one number for every route. The gateway fires about
        # 1 s after silence on plain flash and about 3.3 s on native-audio, and
        # a 1.5 s ceiling on the slower route is a wait that always expires --
        # so the room asks for a second reply on top of the one already coming,
        # which is the doubled reply this method exists to prevent.
        # autofire_wait_for_model still reads AUTOFIRE_WAIT first, so the knob
        # this path has always had still overrides everything.
        try:
            limit = _realtime.autofire_wait_for_model(
                getattr(rt, "model", "") or "")
        except (TypeError, ValueError):
            limit = float(_ADOPT_WAIT_DEFAULT)
        deadline = time.time() + max(limit, 0.0)
        while True:
            if GroupRoom._reply_evidently_started(rt):
                return True
            if time.time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    @staticmethod
    def _reply_evidently_started(rt) -> bool:
        """Has this session's gateway actually begun a reply?

        `responding` alone was the test, and `responding` is also what a
        response.create the gateway IGNORED leaves behind: `_response_active`
        latched with nothing ever arriving for it, for RESPONSE_STALL_S. On
        Gemini the scene open used to be exactly that (a commit of pure
        silence + create drew no frame at all), so the lead's NEXT grant —
        the first one the participant had actually spoken into — read the
        latch as "already answering", committed nothing, asked for nothing,
        and the room answered the participant with nothing. Measured
        yesterday in three rooms: zero agent turns.

        So a reply counts as started only on evidence — the gateway's own
        auto-fire, or output having arrived for the reply in flight. A
        session that cannot say (no `_response_saw_output`, as the test
        doubles here) keeps the old reading of `responding`.
        """
        if getattr(rt, "autofire_active", False):
            return True
        if not rt.responding:
            return False
        saw = getattr(rt, "_response_saw_output", None)
        return True if saw is None else bool(saw)

    async def open_scene(self, agent_id: str, *, prompt: str
                         ) -> Optional[RealtimeVoiceSession]:
        """Make one character speak first, on a family where a commit of
        silence will not do it.

        Measured on nto.gemini-live-2.5-flash (2026-09-14): pad + commit +
        response.create on a session that has heard nothing drew NO frame —
        not response.created, not response.done, not an error — in 5 of 5
        rooms and 2 of 2 replays, and the unanswered create left `responding`
        latched (see _reply_evidently_started). A user TEXT item + a
        response.create on the same sessions drew a full, in-character
        opening line 4 of 4 times, 0.5-3.9 s to first audio. So the scene
        opens with a prompt the record shows, exactly as the audio-recovery
        retry revives a dead reply, and nothing is committed.

        The caller has already put the opening direction into this member's
        connect-time brief (the family reads no brief after it); the prompt
        only says "now". Same failure reporting as give_floor: a member whose
        socket is gone reports None rather than success.
        """
        rt = self.sessions.get(agent_id)
        if rt is None:
            return None
        self.speaking = agent_id
        self._fanned_since_grant.pop(agent_id, None)
        failures_before = getattr(rt, "send_failures", 0)
        try:
            if rt.responding and not self._reply_evidently_started(rt):
                rt.clear_response_state()
            await rt.prompt_response(prompt)
        except Exception:  # noqa: BLE001, a dead session must not kill the turn
            self.sessions.pop(agent_id, None)
            return None
        if (getattr(rt, "ws", True) is None
                or getattr(rt, "send_failures", 0) > failures_before):
            await self._went_deaf(
                agent_id, rt,
                "the scene was opened on a session whose socket is gone: "
                f"{getattr(rt, 'last_send_error', '') or 'send failed'}",
            )
            return None
        return rt

    async def close_participant_turn(self) -> bool:
        """Close the participant's turn on the transcription channel.

        The scribe is the room's only participant channel — _pump_member throws
        its own user_transcript events away, deliberately, because a member's
        input buffer also carries the other characters' fanned-out audio and the
        bridge labels all of it "user". So whatever the scribe does not
        transcribe, the encounter does not have.

        And on a family whose own turn detection is switched off, the scribe
        transcribes nothing, because a buffer is only transcribed when it is
        committed and nothing here was committing it. give_floor commits the
        member it is granting and no one else. Reproduced against the measured
        gateway: after a participant utterance and one floor grant on
        gpt-realtime-2.1, the scribe held 24000 bytes of the participant's audio,
        had been committed zero times and had produced zero
        `input_audio_transcription.completed` frames — a group encounter
        recording a perfect participant WAV, a perfect agent transcript, and not
        one word the participant said. On the configured Gemini family the same
        room transcribes fine, because that gateway's VAD closes the buffer
        itself; the bug is invisible until REALTIME_MODEL moves, which is the
        move that also makes stage directions start arriving.

        WHAT THIS COSTS, said plainly because it is the reason it looks
        optional. The commit is what produces a reply, on both families (see
        server/voice/realtime.py: a commit with no `response.create` at all drew
        a full spoken reply from both), and the scribe's brief does not stop it
        — both families accepted "Never speak" and answered anyway. So closing
        the participant's turn here buys the transcript at the price of one
        generated-and-discarded scribe reply per turn, which _pump_scribe
        cancels before the participant hears any of it. That is a family where
        the room currently generates exactly one reply per participant turn
        going to two. It is the right trade and it is not a free one: an
        encounter with no participant transcript is not an encounter, and a
        second short reply on a channel briefed to answer with a single space is
        the cheapest thing in the room.

        No `response.create` is sent either way; the reply is the gateway's
        idea, and asking for a second one on top is what glues two answers into
        one recorded turn.

        And it does not commit a buffer this room has put nothing in: an empty
        commit is `input_audio_buffer_commit_empty` on gpt and no turn at all on
        Gemini, and a commit holding only the silence pad would hand the record
        a transcript of nothing, attributed to the participant.

        THAT SECOND HALF USED TO BE A CLAIM AND NOT A CHECK. The only test was
        the byte counter, and the participant's page streams continuously: 20 ms
        frames of quiet go to the scribe the whole time nobody is talking, so
        the counter is non-zero again within 20 ms of any close and this method
        would have committed a buffer of pure silence on request, for the rest
        of the encounter. Nothing reached that today — the one caller is the
        runner's `turn_ended`, which by definition follows speech — but the
        guard a future caller would be relying on was not there, and what it
        guards against is the worst outcome available here: a transcription
        model handed several seconds of quiet does not return nothing, it
        invents, and an invented participant turn is indistinguishable from a
        real one everywhere downstream. So the room now also asks whether any
        of what it fanned was loud enough to be somebody speaking, on the same
        RMS threshold the runner's own turn detection uses.

        Returns True when a commit went out, so a caller can tell "closed the
        turn" from "there was no turn to close" — and, now, from "there was
        nothing but quiet to close".

        WHO CALLS THIS, AND WHY NOT give_floor. The room cannot find the
        participant's turn boundary on its own: it is handed audio and never
        told when the talking stopped. The runner is told — `mark ==
        "turn_ended"` in _client_to_model, the same mark that spawns
        _run_group_turn — and that is the one right moment, because
        _run_group_turn then spends up to ROUTE_TRANSCRIPT_WAIT seconds waiting
        for this exact transcript before it routes, and _named_in reads it to
        decide whether the participant addressed somebody by name. One await
        here, before that spawn, and the director sees the turn it is routing.

        give_floor looks like the convenient place and is the wrong one twice
        over. It runs AFTER routing, so the transcript would land a full turn
        late and the director would go on routing blind; and it does not run at
        all on a turn the director answers with silence. It would also close the
        participant's turn around a moment they have already been answered in.
        """
        rt = self.scribe
        if (rt is None or not self.floor_is_real
                or self._fanned_to_scribe <= 0
                or not self._scribe_heard_speech):
            return False
        self._fanned_to_scribe = 0
        self._scribe_heard_speech = False
        failures_before = getattr(rt, "send_failures", 0)
        try:
            # The same 300 ms of silence give_floor appends, for the same
            # reason: the gateway's turn detection is off, so the buffer holds
            # exactly what was appended and a turn that ends on the
            # participant's last syllable transcribes worse than one that ends
            # on a beat of quiet.
            await rt.send_audio(_SILENCE_PAD)
            await rt.commit_input()
        except Exception as exc:  # noqa: BLE001 - a lost scribe must not cost the turn
            await self._went_deaf(
                SCRIBE_ID, rt,
                f"closing the participant's turn raised {type(exc).__name__}: {exc}",
            )
            return False
        if (getattr(rt, "ws", True) is None
                or getattr(rt, "send_failures", 0) > failures_before):
            # _send counts a closed socket and drops the frame rather than
            # raising, so silence here is not success. Unreported, the room
            # would go on believing it had a participant channel.
            await self._went_deaf(
                SCRIBE_ID, rt,
                "the participant's turn was closed on a scribe whose socket is "
                f"gone: {getattr(rt, 'last_send_error', '') or 'send failed'}",
            )
            return False
        self.scribe_commits += 1
        return True

    async def give_floor(self, agent_id: str) -> Optional[RealtimeVoiceSession]:
        """Give one character the floor.

        What "the floor" means depends on the family, and the honest version is
        worth stating, because the version this docstring used to carry was
        wishful. It said the members without the floor "keep the turn in their
        (uncommitted) buffer". They do not, on either family as it was shipped:
        the gateway answers on every open session after speech plus silence, so
        a three-member room generates three replies to one participant turn and
        the runner's per-member pump throws two of them away after they have
        been produced and paid for.

        Where the family's turn detection is switched off — see `floor_is_real`,
        and RealtimeVoiceSession._session_payload, which sends the key — that
        stops being wishful. Live on gpt-realtime-2.1, the two members that were
        not committed emitted no response at all, and the append -> commit ->
        response.create below is then the whole mechanism, working as the
        platform documents it.

        Where it is not switched off (Gemini, whose row says the key changes
        nothing there), the room does the one thing left to it, which is not to
        make the waste worse. `response.cancel` is inert on that family — a
        cancelled reply went on to deliver 44 more audio deltas — so cancelling
        the other two would be a gesture, and this deliberately does not perform
        one. What it does instead is refuse to ask for a SECOND reply from the
        character whose reply the gateway is already producing. That request is
        what glued two answers into one recorded turn ("...back on track.It's a
        tough situation but"), the first of which had never seen the stage
        direction.

        The question is asked before the commit and again after it, because in a
        live room the answer changes in between and the two moments cost
        different things.

        Before: on a family that answers by itself, the reply usually has NOT
        begun by the time the floor is granted — measured at 1.2 s after the
        participant stopped, which is a realistic director latency — so a bare
        "is it already talking?" check catches nothing, and the room waits up to
        AUTOFIRE_WAIT for it. That wait has to happen BEFORE the commit, because
        on Gemini it is the COMMIT that produces the second reply: with the
        wait after it, the grant still recorded two response.dones and the
        transcript still read "...prevent it in the future.I think it's a
        concern and we need to". With it before, the room commits nothing and
        the character speaks once. Skipped where the floor is real, since that
        gateway starts nothing unasked; and skipped for a member this room has
        fanned no audio to since its last turn, because there is nothing there
        for the gateway to answer — that is the scene open, where the whole
        point is to make a session that has heard nothing speak, and it should
        not first spend a second and a half waiting for it not to.

        After: for the same window, because the commit itself starts a reply on
        both families (see the `conversation_already_has_active_response`
        branch in server/voice/realtime.py). Asking again behind it put a
        transient error frame on every gpt group turn, which the participant's
        page shows as "Something went wrong".

        Both waits end the moment the first delta arrives, and both are the same
        decision _client_to_model already makes on the 1:1 path, on the same
        knob.

        The silence pad is unconditional now. It used to be skipped whenever
        `pending_input` looked large, and pending_input counts only what WE
        appended: the gateway's own commit consumes the buffer without resetting
        it, so the counter read "full" on precisely the grant whose buffer was
        empty — which is the grant that needed the pad. Padding a buffer that
        does hold audio costs 300 ms of trailing silence and nothing else.

        A grant to a member whose socket has gone reports failure rather than
        success. It cannot rely on an exception to tell it so: _send counts a
        closed socket and drops the frame, deliberately, so that one dead member
        cannot stop a room. Without the check below the runner would be told the
        floor was granted, and would then wait out the full 45 s turn timeout
        listening to a session that never received the request.
        """
        rt = self.sessions.get(agent_id)
        if rt is None:
            return None
        self.speaking = agent_id
        failures_before = getattr(rt, "send_failures", 0)
        heard_something = self._fanned_since_grant.pop(agent_id, 0) > 0
        model = getattr(rt, "model", "") or self._model or _configured_model()
        try:
            if (self._already_answering(rt) and not grants_via_text_prompt(model)) or (
                    not self.floor_is_real and heard_something
                    and not grants_via_text_prompt(model)
                    and await self._gateway_answers_on_its_own(rt)):
                # This turn is already being answered. Touch nothing: on a
                # family that answers by itself the COMMIT is what produces the
                # second reply, so a grant that commits here has already done
                # the damage whether or not it goes on to ask.
                #
                # Not on a text-grant family, though: there the reply the
                # gateway started for itself is the one it DROPS, so waiting
                # for it and then returning is how a member goes silent for a
                # whole turn. That route gets the text nudge below instead,
                # which is new content and draws a new reply.
                #
                # BOTH disjuncts carry that exclusion, and the first one only
                # gained it on 2026-09-15. It did not have it, and the cost was
                # exactly what the paragraph above describes: driven live on
                # nto.gemini-live-2.5-flash-native-audio with a member sitting
                # in one of the empty responses that route fires for itself
                # (response.created, no delta, so the pump never begins a hold),
                # a grant took 11.00 s, logged `fresh_reply_requested`, and put
                # ZERO frames on the wire — the member was silent for the whole
                # turn, and only a reply older than _AUTOFIRE_WINDOW_S (15 s)
                # escaped it. origin/main documents empty responses as common on
                # that route, and that route is what production runs, so this
                # was not a corner case.
                self.autofire_grants += 1
                return rt
            if grants_via_text_prompt(model):
                # origin/main 169310c, measured on the deployed native-audio
                # route. Pad-and-commit yields an EMPTY response here: the
                # route has already consumed the participant's audio with a
                # reply of its own that was dropped, so the buffer this would
                # commit holds only the padding. It does answer a text item.
                #
                # This is open_scene's recipe, and deliberately the same one:
                # a text item is new CONTENT for the model to answer, which a
                # commit of silence is not. The two paths agree because they
                # are the same finding on two families.
                #
                # A reply still latched from a lost response.done would make
                # request_response send nothing at all, so it is cleared first,
                # exactly as the commit path below does.
                if rt.responding and not self._reply_evidently_started(rt):
                    rt.clear_response_state()
                await rt.inject_text(_TEXT_GRANT_NUDGE)
                await rt.request_response()
            else:
                await rt.send_audio(_SILENCE_PAD)
                # A prior reply whose response.done was lost leaves
                # _response_active stuck True; request_response() would then
                # silently send nothing and this character would be muted for
                # the rest of the encounter. Cleared BEFORE the commit, so that
                # the wait below is measuring this turn's reply and not the
                # ghost of the last one.
                if rt.responding:
                    rt.clear_response_state()
                await rt.commit_input()
                if await self._gateway_answers_on_its_own(rt):
                    self.autofire_grants += 1
                else:
                    # Nobody started. On the gpt route that is the case
                    # origin/main 210fbfc is about: the COMMIT itself starts
                    # the reply there, so a commit that produced nothing has
                    # still left _response_active latched with no reply behind
                    # it -- and request_response() sends nothing while that
                    # flag is up, which mutes this member for the rest of the
                    # encounter. Put it down, then ask.
                    #
                    # Deliberately NOT before the probe. Cleared there it
                    # erases the very evidence the probe reads, so every gpt
                    # grant asks for a second reply on top of the one the
                    # commit already started -- which is the doubled turn this
                    # whole path exists to prevent (measured: priya spoke twice
                    # on every grant).
                    if is_openai_realtime(model) and rt.responding:
                        rt.clear_response_state()
                    await rt.request_response()
        except Exception:  # noqa: BLE001, a dead session must not kill the turn
            self.sessions.pop(agent_id, None)
            return None
        if (getattr(rt, "ws", True) is None
                or getattr(rt, "send_failures", 0) > failures_before):
            await self._went_deaf(
                agent_id, rt,
                "the floor was granted to a session whose socket is gone: "
                f"{getattr(rt, 'last_send_error', '') or 'send failed'}",
            )
            return None
        return rt

    async def rebrief(
        self, instructions_for: Optional[Callable[[object], str]] = None
    ) -> None:
        """Re-issue every member's brief WITHOUT rebuilding the room.

        Two consecutive interactions can share a cast: S4's working session and
        then its close are one continuous meeting with the same three
        colleagues. Closing the room between them and reopening it replaces
        people who heard the working session with fresh sessions that never did,
        and a fresh session cannot be given that history. (Not for the reason
        this docstring used to give: a text conversation item does NOT close the
        socket — measured on both families, and false; the model reads the item
        and answers it. The reason is that a transcript replayed as text is not
        the same thing as having heard the room, and no character should open a
        closing conversation by reading a summary of the meeting they are
        supposed to remember.) So the close would be played by colleagues with
        no memory of what they are closing.

        This is NOT the mid-stream re-brief the module docstring warns about.
        That warning is about changing WHO a session is playing while a
        conversation is under way; here every session keeps the character it was
        opened as, and only the scene framing changes. It also runs between
        turns with the floor ungranted, and any reply still in flight is
        cancelled first, so no session.update can land mid-response and mute a
        member.

        What each member's update actually did is kept in `last_rebrief`, per
        member: True acked, False refused or never sent, None unknowable, or the
        exception. On a family whose row says mid-session session.update is not
        honoured that is False for everyone, every time — which is the truth
        about re-briefing there, and the record should be able to say so rather
        than inheriting a gather() that dropped the answer on the floor.
        """
        build = instructions_for or self._instructions_for
        # Leave the floor ungranted: with nobody holding it the runner's pumps
        # suppress unsolicited replies, which is the correct resting state
        # between turns and the state give_floor expects to start from.
        self.speaking = None

        async def one(agent):
            rt = self.sessions.get(agent.id)
            if rt is None:
                return None
            await rt.cancel_response()
            return await rt.update_instructions(build(agent))

        # return_exceptions: one member whose socket has died must not stop the
        # rest of the room from being re-briefed for the new interaction.
        results = await asyncio.gather(
            *(one(a) for a in self.agents), return_exceptions=True
        )
        self.last_rebrief = {
            a.id: r for a, r in zip(self.agents, results)
            if a.id in self.sessions
        }

    def session_for(self, agent_id: str) -> Optional[RealtimeVoiceSession]:
        return self.sessions.get(agent_id)

    async def close(self) -> None:
        closers = list(self.sessions.values())
        if self.scribe is not None:
            closers.append(self.scribe)
        await asyncio.gather(*(
            rt.close() for rt in closers
        ), return_exceptions=True)
        self.sessions.clear()
        self.scribe = None
