"""Speech-to-speech over the Cornell LiteLLM gateway.

Replaces the v1 cascade (Deepgram STT -> text LLM -> ElevenLabs TTS) with a
single realtime session: participant audio goes up, agent audio and both
transcripts come back.

The gateway serves two families of realtime model, and they do not behave the
same. Which one this process talks to is decided by REALTIME_MODEL alone, and
everything that follows from that choice is written down once, in
`REALTIME_FAMILIES` below, rather than scattered through this file as tests on
the model name. Read that table to find out what a family needs; read
`capabilities_for()` to find out which family a model name belongs to.

Gateway notes, verified 2026-08-19 (see docs/migration-plan.md) and re-probed
live on 2026-09-10, one short socket per case:

* Transport is a WebSocket at /v1/realtime?model=...; the WebRTC path is not
  wired up. The upgrade needs HTTP/1.1.
* `session.update` must stay FLAT: instructions, voice, tools, and the two
  per-family keys the table names. Sending `modalities` or the nested GA
  `audio: {...}` block leaves the session alive but permanently mute, with no
  error event. This is the single easiest way to break it, and the reason
  nothing goes into that dict that a family has not been shown to need.
* The commit is what produces a reply, on BOTH families. `response.create` on
  top of it is belt and braces and sometimes collides with the bridge's own
  (see `conversation_already_has_active_response` in `events`). What no family
  does is reply to appended audio alone, so turn detection still lives here, in
  `SilenceDetector`. (The 2026-08-19 note said the commit was inert without an
  explicit `response.create`; on 2026-09-10 a commit with no create at all drew
  a full spoken reply from both families, so that half of it no longer holds.)
* Gemini transcribes the participant without being asked. The gpt family does
  not: without `input_audio_transcription` in the session dict it returns no
  participant transcript at all, which is the study's primary measurement
  channel going quietly missing.
* A voice the model does not know is the worst failure in here, because on one
  family it is invisible. gpt answers it with an `invalid_value` error and then
  discards the WHOLE `session.update` — the actor keeps talking, fluently, as
  the gateway's stock assistant rather than as the character. Gemini answers
  an unknown voice with nothing whatsoever: no `session.updated`, no error, no
  audio, no transcript, a socket that is simply silent for the rest of its
  life. Hence `connect()` refuses a voice that is not on the family's roster
  before it opens the socket, and an `invalid_value` on the voice is fatal.

Because every one of those failure modes is quiet, `events()` holds one rule of
its own: it never ends, and never stops answering, without saying so. A reply
that produces nothing at all is timed out and reported; a reply the gateway
abandons mid-sentence (a stall, an `error` frame, or the socket going away) is
closed out with an `interrupted` `response_done` and then reported; and ANY
other exception out of the loop — a malformed frame, a bug in here — is caught
and reported the same way rather than escaping as a traceback only the server
log would see. The single exception is a socket we closed ourselves, which is
an ordinary end of the pump. A character that has gone silent must leave a mark
an analyst can find, because an encounter that quietly stopped and one that
concluded look identical in the record otherwise.

That contract is now honoured downstream too. The `interrupted` flag on a
synthetic `response_done` is read by both of the runner's finalize spawn sites
(`_pump`/`_pump_member` in server/realtime_voice_session.py) and reaches the
record on the turn itself, as `assistant_turn.interrupted` and on the steering
pair, so a rater comparing a stage direction to the line it produced can tell a
truncated delivery from a bad one. It also clamps that turn's transcript grace
to 1 s, which is all a dead session's transcript is worth waiting for. The
terminal `error` message that follows every truncation still names the
truncation in words: it is the frame the participant's page acts on, and it is
what an analyst reading events.jsonl alone will find first.

`error` events carry `transient: True` when they describe a fault the session
survived intact — one discarded audio chunk, not a lost turn. The runner still
records every one of them, and shows the participant at most one per reply: the
frame reaches the page as "Something went wrong", and audio deltas arrive every
few tens of milliseconds, so a gateway sending a run of malformed frames would
otherwise bury the participant in identical banners mid-conversation.

The same refusal to claim more than happened governs steering.
`update_instructions()` used to return the moment `ws.send()` did, and every
caller wrote "delivered" into the record on the strength of it. On Gemini that
sentence was false: mid-session `session.update` frames are neither
acknowledged nor obeyed there, so a stage direction reached a socket and
nothing beyond it while the steering log filled up with deliveries. It now
returns True only when a `session.updated` frame actually came back, False when
the platform was asked and did not answer, and None when this bridge is in no
position to tell — nobody is draining `events()`, which is the only place an
ack can be seen. Three values, because "not delivered" and "not checked" are
different findings and an analyst has to be able to tell them apart.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import re
import struct
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

import websockets

# Config comes from server.llm so the .env file wins over ambient environment:
# a stray exported variable must not be able to redirect study traffic.
from ..llm import gateway_api_key, gateway_base_url, setting
from .turn_audio import shortfall as _audio_shortfall, word_count as _word_count

try:  # audioop was removed in Python 3.13 (PEP 594); fall back to pure Python.
    import audioop  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.13+
    audioop = None


def _rms(pcm: bytes) -> int:
    """Root-mean-square of signed 16-bit mono PCM."""
    if audioop is not None:
        return audioop.rms(pcm, 2)
    n = len(pcm) // 2
    if n == 0:
        return 0
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    return int(math.sqrt(sum(s * s for s in samples) / n))


def _ratecv(pcm: bytes, inrate: int, outrate: int, state):
    """Resample signed 16-bit mono PCM, carrying interpolation state between
    chunks. Mirrors the audioop.ratecv contract we rely on; the pure-Python
    path is a streaming linear interpolator with its own opaque state tuple."""
    if audioop is not None:
        return audioop.ratecv(pcm, 2, 1, inrate, outrate, state)
    n = len(pcm) // 2
    if n == 0:
        return b"", state
    samples = struct.unpack("<%dh" % n, pcm[: n * 2])
    if state is None:
        prev, pos = samples[0], 0.0
    else:
        prev, pos = state
    combined = (prev,) + samples  # index 0 == previous chunk's last sample
    step = inrate / outrate
    out = []
    while pos < n:
        i = int(pos)
        frac = pos - i
        a = combined[i]
        b = combined[i + 1]
        val = int(a + (b - a) * frac)
        if val > 32767:
            val = 32767
        elif val < -32768:
            val = -32768
        out.append(val)
        pos += step
    new_state = (samples[-1], pos - n)
    return struct.pack("<%dh" % len(out), *out), new_state

GATEWAY = gateway_base_url()
MODEL = setting("REALTIME_MODEL", "nto.gemini-live-2.5-flash")
# Deliberately no default. "Puck" used to stand here, which made one family's
# voice the answer for both: point REALTIME_MODEL at a gpt model and every
# session would open with a voice that family rejects, and a rejected voice
# takes the character brief down with it. Empty means "whatever this model's
# own roster starts with", which is the only answer that is right either way.
VOICE = setting("REALTIME_VOICE", "")

# "Nobody set this" for RealtimeVoiceSession.turn_detection, distinct from
# None, which is a value the gpt family actually sends.
_UNSET = object()

# The transcriber's placeholder for speech it could not transcribe; see the
# input_audio_transcription branch of events().
_PLACEHOLDER = re.compile(r"\{\s*\}")


# ── what each family of realtime model needs ───────────────────────────────
#
# One row per family, and one place to read. Everything in a row was observed
# on the Cornell gateway on 2026-09-10, one short socket per claim; nothing in
# it is inferred from documentation. The fields are the four questions that
# actually decide whether an encounter works:
#
#   voices                     what the gateway will accept, in the order
#                              characters are cast into. [0] is the default.
#   needs_input_transcription  whether the participant is transcribed only if
#                              the session dict asks for it.
#   needs_turn_detection_null  whether server VAD has to be switched off for a
#                              participant turn to arrive in one piece.
#   honours_session_update     whether a mid-session session.update — the one
#                              and only route every stage direction takes — is
#                              acknowledged and obeyed.
#
# The evidence, per row, is in the row.


# The browser captures and plays 16 kHz; the gateway emits 24 kHz PCM16.
# Defined ABOVE the capability table because a row names an input rate: the
# native-audio route needs 24 kHz in and every other route takes this.
CLIENT_RATE = 16000
GATEWAY_OUTPUT_RATE = int(setting("REALTIME_OUTPUT_RATE", "24000"))


class UnknownRealtimeModel(RuntimeError):
    """REALTIME_MODEL names a model no row in the table covers."""


class UnsupportedVoice(ValueError):
    """A voice this model's family will not accept, caught before connecting."""


@dataclass(frozen=True)
class RealtimeCapabilities:
    """One family's row. Frozen: a row is a finding, not a setting."""

    family: str
    voices: tuple
    needs_input_transcription: bool
    input_transcription_model: str
    needs_turn_detection_null: bool
    honours_session_update: bool
    # The connect-time `turn_detection` dict this family was MEASURED to honour
    # for end-of-turn silence, or None where the family gets `turn_detection:
    # null` (or nothing) instead. Applied by the runner and the room to the
    # sessions they open (see RealtimeVoiceSession.turn_detection); a bare
    # session built by nobody in particular still sends the bare payload, so
    # nothing about the two rows above is changed by this column.
    end_of_turn: Optional[dict] = None
    # The runner's OWN end-of-turn silence has to be at least this long on
    # this family, or the runner commits the participant's turn inside the
    # pause the gateway was told to wait through and splits it anyway. 0 means
    # the runner's VAD_SILENCE_MS stands.
    end_of_turn_silence_ms: int = 0

    # ── the columns that came in from origin/main ──────────────────────────
    #
    # Everything below was five separate substring tests on the model name on
    # that branch (input_rate_for_model, autofire_wait_for_model,
    # accepts_text_items, is_openai_realtime, and an `if "native-audio" in
    # model` in give_floor). They are columns here for the reason the rest of
    # this table exists: a model's behaviour should be written down in one
    # place, and adding a model should be adding a row.

    # PCM16 sample rate the bridge expects on the INPUT side. The browser
    # always captures CLIENT_RATE; RealtimeVoiceSession.send_audio resamples
    # when this differs. 24 kHz on native-audio is not a preference: at 16 kHz
    # that route accepts the session and then stays silent forever — no
    # transcription, no reply, no error (2026-09-08, after nine other config
    # variants were tried first).
    input_rate: int = CLIENT_RATE
    # Seconds to let the bridge start its own reply before asking for one.
    # ~1 s on plain flash, ~3.3 s on native-audio; asking early collides.
    autofire_wait: float = 1.5
    # Whether conversation.item.create with text is accepted on this route.
    accepts_text_items: bool = True
    # Whether a room should relay a colleague's finished line to this member as
    # TEXT instead of fanning that colleague's audio into its input.
    relay_colleagues_as_text: bool = False
    # Whether give_floor hands this member the floor by injecting a text nudge
    # and asking, instead of padding and committing its audio buffer.
    grant_via_text_prompt: bool = False
    # Whether a ROOM MEMBER on this route may be given tools at all.
    member_tools: bool = True
    # Whether `response.created` on its own is proof the bridge started a reply.
    autofire_at_created: bool = False
    # Whether the session dict may carry a transcription language hint.
    transcription_language_hint: bool = True
    # Voices from ANOTHER family that this family will play instead. The
    # scenario bank names Gemini voices; a bank entry is not a typo, and a
    # rejected voice takes the character brief down with it.
    voice_aliases: dict = field(default_factory=dict, compare=False)
    # Which family's column in a scenario's `realtime_voice` map this row casts
    # from. Empty means "its own name". The native-audio row sets it to
    # `gemini-live`: it has the SAME roster, so a character cast as Kore is
    # Kore on both, and asking the bank to write a third identical column for
    # every character in twelve scenarios would be twelve files of duplicated
    # data with twelve chances to disagree with itself. A family that needs its
    # own casting leaves this empty and gets its own column.
    casting_family: str = ""

    @property
    def casting_key(self) -> str:
        return self.casting_family or self.family

    @property
    def default_voice(self) -> str:
        return self.voices[0]

    def accepts_voice(self, voice: str) -> bool:
        return voice in self.voices


REALTIME_FAMILIES = {
    "gemini-live": RealtimeCapabilities(
        family="gemini-live",
        # All eight probed on nto.gemini-live-2.5-flash: session.updated acked
        # and audio came back for every one. The first five are the historical
        # casting order and stay put, so a re-run of an earlier encounter hears
        # the same characters.
        voices=("Puck", "Charon", "Kore", "Fenrir", "Aoede",
                "Leda", "Orus", "Zephyr"),
        # Arrives unasked: the probe never sent the key and still received
        # `conversation.item.input_audio_transcription.completed` with the
        # participant's sentence in it. Asking anyway is a needless risk on the
        # family whose failure mode is a silent mute (see the docstring).
        needs_input_transcription=False,
        input_transcription_model="",
        # Not needed: one appended utterance, one commit, one transcript, one
        # reply. Nothing arrives early and nothing is split.
        needs_turn_detection_null=False,
        # THE finding of this round. Three mid-session session.update frames
        # over two sockets, zero session.updated frames back, no error either;
        # and the actor went on ignoring a direction ("your entire next reply
        # must be the single word banana") that the same frame made a gpt actor
        # obey. The session.update sent at CONNECT is acked, in ~125-250 ms, so
        # the persona does land — it is only re-briefing that does not.
        honours_session_update=False,
        # THE PAUSE SPLIT (2026-09-14, live, hesitant synthetic participant on
        # nto.gemini-live-2.5-flash). A lost participant pauses 700-1300 ms
        # mid-thought; the gateway's default turn detection closes the turn
        # inside that pause (a 600 ms break did not split, 900 ms did), so "I
        # need more context. [900 ms] What do you mean?" arrived as two
        # transcripts and drew two replies, and a trailing-off tail was
        # dropped outright. Connect-time `turn_detection` probed, one short
        # socket per claim (scratchpad PAUSE/out/probe_accept*.json):
        #   server_vad silence 1500 / prefix 300  acked; reply 2.51 s after
        #                                         speech end vs 1.27 s bare —
        #                                         i.e. the window is HONOURED
        #   server_vad silence 2000 / prefix 500  acked; 2.99 s
        #   server_vad silence 3000 / prefix 500  NO session.updated at all:
        #                                         the silent-mute shape
        #   server_vad create_response false      socket closed 1006
        #   type none / semantic_vad              acked, and still auto-fired
        #                                         at ~1.7 s: not honoured
        # 1500 ms is the longest window that leaves the reply latency
        # tolerable and is comfortably past the measured pauses; the runner's
        # own VAD is raised to match (end_of_turn_silence_ms), because with
        # the gateway at 1500 and the runner still committing at 900 the
        # runner became the thing that split the turn.
        end_of_turn={"type": "server_vad", "silence_duration_ms": 1500,
                     "prefix_padding_ms": 300},
        end_of_turn_silence_ms=1500,
        # 16 kHz straight through; this is the route the browser's own capture
        # rate was chosen for.
        input_rate=CLIENT_RATE,
        # ~1 s after silence (origin/main 210fbfc, and consistent with our own
        # give_floor measurements).
        autofire_wait=1.5,
        # Re-probed 2026-09-10 and 2026-09-14 on a FLAT session config: a
        # user-role text conversation.item.create + response.create is accepted
        # and answered, first delta 0.23 s, complete reply. The older finding
        # that a text item closes this socket with 1006 was the 2026-08-19
        # over-specified session config, not the item. Our audio-recovery retry
        # (retry_response) and our group scene-open (open_scene) both ride on
        # this being True; do not flip it without re-probing.
        accepts_text_items=True,
        # But colleague audio still goes in as AUDIO here. Our fan-out byte
        # counters (_fanned_since_grant) and give_floor's `heard_something`
        # were measured on this route with the full audio fan-out, and this is
        # the route every one of our group measurements was taken on.
        relay_colleagues_as_text=False,
        # Pad-and-commit is what produces the reply here; on this route it is
        # the COMMIT that draws the second reply, which is why give_floor waits
        # AUTOFIRE_WAIT before it.
        grant_via_text_prompt=False,
        # END_SEGMENT_TOOL stays wired: measured working on this route, and it
        # is how an actor ending a group conversation advances the encounter.
        member_tools=True,
        # A created that never becomes a delta does happen here, and a latched
        # autofire_active mutes the encounter permanently. The delta is the
        # proof on this route.
        autofire_at_created=False,
        # origin/main cabc1dd, verified 2026-09-08 not to mute this route. It
        # is the fix for a transcriber that returned a Russian word and
        # Japanese syllables from an English-speaking participant.
        transcription_language_hint=True,
    ),
    "gemini-live-native-audio": RealtimeCapabilities(
        family="gemini-live-native-audio",
        # THE ROUTE PRODUCTION IS ACTUALLY RUNNING (image df1ab83,
        # actor_model = nto.gemini-live-2.5-flash-native-audio). Everything in
        # this row comes from origin/main's 2026-09-08 work on the deployed
        # service, except where the comment says otherwise. It is a row of its
        # own and not a variant of the gemini-live row because six of the
        # thirteen columns differ, and because without a row of its own
        # family_of() folds it into gemini-live and feeds it 16 kHz -- which is
        # the documented permanent-silence failure.
        #
        # Voices: the same roster, inherited. The native-audio route was
        # verified for 1:1 and group rooms on 5a45420 with the bank's Gemini
        # voices; nothing reported a rejection.
        voices=("Puck", "Charon", "Kore", "Fenrir", "Aoede",
                "Leda", "Orus", "Zephyr"),
        # Not probed separately. Carried over from the gemini-live row, whose
        # behaviour it shares in every respect that WAS probed; the language
        # hint below is what actually makes the transcript arrive in English.
        needs_input_transcription=False,
        input_transcription_model="",
        needs_turn_detection_null=False,
        # NOT PROBED on this route. Carried over as False from gemini-live,
        # which is the conservative answer: False makes steering_is_real false,
        # so the runner records that a stage direction was not acknowledged
        # rather than claiming one was. If someone probes mid-session
        # session.update here and it is acked and obeyed, flip it and say so.
        honours_session_update=False,
        # NOT PROBED on this route. Left at None -- i.e. the runner's own
        # VAD_SILENCE_MS stands, which is the behaviour this route had in
        # production. Setting the gemini-live row's 1500 ms window here would
        # be asserting a probe that was not run, and on THIS family an
        # unhonoured turn_detection is the silent-mute shape.
        end_of_turn=None,
        end_of_turn_silence_ms=0,
        # 24 kHz IN, and this is not a preference. At 16 kHz the route accepts
        # the session and then stays silent forever: no transcription, no
        # reply, no error. Found 2026-09-08 after nine config variants;
        # 24 kHz fixed it immediately. Output is 24 kHz too, confirmed by
        # pitch 179 Hz vs 180 Hz.
        input_rate=24000,
        # Auto-fires ~3.3 s after silence here, against ~1 s on plain flash.
        # 4.5 s is the wait that stops a grant colliding with a reply the
        # bridge had already started.
        #
        # NOT PROBED ON THIS ROUTE: the three audio-recovery bars next door --
        # RESPONSE_STALL_S (45 s), AUDIO_ABSENT_S (8 s) and REPLAY_UNANSWERED_S
        # (4 s). Every one of them was calibrated on plain flash (190 replies,
        # 502 closed replies, six waves) and none of that was re-run here. Two
        # of the three are conservative on any route, so they stand as they are.
        # The third was not: 4 s is less than the 4.5 s directly above, i.e. the
        # replay path would have called a turn lost before this route's own
        # reply is due. _absent_bar floors it by this column for that reason.
        # If this route is what Phase 1 runs, these bars want re-measuring on it.
        autofire_wait=4.5,
        # Accepted -- and the room depends on it (see relay_colleagues_as_text
        # and grant_via_text_prompt). Both sides agree text items work here.
        accepts_text_items=True,
        # Fanning a colleague's audio into a native-audio member confused its
        # turn detection, so the room TELLS it what the colleague said instead,
        # as a parenthesised context note. Measured on the deployed route;
        # this is why GroupRoom.tell exists.
        relay_colleagues_as_text=True,
        # Pad-and-commit yields an EMPTY response here: the route has already
        # consumed the audio with a reply of its own that was dropped. A text
        # nudge plus request_response is the only recipe that wakes it -- the
        # same recipe as open_scene.
        grant_via_text_prompt=True,
        # No tools for room members: this route calls end_conversation
        # constantly and every call is an empty turn. END_SEGMENT_TOOL is
        # therefore NOT available to members here, and a group segment on this
        # route ends the way it did before the tool existed (the director's
        # turn budget and the participant's own exit), not by an actor calling
        # it. That is a real capability loss on the deployed route and it is
        # recorded here rather than hidden: see the per-file record.
        member_tools=False,
        # It emits many empty responses, and a member still generating when the
        # participant speaks never answers the new turn, so knowing a reply has
        # started as early as possible is what the stale-hold cancel needs.
        autofire_at_created=True,
        transcription_language_hint=True,
        # Same roster, same characters, same voices: the scenario bank's
        # `gemini-live` casting IS this family's casting. See casting_family.
        casting_family="gemini-live",
    ),
    "gpt-realtime": RealtimeCapabilities(
        family="gpt-realtime",
        # Verbatim from the gateway's own rejection of "Puck", and spot-checked
        # live on cedar, marin, verse and alloy.
        voices=("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer",
                "verse", "marin", "cedar"),
        # Without this key: no participant transcript of any kind, on a session
        # that is otherwise perfectly healthy. With {"model": "whisper-1"}:
        # `conversation.item.input_audio_transcription.completed` on every
        # turn. The study measures the participant; this key is the channel.
        needs_input_transcription=True,
        input_transcription_model="whisper-1",
        # Server VAD does not merely fire early here, it CUTS THE TURN UP. The
        # same 4.7 s utterance arrived as two participant transcripts ("Hello."
        # / "I want to talk about the missed deadline last week."), an empty
        # response, and two collided response.creates. With turn_detection
        # null: one transcript, one reply.
        needs_turn_detection_null=True,
        # Acked in 381 ms at connect and 38 ms mid-session, and the actor obeyed
        # the direction on its very next reply.
        honours_session_update=True,
        # Server VAD is OFF here (needs_turn_detection_null) and the runner's
        # own detector is the only turn close, so there is no gateway window
        # to match and the gpt path is left exactly as it was.
        end_of_turn=None,
        end_of_turn_silence_ms=0,
        # 16 kHz in; the bridge accepts the browser's capture rate unchanged.
        input_rate=CLIENT_RATE,
        autofire_wait=1.5,
        accepts_text_items=True,
        # Server VAD is off on this family, so fanned-in colleague audio no
        # longer fires a reply -- but it does still land in the member's own
        # input buffer and get committed as part of the member's turn. The room
        # tells this family what a colleague said, in text, for the same reason
        # it does on native-audio. This is the behaviour origin/main shipped
        # for the fallback route (accepts_text_items gated it there).
        relay_colleagues_as_text=True,
        # The COMMIT starts the reply on this route; a response.create on top
        # is rejected as an active-response conflict. give_floor commits and
        # then clears the response state rather than asking again.
        grant_via_text_prompt=False,
        member_tools=True,
        # The first audio delta can trail response.created by several seconds
        # here, and a commit + create sent in that gap is rejected. On this
        # route `created` is the signal.
        autofire_at_created=True,
        # input_audio_transcription is required here anyway
        # (needs_input_transcription); the language rides on the same dict and
        # asking for it is harmless (verified 2026-09-08).
        transcription_language_hint=True,
        # The scenario bank names Gemini voices and a bank entry is not a typo.
        # Each maps to the nearest voice on this roster, stable per character,
        # so a character cast as Kore is `shimmer` on every gpt run rather than
        # a session that opens with a rejected voice and plays the gateway's
        # stock assistant instead of the character.
        voice_aliases={
            "puck": "alloy", "charon": "echo", "kore": "shimmer",
            "fenrir": "ash", "aoede": "coral", "leda": "sage",
            "orus": "verse", "zephyr": "marin",
        },
    ),
}


def end_of_turn_for(model: str) -> Optional[dict]:
    """The connect-time turn_detection dict `model`'s family was measured to
    honour, or None. A copy, so a caller cannot edit the row through it."""
    caps = capabilities_for(model)
    if caps is None or not caps.end_of_turn:
        return None
    return dict(caps.end_of_turn)


def end_of_turn_silence_ms_for(model: str) -> int:
    """How long the runner's own end-of-turn silence must be on `model`'s
    family (0: the runner's VAD_SILENCE_MS stands). See the gemini row."""
    caps = capabilities_for(model)
    return int(caps.end_of_turn_silence_ms) if caps is not None else 0


def family_of(model: str) -> str:
    """Which family a model name belongs to, or "" for one nothing covers.

    Matched on the substrings that never move in a model id rather than on an
    exact list, because the gateway carries several members of each family
    (gpt-realtime-2, -2.1, -2.1-mini; gemini-live-2.5-flash and its
    native-audio sibling) and adds more without asking us. A name that matches
    nothing returns "" and is refused loudly upstream: guessing a family is how
    a study ends up running a model whose behaviour nobody checked.
    """
    name = (model or "").lower()
    if "gemini" in name and "live" in name:
        # ORDER MATTERS. The native-audio sibling matches the gemini-live test
        # too, and answering "gemini-live" for it is not a near-miss: it feeds
        # that route 16 kHz input, which it accepts and then ignores forever --
        # session open, no transcription, no reply, no error. The narrower test
        # goes first.
        if "native-audio" in name:
            return "gemini-live-native-audio"
        return "gemini-live"
    if "realtime" in name and ("gpt" in name or "openai" in name):
        return "gpt-realtime"
    return ""


def capabilities_for(model: str):
    """The row for `model`, or None when the table does not cover it.

    None rather than a raise, because this is also the door other modules read
    the table through (see server/realtime_voice_session.py) and a lookup that
    explodes is a lookup callers stop making. `require_capabilities` is the
    strict form, and it is what this module's own connect() uses.
    """
    return REALTIME_FAMILIES.get(family_of(model))


def require_capabilities(model: str) -> RealtimeCapabilities:
    caps = capabilities_for(model)
    if caps is None:
        raise UnknownRealtimeModel(
            f"REALTIME_MODEL={model!r} belongs to no family in "
            f"REALTIME_FAMILIES ({', '.join(sorted(REALTIME_FAMILIES))}). "
            "Add a row for it — voices, transcription, turn detection and "
            "whether mid-session steering is honoured — before running a "
            "study on it; every one of those was different between the two "
            "families already in the table."
        )
    return caps


def casting_families() -> tuple:
    """The distinct columns a scenario's `realtime_voice` map has to carry.

    Not the same as the set of family names: two families that share a roster
    share a column (see RealtimeCapabilities.casting_family). This is what the
    bank is checked against, so adding a row that shares a roster does not
    demand a new line in every scenario file, and adding one that does NOT
    share a roster demands it immediately.
    """
    return tuple(sorted({caps.casting_key
                         for caps in REALTIME_FAMILIES.values()}))


def voices_for(model: str) -> tuple:
    """The roster `model` will accept, in casting order."""
    return require_capabilities(model).voices


def resolve_voice(model: str, voice: str = "") -> str:
    """The voice a session on `model` should open with.

    Empty means the family default. Anything else has to be on the family's
    roster: this is the check that happens BEFORE a socket exists, because on
    Gemini a wrong voice produces no error frame to check afterwards — it
    produces nothing at all, forever.
    """
    caps = require_capabilities(model)
    if not voice:
        return caps.default_voice
    if not caps.accepts_voice(voice):
        raise UnsupportedVoice(
            f"voice {voice!r} is not accepted by the {caps.family} family "
            f"(model {model!r}); it accepts {', '.join(caps.voices)}. "
            "A voice from the other family, or an ElevenLabs voice id left "
            "over from the v1 cascade, takes the whole character brief down "
            "with it and the actor then plays the gateway's stock assistant."
        )
    return voice


# -- the same table, read by the names her routes call it by ----------------
#
# input_rate_for_model / autofire_wait_for_model / accepts_text_items /
# is_openai_realtime / voice_for_model came in on origin/main as five
# independent substring tests on the model name. They are kept as names --
# server/group_room.py and server/realtime_voice_session.py import them -- but
# the answers now come out of REALTIME_FAMILIES above, so there is ONE place a
# model's behaviour is written down and one place to change it. The substring
# fallback survives only for a model the table does not cover, and is marked as
# such: a study should not be run on a model with no row (see
# require_capabilities), but a helper that raises in a hot path is a helper
# callers stop calling.


def input_rate_for_model(model: str) -> int:
    """Sample rate the bridge expects for input audio on this model.

    The native-audio Gemini route silently ignores 16 kHz input: the session
    stays open and never transcribes or replies (found 2026-09-08 after nine
    config variants failed; 24 kHz input fixed it immediately). The other
    Gemini route and the OpenAI route accept 16 kHz.
    """
    caps = capabilities_for(model)
    if caps is not None:
        return caps.input_rate
    return 24000 if "native-audio" in (model or "").lower() else CLIENT_RATE


def autofire_wait_for_model(model: str) -> float:
    """How long to give the bridge to start its own reply before asking.

    Measured: the Gemini route fires about 1 s after silence, the native-audio
    route about 3.3 s. Asking too early yields a second, colliding reply.

    AUTOFIRE_WAIT still overrides everywhere, because that env knob is what the
    runner's single 1.5 s default was; the per-family value is what it falls
    back to instead of one number for every route.
    """
    env = os.getenv("AUTOFIRE_WAIT")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    caps = capabilities_for(model)
    if caps is not None:
        return caps.autofire_wait
    return 4.5 if "native-audio" in (model or "").lower() else 1.5


def accepts_text_items(model: str) -> bool:
    """Whether conversation.item.create with text is safe on this route.

    NOTE the row comments: this is True on every family in the table now. The
    2026-08-19 finding that a text item closes the plain-Gemini socket with
    1006 was an over-specified session config, not the item; re-probed
    2026-09-10 and 2026-09-14 on a flat session config, a user-role text item
    plus response.create is accepted and answered, first delta 0.23 s. Our
    audio-recovery retry and our group scene-open both ride on that. See
    docs/migration-plan.md.
    """
    caps = capabilities_for(model)
    if caps is not None:
        return caps.accepts_text_items
    m = (model or "").lower()
    return "native-audio" in m or m.startswith("gpt-")


def relays_colleagues_as_text(model: str) -> bool:
    """Whether a room should TELL this member what a colleague said, in text,
    instead of fanning the colleague's audio into its input.

    True on the routes measured in production with it on: native-audio, where
    fanned-in colleague audio confused the member's turn detection, and the gpt
    fallback, where server VAD is off and fanned audio only pollutes the
    member's own committed turn. False on plain flash, which is the route our
    fan-out byte counters and give_floor's `heard_something` were measured
    against.
    """
    caps = capabilities_for(model)
    if caps is not None:
        return caps.relay_colleagues_as_text
    return accepts_text_items(model)


def grants_via_text_prompt(model: str) -> bool:
    """Whether handing this member the floor means injecting a text nudge and
    asking, rather than padding and committing its audio buffer.

    True on native-audio: the route has already consumed the audio with a reply
    of its own that was dropped, so a commit of padding yields an empty
    response. Injecting a text item is the only recipe that wakes a session
    which will not answer a commit -- the same recipe as open_scene.
    """
    caps = capabilities_for(model)
    if caps is not None:
        return caps.grant_via_text_prompt
    return "native-audio" in (model or "").lower()


def member_tools_allowed(model: str) -> bool:
    """Whether a ROOM MEMBER on this route may be given tools.

    False on native-audio: it calls end_conversation constantly and each call
    is an empty turn (measured in production, origin/main 5a45420). True
    elsewhere, which keeps END_SEGMENT_TOOL wired on the routes where it was
    measured to work. The 1:1 actor is unaffected either way -- this is a
    room-member rule.
    """
    caps = capabilities_for(model)
    if caps is not None:
        return caps.member_tools
    return "native-audio" not in (model or "").lower()


def autofire_visible_at_created(model: str) -> bool:
    """Whether `response.created` alone is proof the bridge started a reply.

    On the gpt route the first audio delta can trail response.created by
    several seconds, and a commit + response.create sent in that gap is
    rejected as an active-response conflict; there, created is the signal. On
    plain flash our own measurement is the other way round -- the reply that
    counts is the one that produces output -- and a created that never becomes
    a delta would otherwise latch autofire_active and mute the encounter.
    """
    caps = capabilities_for(model)
    if caps is not None:
        return caps.autofire_at_created
    name = (model or "").lower()
    return not ("gemini" in name and "native-audio" not in name)


def is_openai_realtime(model: str) -> bool:
    caps = capabilities_for(model)
    if caps is not None:
        return caps.family == "gpt-realtime"
    return (model or "").lower().startswith("gpt-")


def transcription_language() -> str:
    """The language hint put on every realtime session.

    TRANSCRIPTION_LANG, default "en"; set it blank to send no hint at all.
    This is not cosmetic: without it the transcriber returned a Russian word
    and Japanese syllables from an English-speaking participant, and the
    participant's transcript is the measurement. Verified 2026-09-08 not to
    mute either Gemini route.
    """
    return os.getenv("TRANSCRIPTION_LANG", "en")


def voice_for_model(voice: str, model: str) -> str:
    """The voice name this model family accepts, translating across families.

    The scenario bank names Gemini voices. On the gpt route those names are
    rejected, and a rejected voice takes the whole character brief down with
    it, so each is mapped to the nearest voice on that family's roster
    (`voice_aliases` on the row). Stable per character, like the Gemini
    assignment.

    This is the LENIENT door, for a voice that arrived from a scenario file.
    `resolve_voice` is the strict one: it still refuses a name that is neither
    on the roster nor a known alias, because that is a typo or a leftover
    ElevenLabs id and permanent silence is the worst way to find out.
    """
    if not voice:
        return voice
    caps = capabilities_for(model)
    if caps is None:
        return voice
    if caps.accepts_voice(voice):
        return voice
    alias = caps.voice_aliases.get(voice.lower())
    if alias:
        return alias
    lowered = voice.lower()
    for known in caps.voices:
        if known.lower() == lowered:
            return known
    # No alias and not on the roster: hand it back UNTRANSLATED so that
    # connect()'s resolve_voice still refuses it by name. origin/main fell back
    # to "alloy" here, which is right for a scenario-bank voice and wrong for a
    # typo or a leftover ElevenLabs id -- those would then run the whole wave in
    # a voice nobody chose, and the record would say so without anyone noticing.
    # The bank's own names are covered by voice_aliases above; anything else is
    # a mistake worth stopping for.
    return voice


# How long a reply we asked for may produce nothing at all before events()
# declares it lost. 45 s matches the timeout the group sequencer already waits
# out (realtime_voice_session._speak_as), so a stalled reply is called dead at
# the same moment whichever loop is watching it.
RESPONSE_STALL_S = float(setting("REALTIME_RESPONSE_STALL_S", "45"))
# How often events() comes up for air to run that check. Nothing on the wire is
# normal between turns, so this is a poll interval, not a socket timeout.
RECV_POLL_S = 5.0

# A SECOND stall detector, audio-specific, beside RESPONSE_STALL_S rather than
# in place of it. The two answer different questions and are set from different
# measurements:
#
#   RESPONSE_STALL_S asks "has this reply produced ANYTHING lately?" and is
#   matched to the group sequencer's own wait so both loops give up on a dead
#   reply at the same moment. It is the right bar for a reply that never said a
#   word, and lowering it would call a slow-but-live reply dead in one loop while
#   the other was still waiting for it.
#
#   AUDIO_ABSENT_S asks the narrower question "the words of this reply have
#   arrived - where is the voice?". Measured live on nto.gemini-live-2.5-flash
#   through this gateway (190 replies), the first audio delta follows the last
#   transcript delta within 0.14 s on 90% of healthy replies and within 2.65 s
#   on every one of them; the only longer waits were replies the gateway had
#   abandoned outright, which resumed only when the participant spoke again (a
#   fresh commit) or were called dead by RESPONSE_STALL_S after 45 s of silence.
#   3 of 4 single-mode encounters had one. The bar sits at three times that
#   tail, and the clock is HELD while the participant is talking: Gemini's own
#   turn detection withholds a reply's audio while it hears speech and delivers
#   it the moment the speech stops, and a retry issued in that window would
#   commit half of the participant's sentence as a turn.
#
#   The same bar closes a THIRD shape, seen live on the first after-wave: a
#   reply's audio arrives - all of it, 10 words in 2.5 s - and then nothing,
#   ever: no response.done. Only RESPONSE_STALL_S ended it, 45 s later, and
#   every commit the participant made in between was refused by commit_turn as
#   "a reply is in flight". Measured over 502 properly-closed replies across
#   six waves, the longest a healthy reply ever goes quiet between two of its
#   own output frames, net of participant speech, is 3.9 s (p90 1.1 s), so a
#   reply that has said something and then said nothing for 8 s is over. It is
#   closed out as heard - retried only if its audio is short of its words by
#   the usual rule, and never re-spoken when it was delivered whole.
AUDIO_ABSENT_S = float(setting("REALTIME_AUDIO_ABSENT_S", "8"))
# How many times ONE participant turn's reply may be re-requested when its audio
# was demonstrably lost. One, enforced by a counter on the turn (see
# retry_response): a retry that also fails is finalised as truncated by the
# existing detection and the encounter moves on, so a gateway that is broken
# for good costs one extra request per turn and never a loop.
AUDIO_RETRY_LIMIT = 1
# What the retry puts in front of the model, as a user text item, before asking
# again. It is the ONLY thing measured to bring an abandoned Gemini reply back:
# a commit of 300 ms of silence and a commit of 1.2 s of room noise (the
# sequence the group sequencer uses to make a session speak) each produced
# nothing at all after a dead reply, and the turn fell to the 45 s watchdog;
# this item produced a complete, properly closed reply 0.23 s later. It is
# never spoken by the participant and never enters the participant transcript;
# the runner writes it on the audio_retry event so a rater can see that the
# line that follows answers a lost-audio prompt, not the participant.
AUDIO_RETRY_NUDGE = setting(
    "REALTIME_AUDIO_RETRY_NUDGE",
    "(I didn't hear that - the audio dropped. Could you say it again?)",
)
# A truncation verdict is not acted on the instant it lands. It is HELD for
# this long, and dropped if anything in that window says the gateway stopped
# the reply for a reason of its own:
#
#   * the participant is talking. Gemini's own turn detection interrupts a
#     reply ~250-300 ms after it hears speech, and it hears speech the runner's
#     VAD does not: a voice under VAD_RMS_THRESHOLD, or one that has not yet
#     been going for min_speech_ms. Both were measured to produce exactly the
#     truncation signature (bare response.done, 200-1700 ms of audio for
#     17-48 words) with `vad.speaking` still False. Re-asking then puts the
#     character back on top of the participant's sentence, which is the
#     interruption path being undone from underneath. The window is checked
#     against the VAD's HINT (any frame over VAD_HINT_RMS, no minimum
#     duration - see SilenceDetector.active_within), which fires on the first
#     frame of a soft voice rather than 250 ms into a loud one.
#   * the gateway starts a reply of its own (response.created). That is what
#     it does after interrupting itself for speech it heard, once the speech
#     stops; the truncated head was never lost, it was superseded.
#   * the participant's turn ends (a commit) or a barge-in cancels the reply.
#
# One second, because the interruption latency is a quarter of that and the
# VAD hint is real-time; a genuine truncation costs one extra second of dead
# air before the line is re-asked, against the ~1 s the participant already
# sat through hearing it stop. The absent detector needs no such window: its
# bar is eight seconds of quiet already.
AUDIO_RETRY_QUIET_S = float(setting("REALTIME_AUDIO_RETRY_QUIET_S", "1.0"))
# A reply the gateway is looping on (measured once: one sentence repeated 883
# times, 15,868 words and 224 s of audio pushed in 30 s of wall time) is "short
# of its transcript" by the arithmetic and is not a thing to ask for again.
AUDIO_RETRY_MAX_WORDS = int(setting("REALTIME_AUDIO_RETRY_MAX_WORDS", "200"))
AUDIO_RETRY_MAX_AUDIO_MS = int(setting("REALTIME_AUDIO_RETRY_MAX_AUDIO_MS", "20000"))

# How long update_instructions() waits for a session.updated before calling a
# brief undelivered. Only ever spent on a family whose row says the ack is
# coming; measured at 38-381 ms live, so this is a generous ceiling rather than
# a budget, and it is paid on the director's path, never on the participant's.
UPDATE_ACK_S = float(setting("REALTIME_UPDATE_ACK_S", "2.0"))

# A reply THIS bridge asked for (commit_turn / request_response / a room's
# give_floor) that has produced nothing at all — no response.created, no
# delta — for this long is not coming. Measured live on
# nto.gemini-live-2.5-flash with a hesitant participant: 4 of 21 sessions had
# a turn where the commit+create drew no frame of any kind, and every one of
# them sat until RESPONSE_STALL_S (45 s) called it dead — 47 s of a character
# that had "stopped hearing" the participant. A healthy reply's first frame
# follows the request within ~0.3-2.6 s here, so ten seconds of nothing is
# not a slow reply. The bridge then hands the turn back to the runner as
# `reply_missing`; the runner may ask once more with a user TEXT item
# (UNANSWERED_NUDGE), which is the one thing measured to draw a reply out of
# this gateway after it has ignored a create, and which the record shows.
# RESPONSE_STALL_S still stands behind it for replies the runner did not
# request (auto-fired ones have output by definition, so they are covered by
# AUDIO_ABSENT_S instead).
# Six seconds, down from ten (2026-09-14, second live round with the
# researcher's own hesitant lines): across eleven live encounters every reply
# that did begin had its first frame within 2.6 s of the request, and in every
# 1:1 encounter longer than ~90 s the gateway went silent for good — no frame
# of any kind, then the TCP connection dropped 22-38 s later with no close
# frame. Ten seconds of waiting on a socket that will never answer is dead air
# charged to the participant; six is still more than twice the slowest healthy
# reply.
REQUEST_UNANSWERED_S = float(setting("REALTIME_REQUEST_UNANSWERED_S", "6"))
# A REPLAYED line (replay_input) that draws nothing at all for this long has
# gone into a dead socket: live, a replayed line on a socket that was alive
# had its transcript back within a second and its reply within 2.6 s, and the
# sockets that answered nothing were dropped by the gateway 5-15 s later. The
# runner rebuilds the session on this bar rather than the 8 s audio bar, and
# replays the line once more into the new one.
REPLAY_UNANSWERED_S = float(setting("REALTIME_REPLAY_UNANSWERED_S", "4"))
UNANSWERED_NUDGE = setting(
    "REALTIME_UNANSWERED_NUDGE",
    "(They have just spoken and are waiting for you to answer.)",
)
# What opens a group scene on a family whose members answer a commit of pure
# silence with nothing at all (Gemini: pad + commit + response.create on a
# session that has heard nothing drew no frame, 5/5 rooms; a user TEXT item +
# response.create drew a full in-character opening 4/4). Never spoken by the
# participant and never enters the participant transcript; the runner writes
# it on the record beside the opening turn.
SCENE_OPEN_PROMPT = setting(
    "REALTIME_SCENE_OPEN_PROMPT",
    "(The meeting is under way and everyone is looking at you. You have the "
    "floor - speak first, in character.)",
)
# How many times one encounter may rebuild a 1:1 gateway session that the
# GATEWAY closed under it. A close we did not ask for used to end the encounter
# on the participant's screen with a "connection lost" card; the second such
# card hides the retry. Two rebuilds per encounter is the budget; a gateway
# that drops a third socket has stopped being usable.
RECONNECT_LIMIT = int(setting("REALTIME_RECONNECT_LIMIT", "2"))


def _ws_url(model: str) -> str:
    base = GATEWAY.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    return f"{base}/v1/realtime?model={model}"


# How loud a room has to be before the fixed VAD threshold stops meaning
# anything. A participant wearing headphones in a room with a fan sits at an
# RMS the shipped threshold (500, -36 dBFS) calls speech CONTINUOUSLY: measured
# offline on the shipped detector, steady noise at RMS 600 opened a turn every
# 260 ms, forever. That is not a cosmetic fault. Every one of those is a
# `speech_started`, and `speech_started` while a character is talking is a
# barge-in: _client_to_model cancels the reply and the page throws away every
# audio buffer it had already scheduled. The participant hears a good sentence
# stop dead, which is exactly what the researcher reported hearing.
#
# So the floor is measured rather than assumed: the quietest 20 ms in the last
# `VAD_FLOOR_WINDOW_MS` is what this room sounds like when nobody is talking,
# and speech has to clear it by `VAD_NOISE_MARGIN`. A windowed MINIMUM is used
# rather than an average because speech is gappy — the stops between words fall
# to the bed — so a window that is full of talking still reports the bed, and
# the floor cannot be dragged up by the very speech it is supposed to let
# through. VAD_MAX_THRESHOLD is the backstop for the one case that argument
# does not cover (a speaker with no gaps at all for the whole window): however
# loud the room is measured to be, the bar never rises above a level ordinary
# speech clears anyway, so a bad floor estimate can never make the participant
# uninterruptible. Set VAD_NOISE_MARGIN to 0 to switch the adaptation off and
# get the fixed threshold back exactly.
VAD_FLOOR_WINDOW_MS = int(setting("VAD_FLOOR_WINDOW_MS", "2500"))
VAD_NOISE_MARGIN = float(setting("VAD_NOISE_MARGIN", "3.0"))
VAD_MAX_THRESHOLD = int(setting("VAD_MAX_THRESHOLD", "2500"))
# The separate, stricter bar a barge-in has to clear; see `barge_in` below.
VAD_BARGE_RMS = int(setting("VAD_BARGE_RMS", "1000"))
# BARGE_IN_MS is origin/main's name for this same bar, where it stood alone at
# a fixed 600 ms with no loudness gate beside it. It is still honoured — as the
# fallback for VAD_BARGE_MS, so an operator who had exported it is changing the
# sustained-speech bar and not setting an inert variable — but the shipped
# default is 300 ms, because here the bar is one of a PAIR: a frame counts
# towards it only once it has also cleared VAD_BARGE_RMS, and 300 ms of speech
# that loud is a firmer signal than 600 ms of anything at all. Live, that pair
# cancelled the speaker on 6 of 12 deliberate interjections against 2 of 9 for
# the single bar it replaced. BARGE_IN_MS=600 with VAD_BARGE_RMS=0 puts the
# duration back where origin/main had it and drops the extra loudness gate to
# the ordinary speech bar, which is as close to the old rule as this detector
# gets: the old one timed from speech_started, this one counts qualifying
# frames and decays them.
VAD_BARGE_MS = int(setting("VAD_BARGE_MS", setting("BARGE_IN_MS", "300")))
# The third, LOOSEST bar: "is there anything on the microphone that could be a
# voice?" It opens no turn and cuts nobody off; its one job is to keep the
# audio-recovery retry (see AUDIO_RETRY_QUIET_S) from re-speaking a line on
# top of a participant the gateway can hear and this detector's turn bar
# cannot. Measured: speech scaled to RMS 250-450 (under the 500 turn bar) was
# transcribed by Gemini every time and interrupted the character every time.
# The bar is the larger of a fixed floor, a fraction of the turn bar, and a
# multiple of the room's measured noise floor, so room tone alone does not
# hold it up for good; when it IS held up, the cost is a retry not issued,
# which is the behaviour before the retry existed.
VAD_HINT_RMS = int(setting("VAD_HINT_RMS", "160"))
VAD_HINT_RATIO = float(setting("VAD_HINT_RATIO", "0.4"))
VAD_HINT_FLOOR_MARGIN = float(setting("VAD_HINT_FLOOR_MARGIN", "2.0"))
# ...and sustained for this long, with the same frame-by-frame decay the barge
# bar uses, so a keyboard click or a chair (40 ms bursts, however loud) is not
# a voice: on the typing bed a per-frame hint was up 8% of the time with nobody
# speaking, which with the window below is "always". A soft voice clears 120 ms
# in its first syllable, a sixth of the time the gateway takes to act on it.
VAD_HINT_MS = int(setting("VAD_HINT_MS", "120"))
# How long after the last hinted frame the participant still counts as active.
# Covers the 0.3-0.5 s holes the turn detector's `speaking` has inside every
# utterance (measured) and the beat between two of the participant's sentences.
VAD_HINT_WINDOW_S = float(setting("VAD_HINT_WINDOW_S", "1.0"))


@dataclass
class SilenceDetector:
    """End-of-turn detection, because the gateway does not do it for us.

    Speech is detected on RMS energy; a turn ends after `silence_ms` of quiet
    following speech. `min_speech_ms` keeps a cough or a door slam from opening
    a turn that immediately closes.

    Two things here exist because the same mark, `speech_started`, does two
    jobs: it opens the participant's turn, and — while a character is speaking —
    it cuts that character off mid-sentence. The second job is far less
    forgiving than the first, and the detector that was good enough for the
    first was cutting good speech off.

      * The bar adapts to the room (see VAD_FLOOR_WINDOW_MS above), so a fan or
        an air handler is not heard as a person talking.
      * Partial speech decays continuously rather than being reset only after a
        full `silence_ms` of UNBROKEN quiet. The old rule zeroed `_silence_ms_run`
        on every frame over the bar, so noise that recurred even once a second —
        typing, a squeaky chair — never got its 900 ms of quiet and accumulated,
        one 40 ms click at a time, until it crossed min_speech_ms and cancelled
        whoever was speaking. Measured on the shipped detector: keyboard clatter
        opened a turn every 2 s, indefinitely.

    `barge_in` is the third piece and the one that protects the reply directly:
    a stricter test, applied only where a false positive costs the participant
    the rest of a sentence. Nothing about interruption is made slower or harder
    for a person who actually speaks — the measured cost is one extra frame or
    two on a real interjection — and `feed`'s own marks are untouched, so turn
    detection and the "your turn" cue behave exactly as before.
    """

    threshold: int = field(default_factory=lambda: int(setting("VAD_RMS_THRESHOLD", "500")))
    silence_ms: int = field(default_factory=lambda: int(setting("VAD_SILENCE_MS", "900")))
    min_speech_ms: int = 250
    rate: int = CLIENT_RATE

    # Room-noise adaptation. Defaults come from the module constants so a
    # deployment can turn the whole thing off with one env var, and so a test
    # can build a detector with the old fixed-threshold behaviour by passing
    # noise_margin=0.
    floor_window_ms: int = field(default_factory=lambda: VAD_FLOOR_WINDOW_MS)
    noise_margin: float = field(default_factory=lambda: VAD_NOISE_MARGIN)
    max_threshold: int = field(default_factory=lambda: VAD_MAX_THRESHOLD)
    barge_rms: int = field(default_factory=lambda: VAD_BARGE_RMS)
    barge_ms: int = field(default_factory=lambda: VAD_BARGE_MS)
    hint_rms: int = field(default_factory=lambda: VAD_HINT_RMS)
    hint_ratio: float = field(default_factory=lambda: VAD_HINT_RATIO)
    hint_ms: int = field(default_factory=lambda: VAD_HINT_MS)
    hint_window_s: float = field(default_factory=lambda: VAD_HINT_WINDOW_S)

    speaking: bool = False
    # When a frame last cleared the hint bar (see VAD_HINT_RMS), wall clock,
    # and how loud it was. Read through `active_within`; never a mark.
    last_hint_at: float = 0.0
    last_hint_rms: int = 0
    # True on the frame where this turn's speech becomes loud enough and
    # sustained enough to justify cutting a character off, and for as long as
    # the participant keeps talking. Read by the runner's barge-in branches;
    # `feed`'s return value is deliberately not overloaded with it, because the
    # two decisions happen at different moments and the marks are a contract
    # with the rest of the runner.
    barge_in: bool = False
    _speech_ms: float = 0.0
    _silence_ms_run: float = 0.0
    _barge_ms_run: float = 0.0
    _hint_ms_run: float = 0.0
    _floor: list = field(default_factory=list)
    _floor_len: int = 0

    def _note_level(self, rms: int, chunk_ms: float) -> None:
        """Keep the last `floor_window_ms` of levels, for the room's floor."""
        if self.noise_margin <= 0 or chunk_ms <= 0:
            return
        self._floor_len = max(1, int(self.floor_window_ms / chunk_ms))
        self._floor.append(rms)
        if len(self._floor) > self._floor_len:
            del self._floor[:len(self._floor) - self._floor_len]

    def effective_threshold(self) -> int:
        """The level speech has to clear right now.

        The fixed threshold until the window has actually seen `floor_window_ms`
        of audio: an estimate from a part-full window is an estimate made
        entirely of whatever is happening at that second, and at the start of an
        encounter that is as likely to be the participant saying hello as it is
        to be the room.
        """
        if (self.noise_margin <= 0 or not self._floor
                or len(self._floor) < self._floor_len):
            return self.threshold
        adaptive = int(min(self._floor) * self.noise_margin)
        return max(self.threshold, min(adaptive, self.max_threshold))

    def hint_threshold(self) -> int:
        """The level a frame has to reach to count as "possibly a voice".

        Lower than the turn bar by design (see VAD_HINT_RMS), but never lower
        than twice what this room measures as silence, so steady room tone
        cannot keep the participant permanently "active"."""
        bar = self.effective_threshold()
        floor = 0
        if (self.noise_margin > 0 and self._floor
                and len(self._floor) >= self._floor_len):
            floor = min(self._floor)
        return max(self.hint_rms, int(bar * self.hint_ratio),
                   int(floor * VAD_HINT_FLOOR_MARGIN))

    def active_within(self, seconds: Optional[float] = None) -> bool:
        """Is the participant talking, or has anything voice-like reached the
        microphone in the last `seconds` (default: hint_window_s)?

        `speaking` alone was the guard on the audio-recovery retry, and it has
        two holes a retry fell through live: it comes up min_speech_ms after a
        voice starts, and it drops for 0.3-0.5 s inside an ordinary utterance.
        This answers the looser question the retry actually needs answered."""
        if self.speaking:
            return True
        window = self.hint_window_s if seconds is None else seconds
        return self.last_hint_at > 0.0 and (time.time() - self.last_hint_at) <= window

    def feed(self, pcm: bytes) -> Optional[str]:
        """Returns 'speech_started', 'turn_ended', or None."""
        if not pcm:
            return None
        chunk_ms = len(pcm) / 2 / self.rate * 1000.0
        rms = _rms(pcm)
        bar = self.effective_threshold()
        if rms >= self.hint_threshold():
            self._hint_ms_run += chunk_ms
            if self._hint_ms_run >= self.hint_ms:
                self.last_hint_at = time.time()
                self.last_hint_rms = rms
        else:
            self._hint_ms_run = max(0.0, self._hint_ms_run - chunk_ms)
        self._note_level(rms, chunk_ms)

        # The barge-in bar, tracked on every frame so it is current whenever a
        # character starts speaking. Loud enough AND sustained: a door slam
        # clears the level and not the duration, a fan clears neither once the
        # floor is known, and keyboard clatter clears the level in 40 ms bursts
        # that the same decay below takes back before they add up.
        if rms >= max(bar, self.barge_rms):
            self._barge_ms_run += chunk_ms
        else:
            self._barge_ms_run = max(0.0, self._barge_ms_run - chunk_ms)
        self.barge_in = self._barge_ms_run >= self.barge_ms

        if rms >= bar:
            self._silence_ms_run = 0.0
            self._speech_ms += chunk_ms
            if not self.speaking and self._speech_ms >= self.min_speech_ms:
                self.speaking = True
                return "speech_started"
            return None

        if self.speaking:
            self._silence_ms_run += chunk_ms
            if self._silence_ms_run >= self.silence_ms:
                self.speaking = False
                self._speech_ms = 0.0
                self._silence_ms_run = 0.0
                self._barge_ms_run = 0.0
                self.barge_in = False
                return "turn_ended"
            return None

        # Not speaking and below the bar: decay any partial speech, frame by
        # frame, so noise that recurs faster than `silence_ms` cannot accumulate
        # towards min_speech_ms. Real speech outruns the decay comfortably — the
        # stops inside a word are tens of milliseconds against hundreds of
        # milliseconds of voicing — so the cost to a genuine interjection is a
        # frame or two, measured.
        self._silence_ms_run += chunk_ms
        self._speech_ms = max(0.0, self._speech_ms - chunk_ms)
        return None

    def reset(self) -> None:
        self.speaking = False
        self.barge_in = False
        self._speech_ms = 0.0
        self._silence_ms_run = 0.0
        self._barge_ms_run = 0.0
        self._hint_ms_run = 0.0
        # The room's noise floor is a property of the room, not of the turn, and
        # it takes floor_window_ms to re-learn. Deliberately kept.


class RealtimeVoiceSession:
    """One live conversation with the agent.

    Emits dicts: {"type": "user_transcript"|"agent_transcript_delta"|
    "agent_transcript"|"agent_audio"|"response_done"|"error", ...}
    Audio is PCM16 resampled to the client's rate.
    """

    def __init__(
        self,
        instructions: str,
        *,
        model: str = MODEL,
        voice: str = VOICE,
        tools: Optional[list] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.instructions = instructions
        self.model = model
        self.voice = voice_for_model(voice, model)
        self.tools = tools or []
        self.api_key = api_key or gateway_api_key()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        # The connect-time `turn_detection` dict to send, where the family's
        # row does not already force `null`. Left UNSET the payload carries no
        # such key, exactly as before this attribute existed — a session built
        # for nobody in particular stays bare, and the tests that pin the bare
        # payload stay true. The runner and the room set it from
        # `end_of_turn_for(model)` on every session they open (see the gemini
        # row for the measurement). Settable after construction, like `model`,
        # because the factories this project's tests and harnesses hand the
        # room take (instructions, voice, tools) and nothing else.
        self.turn_detection: object = _UNSET
        # True while a reply THIS bridge asked for (request_response, a retry,
        # a prompt) is waiting for its first frame; False for auto-fired ones
        # and once anything arrives. What REQUEST_UNANSWERED_S is measured on.
        self._requested = False
        # How many times connect() has been called on this object after the
        # first: the runner's reconnect-after-gateway-close counts through it.
        self.reconnects = 0
        # Audio appended since the last commit. The bridge kills a session
        # that commits an empty buffer, so give_floor checks this first; it
        # is zeroed when a bridge auto-fired response consumes the buffer.
        self.pending_input = 0
        self.input_rate = input_rate_for_model(model)
        self._in_resample_state = None
        # A response the bridge started on its own (after speech + silence),
        # as opposed to one we asked for. Tracked separately from
        # _response_active so group-room suppression behaviour is unchanged.
        self.autofire_active = False
        self._last_output_at = 0.0
        self._resample_state = None
        self._agent_buffer = ""
        self._response_active = False
        # When the in-flight reply was asked for, and whether anything has come
        # back for it yet. Only ever read through _response_stalled() and only
        # ever cleared through _end_response(), so a reply that dies without a
        # response.done cannot leave a latch behind — the failure that muted an
        # encounter for good.
        self._response_started_at = 0.0
        self._response_saw_output = False
        # Whether THIS reply's audio stream was ever properly closed.
        #
        # On nto.gemini-live-2.5-flash through this gateway, 21 of 191 live
        # replies (11%; 4 of 11 in the worst single encounter) stopped sending
        # audio deltas mid-cadence and emitted a bare `response.done` — usually
        # two of them — with none of response.output_audio.done,
        # response.content_part.done or response.output_item.done behind it. The
        # transcript for such a reply is COMPLETE, so the turn is recorded as a
        # whole sentence while 0.4-0.9 s of voice was delivered for 7 to 13
        # words, and the participant hears the line stop dead. Nothing in this
        # bridge could see that: response.done is read as a clean reply boundary
        # and the turn is finalised on it.
        #
        # It is upstream — the runner forwarded byte for byte what it received
        # on all 191 — so this cannot be repaired here. It can be told apart
        # from a fault of ours, which is what the flag is for: it rides out on
        # response_done and the runner keeps it beside the turn's own delivered
        # audio, so a short turn says which side of the gateway lost it.
        self._response_audio_seen = False
        self._response_audio_closed = False
        # This reply's own transcript and its own gateway audio, kept per reply
        # so response.done can ask the one question that identifies the
        # truncation above: is this enough voice for these words? Reasoned in
        # words per second against the reply's OWN text (turn_audio.shortfall,
        # the same arithmetic the post-hoc scan uses), never in absolute
        # seconds, so "Okay." at 0.4 s is complete and 13 words at 0.92 s is not.
        self._response_text = ""
        self._response_audio_bytes = 0
        # The recovery's books. `_retries_this_turn` is reset by every commit
        # (a new participant turn) and by a reply the gateway starts on its own,
        # and incremented only by retry_response, so a turn can be re-asked
        # AUDIO_RETRY_LIMIT times and no more. `_retry_in_flight` marks the
        # reply now streaming as that retry, so its response_done can say so and
        # the record can show whether the second attempt was heard whole.
        # `_cancelled_by_us` is the exclusion that keeps a barge-in from being
        # read as a truncation: a reply the participant cut off (or a room
        # suppressed) also ends in a bare response.done with the audio stream
        # never closed, and re-speaking it would undo the interruption the
        # participant meant. Set by cancel_response, cleared when the next reply
        # or the next commit begins.
        self._retries_this_turn = 0
        self._retry_in_flight = False
        self._cancelled_by_us = False
        # When the participant last had the floor by talking, as seen by the
        # runner's own VAD through `participant_speaking`. The audio-absent
        # clock counts from the later of this and the reply's last output.
        self._audio_absent_hold = 0.0
        # Set by the runner, which owns the microphone's VAD: a callable that
        # answers "is the participant talking, or has anything voice-like
        # reached the microphone in the last second?" (SilenceDetector
        # .active_within). None means unknown, which is read as no.
        self.participant_speaking: Optional[Callable[[], bool]] = None
        # The gateway names its replies, and the names are what tell a
        # response.done that ends THIS reply from the three other kinds it
        # sends, all measured live on nto.gemini-live-2.5-flash:
        #
        #   * a second done, 0-11 ms after every bare one, whose id was never
        #     response.created (29 of 29 bare dones). Read as a reply boundary
        #     it ended the retry just issued for the truncated reply, so the
        #     retry's real answer arrived as a gateway-started reply, reset the
        #     per-turn budget, and the same turn was retried again - without
        #     bound (seven retries on one participant turn, replayed).
        #   * a late done, 1.6-3.1 s after a reply's own, again with a fresh id.
        #     Landing while the NEXT reply streamed it was judged against that
        #     reply's words-so-far, called truncated, and the healthy reply was
        #     stopped on the page and re-spoken from the start.
        #   * the done of a reply we cancelled (a barge-in, a room suppression),
        #     which can arrive after the participant's next commit - by which
        #     time the per-commit `_cancelled_by_us` reset had already forgotten
        #     the cancel, and the line the participant cut off was re-spoken.
        #
        # So: `_response_created_id` is the reply now in flight (None while a
        # request or a retry is waiting for its response.created); a done for
        # any other id never touches the in-flight state. One never created on
        # this socket is a phantom and is dropped; one created earlier is
        # yielded as `stale` so the runner can clear a latch on it but not
        # close a turn. A socket that never names replies at all (a fake, or
        # a gateway that omits response.created) keeps the old rule: every
        # done ends whatever is in flight.
        self._response_created_id: Optional[str] = None
        self._names_replies = False
        self._created_ids: set = set()
        self._cancelled_ids: set = set()
        self.phantom_dones = 0
        self.stale_dones = 0
        # A truncation verdict being held for AUDIO_RETRY_QUIET_S (see the
        # constant). None when nothing is held.
        self._deferred: Optional[dict] = None
        # True while the gateway is streaming a reply's transcript a SECOND
        # time inside the same response (see the delta branch of events());
        # the counter is for the record.
        self._restreaming = False
        self.transcript_restreams = 0
        # True while the reply in flight was asked for with the participant's
        # own replayed audio (see replay_input and REPLAY_UNANSWERED_S).
        self._replay_in_flight = False
        # True once close() has been called on the CURRENT socket. A socket WE
        # closed (a character switch closes the outgoing session deliberately)
        # is an ordinary end of the pump, not a fault, and must not be reported
        # as one; a socket the gateway closed always is. Scoped to one socket,
        # not to this object: connect() puts it back down, because a latch with
        # no clearing path is what muted an encounter for good once already
        # (see _end_response), and this one would swallow every genuine gateway
        # close on a reconnected session.
        self._closing = False
        self._done_ids: set = set()
        # Steering honesty. `last_update_acked` is the answer to "did the last
        # brief actually arrive": True/False/None with the meanings in the
        # module docstring, kept as an attribute as well as returned because
        # the runner reads it either way. `_update_ack` is how events() — the
        # only reader of this socket — hands the answer back, and
        # `_events_running` is how update_instructions knows whether anyone is
        # listening for it at all; without that check a session nobody is
        # draining would report every direction as refused, which is a
        # different and equally false claim.
        self.last_update_acked: Optional[bool] = None
        self.unacked_updates = 0
        self._update_ack = asyncio.Event()
        self._events_running = False
        # Sends that went nowhere because the socket was already gone. Counted
        # rather than raised; see _send.
        self.send_failures = 0
        self.last_send_error = ""
        self.debug_log: list | None = [] if os.getenv("RT_DEBUG") else None

    @property
    def capabilities(self) -> RealtimeCapabilities:
        """This session's family row. Resolved on demand, not at construction:
        a caller is allowed to build a session for a model this process will
        never open, and the refusal belongs at connect()."""
        return require_capabilities(self.model)

    async def connect(self, *, open_conversation: bool = True) -> None:
        # Both of these are per-socket, and both SUPPRESS an event when set, so
        # carrying either into a new socket loses a turn silently: _closing
        # would make events() end a reconnected session without the terminal
        # error it promises, and a response id left in _done_ids would make the
        # new socket's response.done for that id a no-op, so the runner would
        # never finalise that turn. Cleared here rather than in close(), so a
        # close still reads as intentional right up to the next connect.
        self._closing = False
        self._done_ids.clear()
        self._created_ids.clear()
        self._cancelled_ids.clear()
        self._response_created_id = None
        self._names_replies = False
        self._deferred = None
        if not self.api_key:
            raise RuntimeError("No gateway API key (set LITELLM_API_KEY)")
        # Before the socket, deliberately. A voice the family does not know is
        # not a thing the gateway will reliably tell us about after the fact:
        # gpt says invalid_value and then throws the character brief away,
        # Gemini says nothing and goes silent for good. Both look healthy from
        # every other angle, so the only place this can be caught honestly is
        # here, where it can still be a refusal rather than a bad encounter.
        # It also settles the voice ONCE: self.voice is what the record will
        # say the participant heard, so it may not stay a wish.
        self.voice = resolve_voice(self.model, self.voice)
        # websockets renamed extra_headers -> additional_headers when the new
        # asyncio client became the top-level default in 14.0; requirements
        # allow >=12, so pick the kwarg the installed version actually accepts.
        header_kwarg = "additional_headers"
        try:
            if int(websockets.__version__.split(".")[0]) < 14:
                header_kwarg = "extra_headers"
        except (ValueError, AttributeError):
            pass
        self.ws = await websockets.connect(
            _ws_url(self.model),
            max_size=None,
            ping_interval=20,
            **{header_kwarg: {"Authorization": f"Bearer {self.api_key}"}},
        )
        await self._send({
            "type": "session.update", "session": self._session_payload(),
        })

    def _session_payload(self) -> dict:
        """The session dict, built from the family's row and nothing else.

        FLAT, always: a nested `audio: {...}` block is the shape that leaves a
        Gemini session alive and permanently mute, and that finding stands. The
        two per-family keys go in only where the table says the family needs
        them, for the same reason — an unnecessary key on the family whose
        failure mode is silence is a risk taken for nothing.

        One builder for connect() and for update_instructions(), which is not
        tidiness. A session.update is a whole-session statement, not a patch:
        re-sending it without `turn_detection` would hand server VAD back to a
        gpt session mid-encounter and start chopping the participant's turns in
        half, which is precisely the failure the key exists to prevent, arriving
        the moment the director first steers.
        """
        caps = self.capabilities
        session: dict = {"instructions": self.instructions}
        if self.voice:
            session["voice"] = self.voice
        if self.tools:
            session["tools"] = self.tools
        if caps.needs_input_transcription:
            session["input_audio_transcription"] = {
                "model": caps.input_transcription_model,
            }
        # The language hint, from origin/main cabc1dd, and the reason it is not
        # optional here: our own live runs had the transcriber return a Russian
        # word and Japanese syllables from an English-speaking participant, and
        # the participant's transcript IS the measurement. Verified 2026-09-08
        # not to mute either Gemini route -- which is the one thing the gemini
        # row above warns about, so this is asserted on a measurement and not on
        # a guess. TRANSCRIPTION_LANG= (blank) sends no hint at all.
        #
        # This is _session_payload, which builds the frame for connect AND for
        # every update_instructions: the hint is therefore on every realtime
        # session this server opens, which is what it has to be, and not only on
        # the first frame of each.
        #
        # It rides on whatever input_audio_transcription the row already built,
        # so the gpt route gets {"model": "whisper-1", "language": "en"} and the
        # gemini routes get {"language": "en"} -- the shapes each was measured
        # with.
        lang = transcription_language()
        if lang and caps.transcription_language_hint:
            hint = dict(session.get("input_audio_transcription") or {})
            hint["language"] = lang
            session["input_audio_transcription"] = hint
        if caps.needs_turn_detection_null:
            session["turn_detection"] = None
        elif self.turn_detection is not _UNSET:
            # The measured end-of-turn window (see the gemini row). Sent on
            # every session.update, not only the first, for the reason the
            # docstring gives: a later update without it would hand the
            # gateway's default window back and the pause split with it.
            session["turn_detection"] = self.turn_detection
        return session

    async def _send(self, payload: dict) -> None:
        """Put one frame on the wire, and never take the session down with it.

        A socket that dies mid-append used to raise straight out of
        `send_audio` into the runner's participant->model pump, whose one broad
        `except` records a `voice_error` and RETURNS. That ends the only
        coroutine reading the participant's microphone — for the rest of the
        encounter, across the character switch that installs a healthy new
        session, so the participant goes on talking into a browser that is
        still capturing and a server that no longer relays. In a group room it
        is worse: `GroupRoom.hear` gathers a send per member, so one dead
        member socket stops the room hearing anything.

        A dead socket is not news that has to travel by exception, either:
        `events()` is already the module's one channel for it and reports the
        close in words. So a send to a socket that has gone is counted here and
        dropped, and the encounter keeps the parts of itself that still work.
        """
        if not self.ws:
            return
        try:
            await self.ws.send(json.dumps(payload))
        except websockets.ConnectionClosed as exc:
            self.send_failures += 1
            self.last_send_error = f"{payload.get('type')}: {exc}"

    async def update_instructions(self, instructions: str) -> Optional[bool]:
        """Re-issue the actor's brief, and say whether it arrived.

        This is how the director steers: the stage direction is appended to the
        persona before the next reply, the same contract the v1 director-actor
        loop used. Every director path in the runner comes through here, so
        what this returns is what the steering record is worth.

        True  — a `session.updated` frame came back for this update.
        False — the platform was asked and did not answer. On a family whose
                row says mid-session updates are not honoured this is known in
                advance and returned without waiting, because there is nothing
                to wait for and a study should not pay 2 s a beat to be told so
                again.
        None  — this bridge cannot tell. `events()` is the only reader of the
                socket and therefore the only place an ack can be seen; when
                nobody is draining it, "no ack observed" says something about
                this process, not about the gateway, and must not be recorded
                as a refusal.
        """
        self.instructions = instructions
        caps = self.capabilities
        self._update_ack.clear()
        failures = self.send_failures
        await self._send({
            "type": "session.update", "session": self._session_payload(),
        })
        if self.ws is None or self.send_failures > failures:
            # The frame never left. That is not "we could not tell", it is a
            # direction that certainly did not arrive, and the difference is
            # the whole point of a three-valued answer.
            self.unacked_updates += 1
            self.last_update_acked = False
            return False
        if not caps.honours_session_update:
            self.unacked_updates += 1
            self.last_update_acked = False
            return False
        if not self._events_running:
            self.last_update_acked = None
            return None
        try:
            await asyncio.wait_for(self._update_ack.wait(), UPDATE_ACK_S)
        except asyncio.TimeoutError:
            self.unacked_updates += 1
            self.last_update_acked = False
            return False
        self.last_update_acked = True
        return True

    async def send_audio(self, pcm16: bytes) -> None:
        """Append participant audio (PCM16 at CLIENT_RATE)."""
        if not pcm16:
            return
        if self.input_rate != CLIENT_RATE:
            # Through _ratecv, not audioop directly: audioop was removed in
            # Python 3.13 (PEP 594) and the CI matrix runs three Pythons. The
            # pure-Python fallback carries the same opaque state tuple.
            pcm16, self._in_resample_state = _ratecv(
                pcm16, CLIENT_RATE, self.input_rate, self._in_resample_state
            )
        self.pending_input += len(pcm16)
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm16).decode("ascii"),
        })

    async def inject_text(self, text: str, role: str = "user") -> None:
        """Add a text item to the conversation (no reply requested).

        Used to TELL a room member what a colleague just said, in place of
        fanning that colleague's audio into its input, on the families whose
        row says relay_colleagues_as_text.

        The sibling is `prompt_response`, which sends the same item and then
        asks for a reply; this one deliberately does not, because a context
        note is not a cue to speak.

        origin/main's version of this docstring said the original Gemini route
        closes the socket with 1006 on text items. That was the 2026-08-19
        over-specified session config, not the item: re-probed 2026-09-10 and
        2026-09-14 on a flat config, plain flash accepts a user-role text item
        and answers it, first delta 0.23 s. See the accepts_text_items column
        and docs/migration-plan.md.
        """
        await self._send({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": role,
                     "content": [{"type": "input_text", "text": text}]},
        })

    async def commit_input(self) -> None:
        """Close the participant's turn without asking for a reply. Group rooms
        need this separately: one commit, then a reply per speaker."""
        self.pending_input = 0
        # A commit is a new participant turn: the retry budget starts over and
        # a barge-in on the previous reply no longer describes the next one.
        # This is the ONLY place the budget is reset - not on a reply the
        # gateway starts by itself, because on this gateway that is what the
        # retry's own answer looks like once the phantom done has been read as
        # a boundary (see _response_created_id), and a budget reset there is a
        # loop.
        self._retries_this_turn = 0
        self._cancelled_by_us = False
        self._drop_deferred("new_turn")
        await self._send({"type": "input_audio_buffer.commit"})

    @property
    def responding(self) -> bool:
        """True while a reply is in flight. A group sequencer must wait for this
        to clear before handing the floor to the next character, the gateway
        rejects a second response.create with
        conversation_already_has_active_response.

        Reports the raw flag, including one that has gone stale, because a
        caller waiting on a reply should still see it and time out on its own
        terms; it is commit_turn/request_response that refuse to be blocked by
        a flag older than any reply (see _response_stalled)."""
        return self._response_active

    def _end_response(self) -> None:
        """Drop every trace of an in-flight reply.

        One clearing path for all four flags, called from each way a response
        can end — done, error, cancel, timeout, or the socket going away. They
        went out of sync once (a cancelled reply left autofire_active latched
        and deadlocked the encounter, see cancel_response), and each new piece
        of state here is another chance to do it again, so there is exactly one
        place that puts them all down."""
        self._response_active = False
        self.autofire_active = False
        self._response_started_at = 0.0
        self._response_saw_output = False
        self._response_audio_seen = False
        self._response_audio_closed = False
        self._response_text = ""
        self._response_audio_bytes = 0
        self._retry_in_flight = False
        self._replay_in_flight = False
        self._audio_absent_hold = 0.0
        self._response_created_id = None
        self._requested = False

    def _request_unanswered(self) -> bool:
        """True when a reply this bridge asked for has drawn nothing at all —
        not a response.created, not a delta — for REQUEST_UNANSWERED_S.

        Only for requests, never for the gateway's own replies (those have
        output by definition), and never for a retry (that is
        _retry_unanswered's case, on the tighter audio bar). Held while the
        participant is talking, like the other clocks: Gemini withholds a
        reply while it hears speech."""
        if not (self._response_active and self._requested
                and not self._response_saw_output
                and not self._retry_in_flight
                and self._response_created_id is None):
            return False
        if self._participant_speaking():
            self._audio_absent_hold = time.time()
            return False
        since = max(self._response_started_at, self._audio_absent_hold)
        return since > 0.0 and (time.time() - since) > REQUEST_UNANSWERED_S

    # -- audio recovery --------------------------------------------------------
    def _drop_deferred(self, why: str) -> None:
        """A held truncation verdict has been overtaken - by a new participant
        turn, a barge-in, or a reply the gateway started itself. It is still
        yielded, at the next wake, so the runner closes the turn; it is just
        no longer retryable, and `why` says what overtook it."""
        if self._deferred is not None and self._deferred.get("why") is None:
            self._deferred["why"] = why

    def _deferral_verdict(self) -> Optional[dict]:
        """The response_done for a held truncation verdict, once it is
        settled; None while the quiet window is still running.

        Settled means one of: something overtook it (see _drop_deferred), the
        participant was heard during the window, or the window passed with
        nothing on the microphone and nothing from the gateway - the only
        case that is offered for retry."""
        d = self._deferred
        if d is None:
            return None
        now = time.time()
        why = d.get("why")
        if why is None:
            if self._participant_speaking():
                why = "participant_speaking"
            elif now < d["quiet_until"]:
                return None
        self._deferred = None
        ev = {"type": "response_done", "audio_unterminated": d["unterminated"],
              "retried": d["retried"], **d["verdict"],
              "held_s": round(now - d["since"], 2)}
        if why is not None:
            ev["retryable"] = False
            ev["not_retryable_why"] = why
        return ev

    def _participant_speaking(self) -> bool:
        hook = self.participant_speaking
        if hook is None:
            return False
        try:
            return bool(hook())
        except Exception:  # noqa: BLE001 - a broken hook must not end the pump
            return False

    def _audio_absent_since(self) -> float:
        return max(self._response_started_at, self._last_output_at,
                   self._audio_absent_hold)

    def _absent_bar(self) -> float:
        """The seconds of nothing that call THIS reply lost, on THIS family.

        RESPONSE_STALL_S, AUDIO_ABSENT_S and REPLAY_UNANSWERED_S are all single
        globals measured on nto.gemini-live-2.5-flash, and they stayed single
        globals through the merge while the autofire wait beside them became a
        per-family column -- which is how the replay bar ended up SHORTER than
        the deployed route's own reply latency: REPLAY_UNANSWERED_S is 4.0 s and
        the native-audio row's autofire_wait is 4.5 s, so a replayed turn there
        was declared unanswered and its session rebuilt 0.5 s before the file
        next door says that route starts speaking.

        A reply cannot be lost before the family's own floor for producing one,
        so the bar is floored by that. The row's value is read directly and not
        through autofire_wait_for_model: AUTOFIRE_WAIT is an operator knob for
        the turn-taking wait and must not silently move a recovery bar with it.

        The other two bars are NOT probed on native-audio and are left where the
        measurement put them; that is recorded in the row and in
        docs/migration-plan.md rather than guessed at here.
        """
        if not self._replay_in_flight:
            return AUDIO_ABSENT_S
        caps = capabilities_for(self.model)
        return max(REPLAY_UNANSWERED_S, caps.autofire_wait if caps else 0.0)

    def _audio_watching(self) -> bool:
        """True while the AUDIO_ABSENT_S clock is running: a reply that has
        produced something (words without voice yet, or voice that has since
        gone quiet), or a retry we issued that has produced nothing yet."""
        return self._response_active and (
            self._response_saw_output or self._retry_in_flight)

    def _output_stalled(self) -> bool:
        """True when a reply's audio has arrived and then NOTHING has, for
        AUDIO_ABSENT_S - no delta and no response.done. The third shape the
        constant describes; held while the participant talks, like the other
        two."""
        if not (self._response_active and self._response_saw_output
                and self._response_audio_seen):
            return False
        if self._participant_speaking():
            self._audio_absent_hold = time.time()
            return False
        return (time.time() - self._audio_absent_since()) > AUDIO_ABSENT_S

    def _retry_unanswered(self) -> bool:
        """True when the turn's retry has produced nothing for AUDIO_ABSENT_S.

        A retry is our own request with a known answer time - 0.3 s to the
        first delta whenever the gateway answered one live - so an unanswered
        retry is not RESPONSE_STALL_S's case: leaving it to the 45 s watchdog
        measured 61 s of dead air per stall, worse than the 47 s it replaced.
        Held while the participant talks, like _audio_absent."""
        if not (self._response_active and self._retry_in_flight
                and not self._response_saw_output):
            return False
        if self._participant_speaking():
            self._audio_absent_hold = time.time()
            return False
        bar = self._absent_bar()
        return (time.time() - self._audio_absent_since()) > bar

    def _audio_absent(self) -> bool:
        """True when a reply's transcript arrived and its audio has not, for
        longer than first audio ever takes on this gateway (AUDIO_ABSENT_S),
        counted from the last sign of life and held while the participant is
        talking. See the constant for the measurement behind the bar."""
        if not (self._response_active and self._response_saw_output
                and not self._response_audio_seen):
            return False
        if self._participant_speaking():
            self._audio_absent_hold = time.time()
            return False
        return (time.time() - self._audio_absent_since()) > AUDIO_ABSENT_S

    def _audio_metrics(self) -> dict:
        ms = int(round(self._response_audio_bytes / 2 / GATEWAY_OUTPUT_RATE * 1000))
        return {"words": _word_count(self._response_text), "audio_ms": ms,
                "text": self._response_text}

    def _truncated(self, unterminated: bool) -> bool:
        """The words-per-second shortfall detector.

        Fires only for a reply whose audio stream the gateway never closed
        (`unterminated`, the defect's own signature) AND whose delivered audio
        is under half of what its own transcript needs at a spoken rate, with
        at least four words and at least a second missing - turn_audio's rule,
        so the live decision and the post-hoc scan agree on what "short" means.
        Proven not to fire on the shortest legitimate replies: a one- or
        two-word line has no ratio worth reading and is excluded by the word
        floor. The `unterminated` gate is load-bearing, not belt and braces:
        properly CLOSED replies under the half-ratio do occur live (0.35-0.49
        of expected, measured on replies a barge-in cancelled and on a head
        glued to a resumed reply), so the ratio alone would fire on them, and
        it is the gateway's own missing output_audio.done that says this one
        stopped rather than finished.

        Even then the verdict is not final here: a retryable one is HELD for
        AUDIO_RETRY_QUIET_S in events(), because the same signature is what the
        gateway produces when it interrupts a reply for speech it heard.
        """
        if not unterminated:
            return False
        return _audio_shortfall(self._response_text,
                                self._audio_metrics()["audio_ms"]) is not None

    def _retry_verdict(self, reason: Optional[str]) -> dict:
        """What the runner needs to decide on and record a retry: the reason a
        detector fired (None when neither did, or when this reply was one we
        cancelled - a barge-in is not a truncation), whether the turn's one
        retry is still available, and the reply's own numbers."""
        if reason is None or self._cancelled_by_us:
            return {"retry_reason": None, "retryable": False}
        out = {"retry_reason": reason, "retryable": True}
        out.update(self._audio_metrics())
        if (out["words"] > AUDIO_RETRY_MAX_WORDS
                or out["audio_ms"] > AUDIO_RETRY_MAX_AUDIO_MS):
            # A runaway, not a loss: see AUDIO_RETRY_MAX_WORDS.
            out["retryable"] = False
            out["not_retryable_why"] = "oversized"
        elif self._retries_this_turn >= AUDIO_RETRY_LIMIT:
            out["retryable"] = False
            out["not_retryable_why"] = "already_retried"
        return out

    async def retry_response(self, nudge: Optional[str] = None) -> bool:
        """Ask the gateway for this turn's reply again, once.

        `nudge` is the user text item to put in front of the model; the
        default is AUDIO_RETRY_NUDGE, written for a reply whose voice was
        lost. A reply that never began gets UNANSWERED_NUDGE from the runner
        instead, because "say it again" is the wrong prompt for a character
        who has not said anything.

        The recovery for both audio defects: a reply whose audio stopped short
        of its own transcript, or never came at all, is dropped and ONE fresh
        response is requested for the same participant turn.

        The recipe is a cancel, a user TEXT item (AUDIO_RETRY_NUDGE) and a
        response.create, and it is that and not the bridge's usual
        silence-commit-create because the usual one was measured not to work
        here. Every abandoned reply seen live came back only when the
        participant next spoke; the obvious reading - that a commit and a
        create are what revive it - was tried and is false: after a dead reply
        a commit of 300 ms of silence produced nothing, a commit of 1.2 s of
        room noise produced nothing, and both turns fell to the 45 s watchdog
        (61 s of dead air, worse than the 47 s they were meant to fix). What
        the participant's utterance supplies is new CONTENT for the model to
        answer, and a text item supplies it too: first delta 0.23 s after the
        create, a complete reply with its audio stream closed. So the retry
        does at 8 s, with a prompt the record shows, what a real participant
        would otherwise do by accident after 45 s of dead air.

        Bounded by AUDIO_RETRY_LIMIT per turn, by count and not by hope: False
        means the turn has had its retry and the caller finalises the reply as
        truncated with the existing detection. The floor check and the
        interruption exclusion are the caller's - see the runner - because the
        room's floor and the participant's VAD live there.
        """
        if self._retries_this_turn >= AUDIO_RETRY_LIMIT or self.ws is None:
            return False
        # Harmless when nothing is active; on a family where the dead reply is
        # still nominally open it is the cancel that lets the create through.
        # Sent directly rather than via cancel_response: this is not a barge-in
        # and must not mark the NEXT reply as one we cut off.
        await self._send({"type": "response.cancel"})
        if self._response_created_id:
            # An absent reply is still nominally open under its own id; its
            # done, if the cancel draws one, is that reply's and not the
            # retry's.
            self._cancelled_ids.add(self._response_created_id)
        self._end_response()
        self._deferred = None
        self._agent_buffer = ""
        self._cancelled_by_us = False
        self._retries_this_turn += 1
        self._retry_in_flight = True
        await self.prompt_response(nudge or AUDIO_RETRY_NUDGE)
        return True

    async def replay_input(self, pcm16: bytes) -> bool:
        """Put the participant's own recent speech in front of the model again
        and ask once more, on the turn's one retry.

        The recovery for a commit + response.create the gateway simply never
        answered (`reply_missing`), and for the line that was in flight when
        the gateway died under an encounter. Measured live (2026-09-14): the
        text-item nudge does draw a reply from a socket that ignored one
        create, but the reply answers the nudge, not the participant — a
        "Good morning." that the gateway dropped came back as a SECOND scene
        opening ("You booked this meeting. What's on your mind."). What the
        participant's own utterance supplies is content the model answers as
        itself, so the retry hands the gateway that utterance again: the
        runner keeps the participant's audio since the last transcript the
        gateway returned (see RealtimeVoiceSessionRunner._replay_speech,
        which also compacts the silences out of it) and this appends it,
        commits it and asks.

        Counted against the same AUDIO_RETRY_LIMIT as a nudge, and the commit
        here is deliberately NOT commit_input: that method resets the retry
        budget for a new participant turn, and a replay that reset it would
        be a loop. False when the budget is spent, the socket is gone, or
        there is nothing to replay; the caller then records why.
        """
        if (self._retries_this_turn >= AUDIO_RETRY_LIMIT or self.ws is None
                or not pcm16):
            return False
        await self._send({"type": "response.cancel"})
        if self._response_created_id:
            self._cancelled_ids.add(self._response_created_id)
        self._end_response()
        self._deferred = None
        self._agent_buffer = ""
        self._cancelled_by_us = False
        self._retries_this_turn += 1
        self._retry_in_flight = True
        self._replay_in_flight = True
        # Appended in 100 ms pieces, the size the runner's own pump sends, so
        # a gateway that sizes its buffers on the frames it sees is not handed
        # one 40 s frame.
        step = CLIENT_RATE * 2 // 10
        for i in range(0, len(pcm16), step):
            await self.send_audio(pcm16[i:i + step])
        self.pending_input = 0
        await self._send({"type": "input_audio_buffer.commit"})
        self._response_active = True
        self._requested = True
        self._response_started_at = time.time()
        self._response_saw_output = False
        await self._send({"type": "response.create"})
        return True

    async def prompt_response(self, text: str) -> None:
        """Put a user TEXT item in front of the model and ask it to reply.

        The recipe behind every recovery on this gateway, and behind the
        group scene open on Gemini (see SCENE_OPEN_PROMPT): a text item is new
        CONTENT for the model to answer, which is what a commit of silence is
        not. No cancel here — callers that need one (retry_response) send it
        first — and no budget: the callers keep the books. The reply it draws
        is a requested one (`_requested`), so REQUEST_UNANSWERED_S watches it
        like any other.
        """
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        })
        self._response_active = True
        self._requested = True
        self._response_started_at = time.time()
        self._response_saw_output = False
        await self._send({"type": "response.create"})

    def _response_stalled(self) -> bool:
        """True when a reply is nominally in flight but has produced nothing
        for longer than any reply plausibly takes.

        Measured from the last sign of life, not from the request, so a long
        reply that is still streaming audio is never mistaken for a dead one.
        A stall is what a lost response.done looks like from here, and it is
        what the module docstring's "alive but permanently mute" session looks
        like too."""
        if not self._response_active:
            return False
        last = max(self._response_started_at, self._last_output_at)
        return last > 0.0 and (time.time() - last) > RESPONSE_STALL_S

    def clear_response_state(self) -> None:
        """Force the in-flight flag down after a timeout, so one stalled reply
        cannot mute every character that follows it. The auto-fire latch goes
        with it: a reply that timed out will not emit the response.done that
        would otherwise clear it, and leaving it up makes commit_turn() skip
        the very commit this call exists to unblock."""
        self._end_response()

    async def request_response(self) -> None:
        """Ask the current character to speak."""
        if self._response_active and not self._response_stalled():
            return
        # A stale flag is overridden rather than obeyed. events() normally
        # times the stall out first and says so; this is the backstop for a
        # session whose events() is not being drained at that moment, so that
        # the worst a lost response.done can cost is one turn rather than
        # every turn that follows it.
        self._response_active = True
        self._requested = True
        self._response_started_at = time.time()
        self._response_saw_output = False
        await self._send({"type": "response.create"})

    async def commit_turn(self) -> None:
        """Close the participant's turn and ask for a reply. Required, the
        gateway will not do this on its own."""
        if self._response_active and not self._response_stalled():
            return
        if self.autofire_active and time.time() - self._last_output_at < 15:
            # The bridge is already answering this turn; a commit + create
            # here produces a second, paraphrased reply on top of it.
            return
        if self.pending_input < 3200:
            # Committing an empty buffer kills the session on this bridge;
            # pad with 300 ms of silence if an auto-fire consumed the audio.
            await self.send_audio(b"\x00" * 9600)
        await self.commit_input()
        if is_openai_realtime(self.model):
            # Through the bridge, the commit itself starts the reply on the
            # OpenAI route; an explicit response.create on top is rejected
            # (active-response conflict) and can yield a second reply.
            self._response_active = True
            # ...but the reply still needs a stall clock, or _response_stalled
            # has nothing to measure from and a reply this route loses latches
            # _response_active for the rest of the encounter. request_response
            # sets these three on every other route; this branch returns before
            # reaching it, so it sets them itself.
            self._requested = True
            self._response_started_at = time.time()
            self._response_saw_output = False
            return
        await self.request_response()

    async def cancel_response(self) -> None:
        """Barge-in: stop the agent mid-utterance. Sent unconditionally, because
        response.cancel is harmless when nothing is active and a bridge
        auto-fired reply has to be cancellable too.

        The reply's whole state is dropped here (_end_response). A cancelled
        response may never produce the
        response.done that would otherwise clear autofire_active, and a latched
        autofire_active deadlocks the encounter: the runner's turn loop reads
        the flag, believes the bridge is already answering, and skips
        commit_turn() — so no new response is ever created, so no response.done
        ever arrives, and the agent goes permanently silent after one barge-in.
        """
        await self._send({"type": "response.cancel"})
        # Read at this reply's response.done (which on this gateway arrives bare,
        # exactly like a truncation) so a line the participant chose to cut off
        # is never re-spoken to them. The id is what does this on a gateway
        # that names its replies - whenever that done arrives, even after the
        # participant's next commit; the flag is the fallback for one that
        # does not. See _response_created_id and _retry_verdict.
        if self._response_created_id:
            self._cancelled_ids.add(self._response_created_id)
        self._cancelled_by_us = True
        self._drop_deferred("cancelled")
        self._end_response()

    def _to_client_rate(self, pcm: bytes) -> bytes:
        if GATEWAY_OUTPUT_RATE == CLIENT_RATE:
            return pcm
        converted, self._resample_state = _ratecv(
            pcm, GATEWAY_OUTPUT_RATE, CLIENT_RATE, self._resample_state
        )
        return converted

    async def _frames(self) -> AsyncIterator[Optional[str]]:
        """Raw gateway frames, with a None every RECV_POLL_S of quiet.

        Two reasons this is recv() in a loop rather than `async for raw in
        self.ws`. The None is one: it gives events() a heartbeat on which to
        notice a reply that has stopped producing, which a blocking read never
        would. The other is that the websockets iterator swallows a clean close
        and simply stops — the exact silent ending this module must not have —
        while recv() raises ConnectionClosedOK and can be reported.

        The socket is captured once: close() drops self.ws to None right after
        closing it, and reading the attribute per frame would turn a teardown
        race into an AttributeError out of the pump."""
        ws = self.ws
        while True:
            timeout = RECV_POLL_S
            if self._deferred is not None:
                # A held truncation verdict is re-examined every quarter
                # second: the participant's first hinted frame, or the end of
                # the quiet window, is acted on within that.
                left = self._deferred["quiet_until"] - time.time()
                timeout = min(timeout, 0.25, max(0.02, left + 0.01))
            if self._audio_watching():
                # Wake when the audio-absent bar is reached rather than up to a
                # whole poll interval later: a participant sitting in dead air
                # is charged for every one of those seconds.
                # ...and no less than once a second, so the hold that the
                # participant's own talking puts on the clock is released within
                # a second of them stopping rather than a poll interval later.
                bar = self._absent_bar()
                left = bar - (time.time() - self._audio_absent_since())
                timeout = min(timeout, 1.0, max(0.25, left + 0.01))
            elif (self._response_active and self._requested
                    and not self._response_saw_output):
                # The same for a request that has drawn nothing yet: measured
                # live, a 6 s bar checked every 5 s fired at 8-11 s.
                left = REQUEST_UNANSWERED_S - (time.time() - max(
                    self._response_started_at, self._audio_absent_hold))
                timeout = min(timeout, 1.0, max(0.25, left + 0.01))
            try:
                yield await asyncio.wait_for(ws.recv(), timeout)
            except asyncio.TimeoutError:
                yield None

    async def events(self) -> AsyncIterator[dict]:
        if not self.ws:
            raise RuntimeError("connect() first")
        # Set from whichever exception ends the loop, and yielded on the way
        # out. See the tail of this method: no exit from here is silent.
        closing = "the gateway closed the realtime session"
        # Says that somebody is reading this socket, which is what makes an ack
        # observable at all. update_instructions answers None while this is
        # down, rather than mistaking an undrained session for a refused brief.
        self._events_running = True
        try:
            async for raw in self._frames():
                pending = self._deferral_verdict()
                if pending is not None:
                    # A truncation verdict whose quiet window has run out, or
                    # that something overtook (see AUDIO_RETRY_QUIET_S). Only
                    # the first kind is offered for retry.
                    yield pending
                if self._audio_absent():
                    # The reply's words came and its voice did not (see
                    # AUDIO_ABSENT_S). Checked on every wake, not only on quiet
                    # ones, because the wire can carry the participant's
                    # transcription frames through the whole of a dead reply.
                    # The turn is closed out exactly as the general stall below
                    # closes it, and the runner is told it may ask once more.
                    waited = round(time.time() - self._audio_absent_since())
                    verdict = self._retry_verdict("absent")
                    retried = self._retry_in_flight
                    self._end_response()
                    self._cancelled_by_us = False
                    yield {"type": "response_done", "interrupted": True,
                           "audio_absent": True, "retried": retried,
                           "waited_s": waited, **verdict}
                    if not verdict["retryable"]:
                        yield {
                            "type": "error", "recoverable": True,
                            "message": (
                                f"the gateway sent the words of a reply and "
                                f"no voice for {waited}s; the turn was cut off"
                            ),
                        }
                if self._output_stalled():
                    # The reply's voice arrived and the gateway then went
                    # silent without ever closing it (see AUDIO_ABSENT_S). The
                    # turn is closed as heard: a delivery short of its words is
                    # a truncation and offered for retry like any other, one
                    # that is whole is simply a reply whose done never came,
                    # and the participant's next turn must not wait 45 s on it.
                    waited = round(time.time() - self._audio_absent_since())
                    unterminated = not self._response_audio_closed
                    verdict = self._retry_verdict(
                        "truncated" if self._truncated(unterminated) else None)
                    retried = self._retry_in_flight
                    self._end_response()
                    self._cancelled_by_us = False
                    cut = verdict.get("retry_reason") is not None
                    yield {"type": "response_done", "interrupted": cut,
                           "audio_unterminated": unterminated,
                           "output_stalled": True, "retried": retried,
                           "waited_s": waited, **verdict}
                    if cut and not verdict.get("retryable"):
                        yield {
                            "type": "error", "recoverable": True,
                            "message": (
                                f"the gateway stopped mid-reply and sent "
                                f"nothing for {waited}s; the turn was cut off "
                                "mid-sentence"
                            ),
                        }
                if self._retry_unanswered():
                    # The one retry this turn gets went unanswered. Give the
                    # turn back to the participant now rather than after
                    # RESPONSE_STALL_S: the words they were shown are recorded
                    # as a cut-off turn, the retry's outcome as not recovered.
                    waited = round(time.time() - self._audio_absent_since())
                    self._end_response()
                    self._cancelled_by_us = False
                    yield {"type": "response_done", "interrupted": True,
                           "audio_absent": True, "retried": True,
                           "retry_reason": None, "retryable": False,
                           "waited_s": waited}
                    yield {
                        "type": "error", "recoverable": True,
                        # The runner reads this flag: a socket that has now
                        # ignored a request AND its retry is, on every live
                        # measurement, a socket the gateway is about to drop
                        # (22-38 s later, no close frame). The 1:1 runner
                        # rebuilds the session at once rather than sitting
                        # out that wait, and replays the participant's
                        # unanswered speech into the new one.
                        "retry_unanswered": True,
                        "waited_s": waited,
                        "message": (
                            f"the gateway did not answer the retry for {waited}s; "
                            "the turn was abandoned"
                        ),
                    }
                if self._request_unanswered():
                    # A reply we asked for and never heard the first frame of
                    # (see REQUEST_UNANSWERED_S). No turn was ever begun, so
                    # there is nothing to finalise; the latch comes down so the
                    # participant's next turn is not refused, and the runner is
                    # told it may ask once more — with a prompt, since a commit
                    # of silence is what just went unanswered.
                    waited = round(time.time() - max(
                        self._response_started_at, self._audio_absent_hold))
                    self._end_response()
                    yield {"type": "reply_missing", "waited_s": waited,
                           "retryable": (self._retries_this_turn
                                         < AUDIO_RETRY_LIMIT)}
                if raw is None:
                    # Quiet wire. That is ordinary between turns, and a fault
                    # only when a reply we asked for has produced nothing at
                    # all: a lost response.done leaves _response_active latched
                    # and every later commit_turn() is a silent no-op, so the
                    # participant goes on talking to a character that will
                    # never answer again — the encounter is muted for good and
                    # nothing anywhere says why.
                    if not self._response_stalled():
                        continue
                    waited = round(
                        time.time()
                        - max(self._response_started_at, self._last_output_at)
                    )
                    partial = self._response_saw_output
                    retried = self._retry_in_flight
                    self._end_response()
                    if partial:
                        # Part of the reply was spoken to the participant and
                        # is already in the assistant WAV. Close that turn so
                        # the words survive in the transcript rather than
                        # vanishing from it, or being glued onto the front of
                        # whatever this character says next.
                        yield {"type": "response_done", "interrupted": True,
                               "retried": retried}
                    yield {
                        # `recoverable`: the encounter goes on past this. The
                        # runner relays it as a notice, not as an `error` the
                        # page would read as the server declaring the
                        # encounter failed (see _pump_events in the runner).
                        "type": "error", "recoverable": True,
                        # Two different failures, and the message says which.
                        # "No reply" was false whenever part of one had already
                        # arrived. The runner now also carries the `interrupted`
                        # flag above onto the turn itself, so the record says
                        # the same thing twice, in the two channels an analyst
                        # reads separately (see the module docstring).
                        "message": (
                            f"the gateway stopped mid-reply and sent nothing "
                            f"for {waited}s; the turn was cut off "
                            f"mid-sentence"
                            if partial else
                            f"no reply from the gateway after {waited}s; "
                            "abandoning the turn"
                        ),
                    }
                    continue
                try:
                    ev = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                etype = ev.get("type", "")
                if self.debug_log is not None:
                    self.debug_log.append((time.time(), etype, str(ev)[:160]))

                if etype == "session.updated":
                    # The one frame anywhere that proves a brief landed. Handed
                    # to whoever is waiting in update_instructions, and of no
                    # interest to the runner, so it is not yielded on.
                    self._update_ack.set()
                    continue

                # The bridge auto-fires responses without going through
                # request_response(). Detect that first — an auto-fired reply
                # is one whose deltas arrive while _response_active is still
                # False — then mark the session active anyway, so responding()
                # and cancel_response() track auto-fired replies too. Order
                # matters: setting _response_active before the autofire test
                # would make every reply look like one we asked for.
                if etype == "response.created":
                    # A reply the gateway is starting is not the one we cut off.
                    self._cancelled_by_us = False
                    if (not self._response_active
                            and autofire_visible_at_created(self.model)):
                        # origin/main 210fbfc: on the gpt route the first audio
                        # can trail response.created by several seconds, and a
                        # commit + response.create sent in that gap is rejected
                        # as an active-response conflict. So on the families
                        # where created is the signal, mark the auto-fire here
                        # rather than at the first delta -- and start its stall
                        # clock, because a reply nobody asked for has no other.
                        #
                        # Gated per family rather than taken unconditionally:
                        # on plain flash ours is the measurement, a created that
                        # never becomes a delta does happen there, and a latched
                        # autofire_active mutes the encounter for good (see
                        # cancel_response).
                        self.autofire_active = True
                        self._last_output_at = time.time()
                        if not self._response_started_at:
                            self._response_started_at = self._last_output_at
                    if self._deferred is not None:
                        # The gateway resumed on its own behind a bare done:
                        # that is what it does after interrupting itself for
                        # speech it heard, once the speech stops. The held
                        # verdict is closed out first so the head's turn ends
                        # before this reply's first delta opens the next.
                        self._drop_deferred("gateway_resumed")
                        pending = self._deferral_verdict()
                        if pending is not None:
                            yield pending
                    rid = (ev.get("response") or {}).get("id")
                    if rid:
                        # This reply is now the one in flight - whether we
                        # asked for it (request_response, retry_response) or
                        # the gateway started it. Every check on a later done
                        # is against this name; see _response_created_id.
                        self._names_replies = True
                        self._created_ids.add(rid)
                        if rid != self._response_created_id:
                            # A NEW reply: its own words and its own audio
                            # start from nothing, whatever the previous one
                            # left (a reply the gateway starts without ever
                            # closing the last one). This is also what makes
                            # the next transcript chunk `first` (see the delta
                            # branch), so a continuation is never glued to a
                            # head.
                            self._response_text = ""
                            self._agent_buffer = ""
                            self._response_audio_bytes = 0
                            self._restreaming = False
                        self._response_created_id = rid

                if etype.startswith("response.") and etype.endswith(".delta"):
                    self._last_output_at = time.time()
                    drid = ev.get("response_id")
                    if drid and self._response_created_id is None:
                        # Output under a name with no response.created since
                        # the reply in flight began: the gateway is continuing
                        # a reply this bridge had closed out. Measured live in
                        # two shapes - a retry's answer arrives as the
                        # abandoned reply resuming under its own id (no new
                        # created at all), and a reply we cancelled keeps
                        # streaming. Bound here so its done can end it; read as
                        # stale, that done left the flags up for 45 s.
                        self._names_replies = True
                        self._created_ids.add(drid)
                        self._done_ids.discard(drid)
                        self._response_created_id = drid
                    if not self._response_active:
                        self.autofire_active = True
                        # An auto-fired reply starts its own stall clock: it was
                        # never requested, so nothing else has started one, and
                        # a reply with no clock can never be timed out. The
                        # retry budget is deliberately NOT reset here: see
                        # commit_input.
                        self._response_started_at = self._last_output_at
                    self._response_active = True
                    # This reply has been heard. Only such a reply is worth
                    # closing out when the socket or the gateway abandons it
                    # mid-turn; one that produced nothing has no turn to write.
                    self._response_saw_output = True

                if etype in ("response.output_audio.delta", "response.audio.delta"):
                    self._response_audio_seen = True
                    try:
                        pcm = base64.b64decode(ev.get("delta") or "")
                        self._response_audio_bytes += len(pcm)
                    except (binascii.Error, ValueError) as exc:
                        # One malformed audio frame must cost one chunk, not
                        # the encounter. Unguarded, this raise leaves events()
                        # entirely, trips run()'s finally in the runner and
                        # drops the participant mid-conversation — loud on
                        # screen, but with nothing in the record saying why.
                        # Reported per chunk rather than counted: a gateway
                        # sending corrupt audio at all is not a normal
                        # condition, and a row per occurrence is the record an
                        # analyst needs to see how much audio was lost.
                        #
                        # `transient` says the session survived this: one chunk
                        # of audio is gone, the turn is not. The per-chunk store
                        # row is the point and stays; what must NOT be per-chunk
                        # is the participant-visible frame the runner builds
                        # from it, because deltas arrive every few tens of
                        # milliseconds and a corrupt run would fill the page
                        # with dozens of identical error banners mid-encounter.
                        # See _pump in server/realtime_voice_session.py, which
                        # shows at most one such notice per reply.
                        yield {
                            "type": "error",
                            "transient": True,
                            "message": (
                                "discarded a corrupt audio frame from the "
                                f"gateway: {exc}"
                            ),
                        }
                        continue
                    if pcm:
                        yield {"type": "agent_audio", "pcm": self._to_client_rate(pcm),
                               "response_id": ev.get("response_id")}

                elif etype in (
                    "response.output_audio_transcript.delta",
                    "response.audio_transcript.delta",
                ):
                    delta = ev.get("delta") or ""
                    if self._restreaming:
                        # See below: the rest of a transcript the gateway is
                        # sending a second time. Swallowed; the .done frame
                        # carries the line once and replaces the buffer.
                        continue
                    probe = delta.strip()
                    if (self._response_text and len(probe) >= 12
                            and len(self._response_text) > len(probe) + 8
                            and self._response_text.startswith(probe)):
                        # THE DOUBLED CAPTION (measured live 2026-09-14, S3C
                        # room, Rafa): inside ONE response id the gateway
                        # streamed the whole transcript, then streamed it
                        # again from the first word — "I stopped keeping the
                        # workarounds up..." twice under resp_04b2f659 — and
                        # the participant's screen showed the line doubled
                        # while the record, which takes the .done frame as
                        # authoritative, kept it once. A delta that restarts
                        # the reply's own opening words is that second pass,
                        # and nothing of it is relayed.
                        self._restreaming = True
                        self.transcript_restreams += 1
                        yield {"type": "transcript_restreamed"}
                        continue
                    # `first`: the opening chunk of a reply as this bridge
                    # counts replies — after a response.created, a cancel, a
                    # commit or a retry. Chunks within one reply are joined
                    # with nothing between them, correctly, because a chunk
                    # boundary usually falls mid-word; the boundary between
                    # a reply the participant cut off and the gateway's
                    # continuation of it (cancel is inert on Gemini) is not
                    # such a seam, and joining across it is how "or notIt
                    # needed to be said" reached the record. The runner puts
                    # the space back at exactly these boundaries and nowhere
                    # else.
                    first = not self._response_text
                    self._agent_buffer += delta
                    self._response_text += delta
                    yield {"type": "agent_transcript_delta", "text": delta,
                           "first": first,
                           "response_id": ev.get("response_id")}

                elif etype in (
                    "response.output_audio_transcript.done",
                    "response.audio_transcript.done",
                ):
                    text = (ev.get("transcript") or self._agent_buffer).strip()
                    self._agent_buffer = ""
                    self._restreaming = False
                    if text:
                        self._response_text = text
                        yield {"type": "agent_transcript", "text": text}

                elif etype == "conversation.item.input_audio_transcription.completed":
                    raw = (ev.get("transcript") or "").strip()
                    # The gateway's transcriber writes a literal "{}" where it
                    # heard speech it could not transcribe — the "{}" the
                    # researcher saw inside their own lines ("...from your uh
                    # {} likely don't appreciate it"), reproduced live: "{}
                    # Isle Do what I can." for "side... Uh, I'll do what I
                    # can." There is no "{}" in any source of ours; it is the
                    # transcriber's placeholder, and it must not be recorded
                    # as something the participant said. Scrubbed here, at the
                    # one door every participant transcript comes through, and
                    # flagged so the record says a piece of the line is
                    # missing rather than pretending the line was whole.
                    text = _PLACEHOLDER.sub(" ", raw)
                    text = re.sub(r"\s{2,}", " ", text).strip()
                    garbled = text != raw
                    if text:
                        yield {"type": "user_transcript", "text": text,
                               "garbled": garbled}
                    elif raw:
                        # Nothing but placeholders: the participant spoke and
                        # none of it was transcribed. Said as such.
                        yield {"type": "user_transcript", "text": "",
                               "garbled": True}

                elif etype in ("response.output_audio.done",
                               "response.audio.done"):
                    # The frame that says the audio stream for this reply ended
                    # on purpose. Its ABSENCE behind a response.done is the
                    # gateway abandoning the stream mid-sentence; see
                    # _response_audio_closed. Not yielded — the runner has never
                    # needed a second end-of-reply boundary and giving it one
                    # would be a new way to finalise a turn twice.
                    self._response_audio_closed = True
                    continue

                elif etype == "response.done":
                    # The gateway can repeat response.done for one reply; emit
                    # it once per response id. Checked BEFORE the flags come
                    # down: the repeat can arrive seconds after the first (3.6 s
                    # measured), by which time the reply it names is long over
                    # and the flags belong to the NEXT reply - on this gateway
                    # the retry that replaces a truncated one - which a late
                    # duplicate must not end mid-stream.
                    rid = (ev.get("response") or {}).get("id") or ev.get("response_id")
                    if rid and rid in self._done_ids:
                        continue
                    if rid and self._names_replies and rid not in self._created_ids:
                        # Never response.created on this socket: the phantom
                        # that shadows every bare done here (see
                        # _response_created_id). It names no reply, so it ends
                        # none - above all not the retry that may have just
                        # been issued and is waiting for its own created.
                        self.phantom_dones += 1
                        continue
                    current = (not rid or not self._names_replies
                               or rid == self._response_created_id
                               # Output arrived under no new name since the
                               # reply in flight began (see the delta branch):
                               # whatever this bridge asked for, THIS is the
                               # reply that answered, and this is its end.
                               or (self._response_created_id is None
                                   and self._response_active
                                   and self._response_saw_output))
                    cancelled = rid in self._cancelled_ids
                    if rid:
                        self._done_ids.add(rid)
                        self._created_ids.discard(rid)
                        self._cancelled_ids.discard(rid)
                    if not current:
                        # A reply that is over from this bridge's point of view
                        # - cancelled by us, or closed out by a watchdog - and
                        # whose done the gateway has only now got round to.
                        # Handed on marked, so a pump can drop a latch it holds
                        # on that reply, and never as a boundary of the reply
                        # now streaming.
                        self.stale_dones += 1
                        yield {"type": "response_done", "stale": True,
                               "audio_unterminated": False, "retried": False,
                               "retry_reason": None, "retryable": False}
                        continue
                    # Read BEFORE _end_response puts the flags down.
                    unterminated = (self._response_audio_seen
                                    and not self._response_audio_closed)
                    if cancelled:
                        # The reply we cancelled, ending at last - after the
                        # participant's next commit, as often as not. Its name
                        # remembers the cancel when the per-commit flag has
                        # forgotten it: never a truncation.
                        self._cancelled_by_us = True
                    verdict = self._retry_verdict(
                        "truncated" if self._truncated(unterminated) else None)
                    retried = self._retry_in_flight
                    self._end_response()
                    self._cancelled_by_us = False
                    if cancelled:
                        verdict["cancelled"] = True
                    if verdict.get("retryable"):
                        # Not yet. Held for AUDIO_RETRY_QUIET_S and yielded by
                        # _deferral_verdict when the window has settled it.
                        now = time.time()
                        self._deferred = {
                            "verdict": verdict, "retried": retried,
                            "unterminated": unterminated, "since": now,
                            "quiet_until": now + AUDIO_RETRY_QUIET_S,
                        }
                        continue
                    yield {"type": "response_done",
                           "audio_unterminated": unterminated,
                           "retried": retried, **verdict}

                elif etype == "response.function_call_arguments.done":
                    yield {
                        "type": "tool_call",
                        "name": ev.get("name"),
                        "call_id": ev.get("call_id"),
                        "arguments": ev.get("arguments"),
                    }

                elif etype == "error":
                    err = ev.get("error")
                    err = err if isinstance(err, dict) else {}
                    code = str(err.get("code") or "")
                    param = str(err.get("param") or "")

                    if code == "conversation_already_has_active_response":
                        # Not a fault, and above all not the end of a reply.
                        # Both families start a response off the commit alone,
                        # so commit_turn()'s response.create — belt and braces
                        # on a bridge that has been inconsistent about this —
                        # arrives a beat late and is refused. The reply named in
                        # the refusal is the one already streaming and is fine.
                        # Treated as a loss it was actively harmful: seen live
                        # on gpt-realtime-2.1 mid-reply, it would have called
                        # the turn abandoned, closed it out as interrupted, and
                        # filed the second half of the sentence under the next
                        # turn.
                        yield {
                            "type": "error",
                            "transient": True,
                            "message": (
                                "the gateway had already started this reply; "
                                "the extra response.create was refused"
                            ),
                        }
                        continue

                    if code == "invalid_value" and "voice" in param:
                        # Fatal, and fatal on purpose. This is the gateway
                        # saying it threw the WHOLE session.update away: no
                        # session.updated follows, so the character brief never
                        # landed and the actor is about to play the gateway's
                        # stock assistant — fluently, in a healthy-looking
                        # session, for a whole encounter. A persona that never
                        # arrived must not be survivable, so the session is
                        # ended here rather than allowed to sound fine. The
                        # close is marked as ours (see close/_closing) because
                        # the sentence below is the explanation and a second,
                        # vaguer one behind it would only bury it.
                        self._end_response()
                        yield {
                            "type": "error",
                            "fatal": True,
                            "message": (
                                f"the gateway rejected the voice for "
                                f"{self.model}: {err.get('message') or err}. "
                                "It discarded the character brief with it, so "
                                "this session was closed rather than run as a "
                                "stock assistant."
                            ),
                        }
                        await self.close()
                        return

                    # An errored response never reaches response.done either, so
                    # clear the auto-fire latch here too (see cancel_response).
                    # Read whether anything was spoken BEFORE _end_response
                    # zeroes that flag, or the rescue below can never fire.
                    partial = self._response_saw_output
                    self._end_response()
                    if partial:
                        # The third way the gateway abandons a reply mid-
                        # sentence, and the one the stall and close paths did
                        # not cover. Without a response_done the runner never
                        # finalises this turn, so the words already spoken stay
                        # in its buffer and are glued onto the front of
                        # whatever this character says next: one assistant_turn
                        # holding two separate replies under one stage
                        # direction. Verified before this line existed: deltas
                        # "Turn one words." / error / "Turn two words." /
                        # response.done produced a single assistant_turn
                        # reading "Turn one words.Turn two words.".
                        yield {"type": "response_done", "interrupted": True}
                    yield {"type": "error", "message": str(ev.get("error"))}
        except websockets.ConnectionClosedOK as exc:
            # A clean 1000/1001/1005 is not a conclusion. It is what a bridge
            # recycling a connection, a session hitting its duration limit, or
            # a polite quota cut-off looks like, and it used to end this
            # iterator with no event of any kind: the character simply stopped
            # answering, no voice_error, no frame on the participant's screen,
            # nothing in the record to distinguish it from an encounter that
            # ran out of things to say. Report it like any other loss.
            closing = f"realtime session closed by gateway: {exc}"
        except websockets.ConnectionClosedError as exc:
            closing = f"realtime connection lost: {exc}"
        except Exception as exc:  # noqa: BLE001 — deliberate: see the docstring
            # The module docstring promises events() never ends without saying
            # so, and before this handler that promise held only for the two
            # close exceptions: anything else propagated past the trailing
            # yields (they sit after the try, so an exception skips them),
            # leaving _pump and _model_to_client unguarded, tripping run()'s
            # finally, and dropping the participant with no voice_error in the
            # record to explain it. Reported like any other loss instead; repr
            # keeps the exception type, so a bug in here is still diagnosable.
            # CancelledError and GeneratorExit are BaseException and are
            # deliberately NOT caught: a consumer that walked away must not be
            # answered, and a yield during GeneratorExit is an error.
            closing = f"realtime session failed: {exc!r}"
        finally:
            # Whatever ended the loop — a close, an unexpected raise, or the
            # consumer abandoning this generator — no in-flight latch may
            # outlive it. Nothing is yielded from here: a yield during
            # GeneratorExit is an error, and a dead session's flags matter more
            # than its last words.
            partial = self._response_saw_output
            deferred, self._deferred = self._deferred, None
            self._end_response()
            # Nobody is reading the socket from here on, so an ack can no
            # longer be seen: update_instructions must go back to answering
            # None rather than False. Cleared in the finally so it is also
            # cleared on the fatal-voice return above and on a consumer that
            # simply walks away.
            self._events_running = False

        if self._closing:
            # We closed this socket ourselves (a character switch closes the
            # outgoing session on purpose, and so does the end of the
            # encounter). That is an ordinary end of the pump, and reporting it
            # would put a spurious error in the record and on the
            # participant's screen at every interaction boundary. It also must
            # not close out the abandoned turn: by now the runner has already
            # moved on to the next character, and the finalise would file the
            # old character's words under the new one.
            return
        if deferred is not None:
            # A truncation verdict was being held when the socket went: the
            # head the participant heard is still a turn to close, and there
            # is nothing left to retry it on.
            yield {"type": "response_done",
                   "audio_unterminated": deferred["unterminated"],
                   "retried": deferred["retried"], **deferred["verdict"],
                   "retryable": False, "not_retryable_why": "closing"}
        if partial:
            # The socket died mid-reply. Those words reached the participant
            # and their audio is in the assistant WAV, so dropping the text
            # here would leave the two halves of the record disagreeing and
            # this turn's stage direction paired with nothing — the same
            # failure the barge-in path finalises to avoid.
            yield {"type": "response_done", "interrupted": True}
            # Said in the message as well as in the flag. The runner does read
            # the flag now (see the module docstring), so this is no longer the
            # only trace of the truncation — but it is the one that travels with
            # the reason the socket died, which the flag on the turn cannot
            # carry.
            closing += " — a reply was cut off mid-sentence"
        # Recoverable from the runner's point of view: a 1:1 runner rebuilds
        # the session (see _reconnect_after_gateway_close) and only says
        # `error` to the participant when it cannot.
        yield {"type": "error", "recoverable": True, "message": closing}

    async def close(self) -> None:
        # Set before the await so events(), which can only wake after it, sees
        # an intentional teardown rather than reporting a gateway failure.
        self._closing = True
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
