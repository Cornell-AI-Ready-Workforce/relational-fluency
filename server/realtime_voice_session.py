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
import inspect
import re
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Dict, List, Optional

from .director import Director, DIRECTOR_MAX_SPEAKERS


#: A sentence boundary the transcript stream ran together: a full stop,
#: question mark or exclamation mark with NO space after it, sitting between a
#: lower-case letter and a capital one.
#:
#: Both sides of that sandwich are load-bearing, because a full stop with no
#: space after it is usually NOT a sentence boundary:
#:
#:   "3.5", "1,200.00"   digit before       -> not matched
#:   "U.S.A"             capital before     -> not matched
#:   "example.com/R"     lower-case after the dot -> not matched
#:   "no.then"           lower-case after   -> not matched. A missing capital
#:                       says the transcriber did not think this was a sentence
#:                       boundary either, and inserting a space would assert
#:                       something this function does not know.
#:
#: Deliberately conservative in that direction: a missed seam is a blemish in a
#: caption, while a space pushed into a number or a URL is a transcript that
#: says something the character did not.
_RUN_ON_SEAM = re.compile(r"(?<=[a-z])([.!?])(?=[A-Z])")


def _space_sentence_seams(text: str) -> str:
    """Put the space back where the transcript stream ran two sentences together.

    OBSERVED in the cleanest measured encounter — a quiet microphone, no
    barge-in, nothing else wrong with the turn:

        "The cleanest thing is I walk the client through it.I got the deck
         done last night"

    The agent's audio is fine; the caption is what is wrong. A turn's
    transcript arrives in chunks that are joined with no separator — correctly,
    because a chunk boundary usually falls mid-word — and when the bridge
    re-sends a fragment it can begin a chunk at a sentence boundary, which is
    where the space goes missing.

    _clean_agent_text's repeat pass does not catch this one and is right not
    to: the tail here is a PARAPHRASE of an earlier sentence rather than a copy
    of it, so dropping it would delete something the character said.

    Cosmetic for the participant, who has already heard the line. Not cosmetic
    for the rater, who reads these transcripts as the record of the encounter,
    nor for anyone coding them afterwards.
    """
    return _RUN_ON_SEAM.sub(r"\1 ", text or "")


def _resume_seam(buf: List[str], ev: dict) -> str:
    """The delta's text, with a space in front of it where a reply RESUMED.

    Transcript chunks within one reply are joined with nothing between them,
    correctly: a chunk boundary usually falls mid-word. The bridge marks the
    opening chunk of a reply as `first` (after a response.created, a cancel,
    a commit or a retry). When such a chunk lands in a buffer that already
    holds text, the boundary is not a chunk boundary but the seam between a
    reply the participant cut off and the gateway's continuation of it —
    cancel is inert on Gemini, so the continuation streams on into the same
    turn — and joining across it is how "Is the handoff fixed or notIt needed
    to be said" reached the record. _space_sentence_seams cannot repair that
    one: it only knows a seam by the punctuation at it.

    Only at those boundaries, and only when both sides need it: a buffer that
    already ends in whitespace, or a chunk that begins with one or with
    punctuation, is left exactly as delivered.
    """
    text = ev.get("text") or ""
    if not (ev.get("first") and buf and text):
        return text
    tail = "".join(buf[-2:])
    if not tail or tail[-1].isspace() or not (text[0].isalnum() or text[0] in "\"'("):
        return text
    return " " + text


# On every brief, group and 1:1. See the note at the call site.
_LANGUAGE_RULE = (
    "\n\nLANGUAGE: The participant is speaking English. Always speak English, "
    "whatever language you think you heard."
)


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

    The seam repair runs FIRST, and the order matters in both directions. The
    whole-turn double above is detected on a seam with or without a space at
    it, so spacing first cannot hide a repeat; and the trailing-fragment pass
    splits on sentence boundaries, which is a split a run-on seam defeats — the
    two halves arrive as one part and the repeat is never seen.
    """
    t = _space_sentence_seams(text or "").strip()
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


_SAYS_MARKER = re.compile(
    r"^\s*(?:\[[^\]]{1,40}\s+says\]:|\(Context, not for you to repeat:|"
    r"[A-Z][a-z]+\s+(?:just\s+)?(?:says|said)(?:\s+out\s+loud(?:\s+to\s+the\s+group)?)?:)\s*"
)


def _strip_context_echo(text: str, told: list) -> str:
    """Drop parroted colleague-context from a reply.

    Seen on the native-audio route: Alex opened with '[Jordan says]: It's
    fine. Everything's fine. What do you need to discuss?' before answering.
    The room knows exactly what it told him, so any sentence of the reply
    that also appears in a recently injected line is removed, along with a
    leading 'X says:' marker.
    """
    t = (text or "").strip()
    # Marker anywhere in the reply (the model sometimes narrates a colleague
    # mid-line, or invents a note in our own format): remove the marker and
    # the quoted or single sentence it introduces.
    # The closing `"` and `)` are optional and ORDERED, because the room's own
    # note nests one marker inside another: `(Context, not for you to repeat:
    # Priya just said out loud to the group: "We slipped.")` is precisely what
    # GroupRoom.tell injects, and it is the shape a model parrots most often.
    # The alternation stops at the INNER sentence's full stop, so without a
    # place for the quote and the bracket to go the reply was recorded starting
    # `") Right, so...` \u2014 the note stripped and its punctuation left behind.
    t = re.sub(
        r"(?:\[[^\]]{1,40}\s+says\]:|\(Context, not for you to repeat:|"
        r"\b[A-Z][a-z]+\s+(?:just\s+)?(?:says|said)(?:\s+out\s+loud(?:\s+to\s+the\s+group)?)?:)"
        r"\s*(?:\"[^\"]*\"?|\u201c[^\u201d]*\u201d?|[^.!?]*[.!?])[\"\u201d]?\s*\)?\s*",
        " ", t,
    ).strip()
    t = re.sub(r"\s{2,}", " ", t)
    if not told or not t:
        return t
    told_norm = " ".join(_norm_speech(x) for x in told)
    kept = []
    for sent in [x for x in re.split(r"(?<=[.!?])\s+", t) if x]:
        n = _norm_speech(sent)
        if len(n.split()) >= 2 and n in told_norm:
            continue
        kept.append(sent)
    return " ".join(kept).strip().strip('"').strip()


def _is_stage_direction(text: str) -> bool:
    """'[Priya remains quiet.]' or '[Silence]': the model narrating instead of
    speaking. Treated as no reply; the native-audio route does this sometimes."""
    t = (text or "").strip()
    return bool(t) and t.startswith(("[", "(", "*")) and t.endswith(("]", ")", "*")) and len(t) < 80


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


# How much of a lost line has to come back in the retried one for the retry
# to count as having RECOVERED it (see _note_retry_outcome). On the retries
# that fired live, a genuine re-speak scores 0.8-1.0 and a line that answers
# the nudge instead ("I said understood", "I didn't say anything") 0.3-0.5.
RETRY_RECOVERED_OVERLAP = 0.6

_WORD_CHARS = re.compile(r"[a-z0-9']+")


def _word_overlap(lost: str, got: str) -> Optional[float]:
    """Share of the distinct words of `lost` that appear in `got`; None when
    `lost` has no words to look for."""
    want = set(_WORD_CHARS.findall((lost or "").lower()))
    if not want:
        return None
    have = set(_WORD_CHARS.findall((got or "").lower()))
    return round(len(want & have) / len(want), 3)


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
# redact_key goes on every gateway-derived string that leaves this process. Two
# live-gateway exceptions carry the credential verbatim: a 401 body that echoes
# back the key it was sent, and a key pasted wrapped across two lines, which the
# header parser rejects by quoting the whole Authorization value into its
# message. Everything written below is permanent or public — events.jsonl ships
# whole in the per-session download.zip and is mirrored to CloudWatch, and the
# participant's socket is outside this process entirely — so a bare str(exc)
# here puts a live credential into the IRB record. See llm.redact_key.
from .llm import provenance, redact_key
from .group_room import GroupRoom
from .voice.realtime import RealtimeVoiceSession, SilenceDetector
# The per-family answers origin/main reached for by model name. They read
# REALTIME_FAMILIES now (see server/voice/realtime.py), so the room, the runner
# and the bridge all get the same answer from the same table.
from .voice.realtime import (accepts_text_items, autofire_wait_for_model,
                             is_openai_realtime, member_tools_allowed,
                             relays_colleagues_as_text)
# The module, not just the names, because REALTIME_MODEL is what selects every
# piece of per-family behaviour below and it has to be readable at call time
# rather than frozen into this module at import.
from .voice import realtime as _realtime
# The one check that can tell a line the participant HEARD from a line the
# record merely says was spoken. See server/voice/turn_audio.py.
from .voice import turn_audio

if TYPE_CHECKING:
    from fastapi import WebSocket

    from .session import Session


# ── the voice a realtime session may be given ──────────────────────────────
#
# There used to be one field, `voice_id`, shared by the v1 ElevenLabs cascade
# and the realtime path, and that is how a group scenario's ElevenLabs id
# ("9BWtsMINqrJLrRacOk9x", see scenarios/g1..g5) came to be sent as the voice of
# a speech-to-speech session. It is not a harmless mismatch. A voice the model
# does not know takes the WHOLE session.update down with it, and the session
# then runs on whatever persona the gateway starts with. Probed against the
# Cornell gateway on 2026-09-10, one socket per case:
#
#   gpt-realtime-2.1          voice="9BWtsMINqrJLrRacOk9x"
#       -> error invalid_value on session.audio.output.voice, and NO
#          session.updated: the character brief never landed, so the actor
#          answers the participant as a stock assistant.
#   nto.gemini-live-2.5-flash  voice="9BWtsMINqrJLrRacOk9x"
#       -> no error frame at all, and still NO session.updated. The same loss,
#          with nothing on the wire to notice it by.
#
# The same probe with voice="Puck" acked on Gemini and was refused by gpt, and
# with voice="alloy" acked on gpt and not on Gemini: the rosters are disjoint,
# so a voice is only ever correct with respect to a model. Hence a roster, and
# hence membership of it — not the shape of the string — is what decides
# whether a scenario's voice may be used. An ElevenLabs id is in neither
# roster, so it cannot reach a session.
#
# The rosters themselves are NOT repeated here. Per-model behaviour lives in
# one named table — REALTIME_FAMILIES in server/voice/realtime.py — so that
# somebody choosing REALTIME_MODEL can read what that choice implies in a
# single place, with the evidence for each claim beside it. The two functions
# below are this module's only door onto that table, and the runner asks no
# other question about which model it is on.


# THE UNANSWERED LINE. The participant's own audio since the last transcript
# the gateway returned, kept so it can be put in front of the model again
# when the gateway drops a turn — a create it ignored (reply_missing), or
# the line in flight when the gateway died under the encounter. Measured
# live (2026-09-14, second hesitant round): every 1:1 encounter past ~90 s
# hit a gateway that went silent for good and dropped the socket 22-38 s
# later; the participant's last line ("Okay." / "Sure, I'll look at it
# later") was never transcribed, never answered, and the page told them to
# say it again. Bounded in seconds of raw audio; the replay itself is
# compacted (silences collapsed, see _replay_speech) and capped shorter.
REPLAY_KEEP_S = float(os.getenv("REPLAY_KEEP_S", "45"))
REPLAY_MAX_S = float(os.getenv("REPLAY_MAX_S", "30"))
REPLAY_PAUSE_MS = int(os.getenv("REPLAY_PAUSE_MS", "600"))
REPLAY_MIN_SPEECH_MS = int(os.getenv("REPLAY_MIN_SPEECH_MS", "300"))
# How much of a room member's reply is held back while it is being suppressed
# for lack of the floor, so that a floor grant arriving mid-reply can relay
# the line from its first word rather than from wherever the grant landed.
HELD_AUDIO_MAX_S = float(os.getenv("HELD_AUDIO_MAX_S", "20"))


def realtime_model() -> str:
    """The model every RealtimeVoiceSession in this process will open on.

    Read off the bridge module at call time rather than off a live session: the
    voice for a character has to be resolved BEFORE the session that will carry
    it exists, and a half-built session's `model` is not an answer.
    """
    return getattr(_realtime, "MODEL", "") or ""


def realtime_voices(model: Optional[str] = None) -> List[str]:
    """The voices `model` accepts, in the order characters are cast into them.

    Empty for a model the table does not cover, so that a character resolves to
    no voice at all and the refusal happens where it can say something useful —
    require_capabilities, at connect, naming the model and the table it is
    missing from — rather than here, inside a turn, as a KeyError.
    """
    caps = _realtime.capabilities_for(model or realtime_model())
    return list(caps.voices) if caps is not None else []

# The actor holds this tool and its brief at the same time, and the brief says
# "Do not wrap it up early, and never end it yourself unless told to." The old
# description invited exactly what the brief forbids — "reached its natural end,
# the matter has been addressed" — so the actor was handed both rules and
# neither won reliably. Under-calling stalls a segment until the pacing gate
# moves it; over-calling truncates a scored interaction the moment the argument
# feels settled, which is routinely several beats before the beats are spent.
# Both failures are silent in the record.
#
# So the two are made to say ONE thing: the only event that ends a segment is
# the other person ending it — which is what "unless told to" means in
# practice, so the tool no longer contradicts the brief. The description names
# the three temptations it must refuse rather than leaving them to inference.
END_SEGMENT_TOOL = {
    "type": "function",
    "name": "end_conversation",
    "description": (
        "Call this only when the other person has ended the conversation: they "
        "have said goodbye, said they are done, or walked away. Never call it "
        "because the matter feels resolved, because there is a pause, or "
        "because you have nothing more to add. Do not mention the tool."
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
        # The runner's own end-of-turn silence is raised to the family's
        # measured window (REALTIME_FAMILIES, the gemini row): with the gateway
        # told to wait 1500 ms through a lost participant's mid-thought pause
        # and the runner still committing at 900 ms, the runner was the thing
        # that split the turn — "I need more context. [900 ms] What do you
        # mean?" committed as two turns, answered twice. The env floor
        # VAD_SILENCE_MS still applies where it is the larger; the gpt row
        # asks for nothing and keeps the runner's own bar.
        # ms of sub-bar audio still to hear before a 1:1 turn end the VAD has
        # marked is acted on; None when no turn end is being held. See
        # _end_of_turn_confirm_ms and the turn_ended branch of _client_to_model.
        self._turn_end_pending_ms: Optional[float] = None
        # True from a room turn being spawned until it has the participant's
        # transcript to route on. A second turn_ended inside that window (the
        # runner's bar firing inside a mid-thought pause, then again at the
        # real end) must not spawn a second routing of the same turn.
        self._group_turn_waiting = False
        # Edge-latch on the VAD's barge-in gate: one cancellation per stretch of
        # participant speech, not one per frame of it. See _client_to_model.
        self._was_barging = False
        # The interaction's `opening:` framing, folded into the CONNECT-TIME
        # brief on a family whose row says a mid-session session.update is
        # inert (see _fold_opening). Cleared once the actor has spoken under
        # it. `_opening_agent` scopes it to one member of a room.
        self._opening_note = ""
        self._opening_agent: Optional[str] = None
        # How many 1:1 gateway sessions this encounter has rebuilt after the
        # GATEWAY closed one (see _reconnect_after_gateway_close).
        self._reconnects = 0
        # Participant utterances recorded so far, numbered on the frame the
        # page gets so two fragments of one thought are two numbered lines.
        self._user_utterances = 0
        # Audio actually RELAYED to the participant for the 1:1 turn now
        # speaking. Counted at the point of delivery, because that is the only
        # place the question "did they hear this line?" has an answer — the
        # transcript is whole whatever happens, and so is the assistant WAV.
        # See server/voice/turn_audio.py.
        self._turn_audio_bytes = 0
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
        # The console's half of that same restraint, which it did not have:
        # (source, message) -> the frame already sent for this burst and how
        # many identical ones have been counted since. Source alone ("model",
        # "room:<id>", "scribe") is not the key, because two DIFFERENT faults
        # on one stream are not repeats of each other. The first goes out at
        # once, the total goes out when the burst ends. See
        # _encounter_event_soon and _flush_console_repeats.
        self._console_repeats: dict = {}
        # Every console frame dispatched off a pump, held for as long as it
        # takes to send. Session.spawn_auto_steer keeps its tasks in a set for
        # the same reason: the loop holds only a weak reference to a bare
        # ensure_future handle, and a frame collected mid-send is a failure
        # report that vanished with nothing to say it ever existed.
        self._console_tasks: set = set()
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
        # (agent_id, field, value) for every scenario voice this model cannot
        # speak, so _note_unusable_voice says it once and not once per switch.
        self._unusable_voices: set = set()
        # Latch for the once-per-encounter console line about an actor whose
        # stage directions are going out unacknowledged. See _deliver_brief.
        self._unacked_reported = False
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
        # The line each character's in-flight retry is standing in for; see
        # _retry_reply and _note_retry_outcome.
        self._retry_lost: dict = {}
        # The participant's audio since the gateway last returned a
        # transcript, 1:1 only (see REPLAY_KEEP_S and _replay_speech). Cleared
        # by every user_transcript, at a character switch, and after a replay.
        self._replay_pcm = bytearray()
        # Set when the 1:1 pump has closed a socket ITSELF because the gateway
        # ignored a request and its retry: _reconnect_after_gateway_close then
        # rebuilds despite the close being ours, and replays the line.
        self._rebuild_for_replay = False
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
        # (agent_id, line) for each finished line the room relayed to the
        # OTHER members as text (GroupRoom.tell). _strip_context_echo checks a
        # reply against these, so it has to be able to leave out the speaker's
        # own: a flat list of strings stripped a character's line to nothing
        # whenever the gateway answered one turn twice with the same words,
        # which is precisely the doubled reply second_reply_split exists to
        # record. The room never tells a member its own line (exclude=), so
        # neither does this check.
        self._recent_told: List[tuple] = []
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
        self._watch_store_for_swallowed_failures()

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
        # No VOICE block and no MEETING block are built here any more. They were
        # a second copy of rules engine.py was already emitting — a tighter
        # length cap than the mode line above it, a floor rule already in the
        # group branch — and the two copies disagreed. Both now live in
        # engine.SPEECH_RULES / engine.MEETING_RULES, which this call has
        # already placed at the end of `base`, so what the actor reads last
        # about how to speak is one rule and not three. See the comment on
        # SPEECH_RULES for what the duplication was measured to cost.
        #
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
        opening = ""
        note = getattr(self, "_opening_note", "")
        if note and getattr(self, "_opening_agent", None) in (None, self.agent_id):
            opening = note
        room_rule = ""
        if self.is_group():
            # Every member's input carries the other characters' fanned-out
            # audio labelled "user" (see GroupRoom.hear). Measured live
            # (2026-09-14, S3C, Noor): a member granted the floor right after
            # another character's line answered THAT line as if it were an
            # unintelligible participant — the model's stock "I'm sorry, but I
            # don't think that's quite right based on what I'm hearing. Could
            # you clarify what you mean?", four times in one turn. Said once
            # here, for rooms only: what a member hears that is not the
            # participant is a colleague, and confusion is never voiced.
            #
            # The second paragraph is origin/main's (cabc1dd, df1ab83) and is
            # about the OTHER way a colleague reaches a member: as a
            # parenthesised context note from GroupRoom.tell, on the families
            # whose row says relay_colleagues_as_text. Both halves are needed
            # now, because after this merge a room uses BOTH channels — audio
            # on plain flash, text on native-audio and gpt — and a member must
            # handle a colleague correctly whichever way it arrives.
            # _strip_context_echo is the second line of defence for the route
            # that parrots the note anyway.
            room_rule = (
                "\n\nThe other people in this room are audible to you. A voice "
                "that is not the person running the meeting is one of them, not "
                "something you must answer. Never say that you did not understand "
                "or did not catch something, never ask anyone to clarify what they "
                "mean, and never apologise for mishearing: answer the last thing "
                "that was actually said to you, or stay quiet."
                "\n\nYou will sometimes receive notes in parentheses telling you "
                "what a colleague just said. They are context only. Never read "
                "them out and never narrate what a colleague said (no 'Casey just "
                "said...'): respond as someone who heard it, in your own words. "
                "If the participant addresses someone else by name, stay silent "
                "and let them answer. Do not repeat or rephrase what another "
                "person just said, and do not answer every turn: leave room for "
                "quieter colleagues."
            )
        return base + _LANGUAGE_RULE + self._scene_note + opening + room_rule

    # ── the framing line, on a family that cannot be steered after connect ──
    def _steering_is_inert(self, model: Optional[str] = None) -> bool:
        """True on a family whose row says a mid-session session.update is
        not honoured — the configured nto.gemini-live-2.5-flash. On such a
        family the ONLY brief the actor ever reads is the one sent at
        connect, so anything that has to reach it goes there or nowhere."""
        caps = _realtime.capabilities_for(model or realtime_model())
        return caps is not None and not caps.honours_session_update

    def _opening_direction(self, opening: str) -> str:
        """The direction that makes a room's lead open the scene; see
        _open_group_scene for why it names the part to be spoken."""
        return (
            # No sentence count here: SPEECH_RULES, three lines above this
            # note in the same prompt, already sets the length of a turn.
            # A second count in the moment-note is how the prompt grew six
            # of them.
            "You speak first and open the scene, in character. Here is "
            "where the scene is found — "
            f"{opening} — if that quotes a line of dialogue, say that "
            "line; otherwise begin from that situation in your own words. "
            "Never read the description out, and never narrate or describe "
            "the scene or your own actions: speak only what your character "
            "says to the people in the room."
        )

    def _first_reply_note(self, opening: str, agent_name: str = "") -> str:
        """The 1:1 framing, as a standing note for the actor's FIRST reply.

        In a 1:1 the participant speaks first, so the interaction's `opening:`
        is not a line the character delivers unprompted — it is what their
        first reply carries, whatever the participant opened with. Live, with
        a lost participant ("Hello." / "Good morning." / "What? Hello."), an
        actor with no framing answered in content-free fragments ("The
        pack." / "The numbers.") because it had nothing to converse ABOUT;
        the scene existed on the situation card and nowhere in the room.
        """
        # Two shapes of `opening:` in the bank, and the note has to cover
        # both: S2's are lines the character says ("The pack went out this
        # morning..."); S1's are a quoted line plus a third-person stage note
        # ("Next morning you and Drew reach the coffee machine together,
        # speak, or not."). Live, an actor handed the S1 shape read the stage
        # note out loud as its first reply, so the note names which part is
        # to be spoken and forbids narrating the rest, exactly as the group
        # direction does.
        if self._opening_is_stage_note(opening, agent_name):
            # S1's second interactions ("Next morning you and Drew reach the
            # coffee machine together, speak, or not." / "You run into Sam by
            # the elevators...") describe the character in the THIRD PERSON
            # and quote nothing. Handed the general note below, the live
            # actor read the description out as its first line ("Drew: Next
            # morning you and Drew reach the coffee machine together, speak,
            # or not.") — measured 2026-09-14, S1B hesitant run, first Drew
            # turn. So a note that names the actor and quotes no dialogue is
            # never put in guillemets, never offered as something to say, and
            # the first reply is sent to the brief's own opening move instead.
            return (
                "\n\nFIRST REPLY, for you alone — never say any of this out loud:\n"
                "The other person speaks first. Whatever they open with — a "
                "greeting, a false start, half a sentence, or not much at all — "
                "your FIRST reply is your own opening move, the one your brief "
                "describes under how you behave, said in the first person as "
                "yourself. Where the scene is found, for you only and not to be "
                f"spoken: {opening} That is a stage note about you, written in "
                "the third person; it is not a line, so never read it out, never "
                "narrate it, never describe the scene or your own actions — "
                "speak only what your character says to the person in front of "
                "you, then hand the floor back to them. That belongs to your "
                "first reply ONLY. Their very first utterance is the only first "
                "one: when they greet you again, say \"what?\", \"hello?\" or "
                "\"what about it?\", that is NOT a new start — the scene is "
                "already open and you have already made your opening move, so "
                "never make it again in any form, not those words and not a "
                "paraphrase. From your second reply on: answer the thing they "
                "actually said, in one plain sentence, and if they gave you "
                "nothing to answer, ask them one question about it — a "
                "different question each time."
            )
        return (
            "\n\nFIRST REPLY, for you alone — never say any of this out loud:\n"
            "The other person speaks first. Whatever they open with — a "
            "greeting, a false start, half a sentence, or not much at all — "
            f"your FIRST reply is where this scene is found: «{opening}». If "
            "that quotes a line of dialogue, say that line, in your own words "
            "if you like; where it describes the situation instead, begin "
            "from that situation as yourself — never read the description "
            "out, and never narrate or describe the scene or your own "
            "actions: speak only what your character says to the person in "
            "front of you. Then hand the floor back to them. That belongs to "
            "your first reply ONLY. Their very first utterance is the only "
            "first one: when they greet you again, say \"what?\", \"hello?\" "
            "or \"what about it?\", that is NOT a new start — the scene is "
            "already open and you have already said where you stand, so never "
            "say it again in any form, not those words and not a paraphrase. "
            "From your second reply on: answer the thing they actually said, "
            "in one plain sentence, and if they gave you nothing to answer, "
            "ask them one question about what they came in to say — a "
            "different question each time."
        )

    @staticmethod
    def _opening_is_stage_note(opening: str, agent_name: str = "") -> bool:
        """True for an `opening:` that is a description OF the actor rather
        than a line BY the actor: it names the character in the third person
        and quotes no dialogue. Mel's "Saw Drew's message last night..." names
        Drew, not Mel, and is her line; "Next morning you and Drew reach the
        coffee machine together" names Drew and is a stage note."""
        if not agent_name:
            return False
        if re.search(r'["“”«»]', opening):
            return False
        return re.search(r"\b" + re.escape(agent_name) + r"\b", opening) is not None

    def _fold_opening(self, agent, *, group: bool) -> Optional[bool]:
        """Put the current interaction's `opening:` where this family will
        actually read it, and write down which way it went.

        Returns True when the framing was folded into the connect-time brief
        (the actor's session must be built AFTER this call), False when the
        family honours mid-session updates and the existing path carries it
        (the t1 beat's cue in 1:1, the opening stage direction in a room),
        None when the interaction has no `opening:` at all.

        Recorded as `opening_framing` either way, because the two routes are
        not equally trustworthy and an analyst reading the record has to be
        able to tell which one this encounter's first line came through.
        """
        opening = str(self._interaction().get("opening") or "").strip()
        if not opening:
            return None
        model = realtime_model()
        folded = self._steering_is_inert(model)
        if folded:
            self._opening_note = (self._director_note(self._opening_direction(opening))
                                  if group else self._first_reply_note(
                                      opening, getattr(agent, "name", "") or ""))
            self._opening_agent = agent.id
        else:
            self._opening_note = ""
            self._opening_agent = None
        self.session.store.event(
            "opening_framing", agent_id=agent.id, interaction=self._interaction_id(),
            segment=self.segment, mode="group" if group else "one_to_one",
            via="connect_brief" if folded else (
                "stage_direction" if group else "trigger_cue"),
            model=model, honours_session_update=not folded,
        )
        return folded

    def _end_of_turn_confirm_ms(self) -> int:
        """How much MORE quiet than its own bar the runner must hear before a
        1:1 turn end is real on the sockets it is feeding now.

        The family's measured gateway window (REALTIME_FAMILIES, the gemini
        row: 1500 ms) less the runner's own VAD_SILENCE_MS (900). Zero on a
        family that asks for no window (gpt: server VAD off, the runner's bar
        is the only turn close, unchanged). Read off the room where there is
        one, or the live 1:1 session, and only then off the process default —
        a room can be opened on a model the process is not on.
        """
        model = None
        room = getattr(self, "room", None)
        if room is not None:
            model = getattr(room, "model", None)
        elif getattr(self, "rt", None) is not None:
            model = getattr(self.rt, "model", None)
        floor = _realtime.end_of_turn_silence_ms_for(model or realtime_model())
        return max(0, int(floor or 0) - int(self.vad.silence_ms))

    def _new_session(self, *, instructions: str, voice: str) -> RealtimeVoiceSession:
        """One 1:1 session, with the family's measured end-of-turn window on
        it (see REALTIME_FAMILIES and RealtimeVoiceSession.turn_detection)."""
        rt = RealtimeVoiceSession(
            instructions=instructions, voice=voice, tools=[END_SEGMENT_TOOL],
        )
        window = _realtime.end_of_turn_for(getattr(rt, "model", realtime_model()))
        if window is not None:
            rt.turn_detection = window
        return rt

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

    def _director_note(self, direction: str) -> str:
        """Wrap a stage direction the one way every path in this file wraps it.

        Five call sites used to carry their own copy of the same f-string, so
        the wording could only ever be improved in four of them.

        What the wording has to do: be read, not spoken. The old
        "(follow precisely, never mention)" put the actor in the posture of
        executing a script it must conceal, which is a different mental posture
        from being a person with a grievance — and it read as production
        vocabulary handed to someone standing by an elevator. "Never say it out
        loud" is the part that was load-bearing; keep that and drop the cloak.

        The "DIRECTOR NOTE" marker itself stays: it is the token the runner's
        own tests look for to prove a beat was briefed, and the steering log a
        rater reads is keyed to the text that follows it.
        """
        return f"\n\nDIRECTOR NOTE, for you alone — never say any of this out loud:\n{direction}"

    def _trigger_instruction(self, trigger: dict, *, probing: bool) -> str:
        """Turn a planted trigger into a stage direction for the actor. The cue
        is what should happen next; on_silence is the probe that keeps a silent
        participant from turning into missing data.

        Written in the SECOND person, and it names the actor. The old wording —
        "Bring about this beat now" over a cue like "Sam defends: 'I did most of
        the legwork anyway'" — handed Sam a note about Sam in the third person,
        twenty lines under a brief that forbids both referring to himself in the
        third person and reading stage directions aloud. The beat still fired in
        most samples, but the actor was resolving that contradiction on the one
        turn the whole encounter is scored on. "beat" and "director note" are
        craft vocabulary besides, handed to someone standing by a lift.

        The wrapper has to read correctly around a cue written either way —
        "Sam defends: '...'" or "Defend yourself before you have thought about
        it" — because the cues live in the scenario bank and are not this file's
        to rewrite. "Your next move, as <name>" frames both as this character's
        own action without touching a word of the cue itself, which must reach
        the actor verbatim: it IS the measurement.

        "the participant" is gone from the probe for the same reason it is going
        everywhere else in the brief: it is the clinical noun for a research
        subject, and it was sitting in the sentence that asks the actor to be a
        person who has noticed the other one has stopped talking.

        The PROBE now uses that SAME framing, and that is the whole of the fix
        below. It used to say "say this now", which assumed `on_silence` was
        always a quoted line. It was, once. S1 and S2 still write theirs as
        speakable lines in the character's own register — deliberately, and
        pinned, so that every participant who freezes at a beat meets the same
        sentence and what differs between them is their answer and not the
        prompt. The naturalness round then rewrote S3's and S4's probes into
        DESCRIBED MOVES — "Read the silence as agreement and say that is the
        running order then", "They have not answered ... take it as the answer,
        dryly" — and "say this now: <description of a move>" is an instruction
        to speak the description.

        Measured on the gateway against the bank as it stood then, one synthetic
        silent participant throughout, 136 described-move turns per family:
        nto.gemini-live-2.5-flash leaked nothing (it re-reads the note as a move
        unprompted); gpt-realtime-2.1 spoke the note back at the person in the
        room on 10 of 136, three of them unarguable — "They haven't answered."
        opening a turn in the third person about the participant standing there
        (S3B t1), "You've said nothing, so I'm not filling the silence" (S3A
        t2), "the silence makes it worse" (S3A t3). Latent on the configured
        model, live on the family the study moves to if it wants mid-session
        steering that works.

        The obvious repair — tell the actor to branch, "if that is a line say
        it, if it describes a move make it" — was tried FIRST and is the wrong
        answer, which is worth recording because it looks right. Three branching
        wordings all closed the leak (0/24 on both families) and all bought it
        by making the actor deliberate about the note instead of acting on it:
        recall of the pinned S1/S2 wording fell from 0.86/0.90 (gemini/gpt) to
        0.44/0.66, and 8 of 36 quoted-line probes were abandoned mid-turn for a
        generic re-opening of the scene — a probe that fires precisely because
        there is no other signal, producing no signal. Asking an actor to
        classify its own direction costs more than the classification is worth.

        "Your next move, as <name>, now, in your own words" is what the probe
        says instead, because it never names a mode of delivery to get wrong: a
        line is a thing this character says next, a described move is a thing
        this character does next, and both are its next move. It needs no branch
        and asks the actor to decide nothing. It is also the formula the
        naturalness round drove 3,051 actor turns through on the cue path for
        zero spoken stage directions, so the probe is no longer the one path in
        this file still guessing at which style the bank was written in.

        PARALLEL-FORM EQUIVALENCE is what decided it, measured rather than
        argued: 272 probe turns per wording over every on_silence in the bank,
        both families, both forms of every pair driven with identical
        participant input. Under "say this now", S1A's probes were delivered
        less faithfully than its twin S1B's by 0.115 of pinned-wording recall,
        95% CI [-0.197, -0.033] — a gap excluding zero, between two forms that
        are supposed to be interchangeable, in a quantity nobody reading the
        ESCI scores afterwards could recover. Under this wording the gap is
        +0.045, CI [-0.156, +0.292], spanning zero. S2A/S2B spans zero under
        both.

        The cost, stated because it is real and one-sided: on
        nto.gemini-live-2.5-flash, recall of S1/S2's pinned lines falls 0.926
        [0.874, 0.971] -> 0.807 [0.724, 0.882], while on gpt-realtime-2.1 it
        rises 0.962 -> 0.981. Some standardisation of the probe's wording is
        being traded on the configured model for a form asymmetry that is a
        confound rather than noise. That is the right way round: S1A's own
        header already permits paraphrase and pins substance, not words, and a
        difference between forms is the one kind of variance the design cannot
        absorb.

        What is NOT claimed: that this alone closed the leak on the current
        bank. S3's owner fixed it from the other side in the same round, by
        rewriting S3's probes as "that ..." complements that cannot be spoken as
        they stand, and against today's bank neither wording leaks unarguably.
        S4's probes are still plain imperative described moves ("Take the
        silence for agreement and say you will send it round tonight"), so they
        are covered by this wrapper and not by that convention — and the content
        fix holds only while the wrapper keeps saying "say this now", which is
        the coupling that produced the bug. Both fixes standing is the point.
        One borderline case survives on both wordings, S4A t4 on gpt (1-2 of
        32), where the move performed in character and the words describing it
        coincide: "Silence is agreement. I'll send the schedule around tonight."
        Its twin S4B t4 produces 0 of 32, so that residue is a content-side
        asymmetry and belongs to S4A.

        The two clauses that are NOT the cue's are load-bearing and stay. "They
        have gone quiet" is why this beat is firing at all, and "then stop and
        let them answer" is the floor returning: a probe carries that beat's own
        move and never the next beat's work.
        """
        name = getattr(self.agent, "name", "") or "you"
        if probing and trigger.get("on_silence"):
            return (
                f"They have gone quiet. Your next move, as {name}, now, in "
                f"your own words, then stop and let them answer: "
                f"{trigger['on_silence']}"
            )
        return f"Your next move, as {name}, now, in your own words: {trigger['cue']}"

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
        return self._voice_of(self.agent)

    def _voice_of(self, agent) -> str:
        """The realtime voice for one character.

        `realtime_voice` is the realtime path's own field and is asked for
        first. `voice_id` is the v1 cascade's field and is still worth asking:
        the v3 loader writes Gemini voice names into it (scenarios_v3._VOICES),
        so honouring it keeps every v3 scenario sounding as its author cast it.
        But it is ALSO where the group scenarios keep their ElevenLabs ids, and
        one of those silently costs the session its persona (see the roster
        comment at the top of this module). So neither field is trusted for
        being the right field: a value is used when the running model offers
        that voice, and otherwise the character falls back to its position in
        the roster, exactly as an uncast character always has.

        The fallback is not silent. A scenario that named a voice this model
        cannot speak is a casting decision that did not survive the run, and a
        rater listening for two distinguishable characters should be able to
        find out why they sound alike.
        """
        pool = realtime_voices()
        if not pool:
            # A model the capability table does not cover. Naming no voice is
            # the only safe answer: connect() refuses this model by name a
            # moment later, and until it does, an invented voice is the one
            # thing that could turn a loud refusal into a silent session.
            return ""
        for field in ("realtime_voice", "voice_id"):
            named = getattr(agent, field, None)
            if not named:
                continue
            if named in pool:
                return named
            self._note_unusable_voice(agent, field, named)
        idx = next((i for i, a in enumerate(self.cast) if a.id == agent.id), 0)
        return pool[idx % len(pool)]

    def _note_unusable_voice(self, agent, field: str, named: str) -> None:
        """Record once that a scenario's voice cannot be used on this model.

        Once, because _voice_of is called on every character switch and on
        every member of a room, and a line per call would bury the fact it is
        reporting. The value itself is written: an analyst reading this needs
        to see that it is an ElevenLabs id rather than a misspelt Gemini name,
        because those are different repairs to the scenario file.
        """
        key = (agent.id, field, named)
        if key in self._unusable_voices:
            return
        self._unusable_voices.add(key)
        self.session.store.event(
            "realtime_voice_unusable",
            agent_id=agent.id,
            field=field,
            value=named,
            model=realtime_model(),
            offered=realtime_voices(),
        )

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
        # The scene's framing goes into the LEAD's connect-time brief on a
        # family that will read no brief after it (see _fold_opening). It has
        # to happen before the room builds its sessions, because
        # `instructions_for` is read at connect and never again there.
        if agents:
            self._fold_opening(agents[0], group=True)
        room = GroupRoom(
            agents,
            instructions_for=lambda a: self._instructions_for(a),
            voice_for=lambda a: self._voice_for(a),
            # Offered, not imposed: GroupRoom._member_tools drops it on a
            # family whose row says member_tools=False (native-audio, which
            # calls end_conversation constantly and turns every call into an
            # empty turn -- origin/main 5a45420). On every other route the
            # tool stays, which is where our END_SEGMENT_TOOL wiring was
            # measured working.
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
                agents=[a.id for a in agents], message=redact_key(str(exc)),
            )
            await self._encounter_event(
                "voice_error", detail=f"open_room: {exc}", severity="error",
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
        self._member_states = {a.id: _MemberState() for a in agents}
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
            direction = self._opening_direction(opening)
            # Two routes, and the record says which. On a family that honours a
            # mid-session session.update the direction goes out now, as the
            # brief the lead speaks under, and the floor is a pad+commit+create
            # like any other grant. On the configured Gemini family that update
            # is inert AND a commit of pure silence draws no frame at all (5/5
            # rooms yesterday: the opener was never spoken, `empty_response`,
            # and the unanswered create left `responding` latched so the next
            # grant was skipped as "already answering"). There the direction
            # was folded into the lead's CONNECT brief by _open_room, and the
            # lead is made to speak by a user text item + response.create,
            # which drew a full in-character opening 4/4 on the same sessions.
            folded = (bool(self._opening_note)
                      and self._opening_agent == lead.id)
            if folded:
                instructions = self._instructions_for(lead)
                acked = None
            else:
                instructions = self._instructions_for(lead) + self._director_note(direction)
                acked = await self._deliver_brief(rt, instructions)
            # The opening line is a director instruction like any other, so it
            # belongs in the steering log; it fires no planted trigger, hence
            # the null trigger_id.
            self._pending_direction = {
                "acked": acked,
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
                "via": "connect_brief" if folded else "session_update",
                "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest()[:16],
                "director_model": (self.director.model if getattr(self, "director", None)
                                   else provenance()["text_model"]),
            }
            self.session.store.event("stage_direction", **self._pending_direction)
            self._response_done.clear()
            if folded and hasattr(room, "open_scene"):
                granted = await room.open_scene(
                    lead.id, prompt=getattr(_realtime, "SCENE_OPEN_PROMPT", ""))
            else:
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
                "voice_error", where="pump", message=redact_key(str(exc))
            )
            self._encounter_event_soon(
                "voice_error", detail=f"pump: {exc}", severity="error"
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
        # However this turn ended, it is no longer waiting for a transcript:
        # the next turn_ended is a new turn. (_run_group_turn clears this
        # itself once it has routed; this covers a turn that never got there.)
        self._group_turn_waiting = False
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.session.store.event(
                "voice_error", where="group_turn", message=redact_key(str(exc))
            )
            self._encounter_event_soon(
                "voice_error", detail=f"group_turn: {exc}", severity="error"
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
        return self._voice_of(agent)

    def _member_voice(self, agent) -> Optional[str]:
        """The voice a room member is actually speaking with.

        Off the live session where there is one, because that is the only place
        that knows what the gateway was given; resolved the same way it was
        chosen otherwise, for a member whose session has already gone.
        """
        rt = self.room.session_for(agent.id) if self.room is not None else None
        return getattr(rt, "voice", None) or self._voice_of(agent)

    # ── did the brief actually arrive? ─────────────────────────────────────
    async def _deliver_brief(self, rt, instructions: str) -> Optional[bool]:
        """Send a brief and answer whether the PLATFORM said it arrived.

        Every director path in this file — a planted beat, the opening of a
        group scene, a character switch, the steering re-brief — reaches the
        actor by one route: a mid-session session.update carrying the persona
        with the stage direction appended to it. The record was written when
        that send returned, which is to say when the bytes left this process,
        and that is not the same claim at all.

        On the Cornell gateway, probed 2026-09-10: gpt-realtime-2.1 answers a
        mid-session session.update with a session.updated frame, twice out of
        two. nto.gemini-live-2.5-flash answers the session.update sent at
        connect and answers nothing after it — three mid-session frames, zero
        acknowledgements, no error. So on the model the study is configured for
        today, every stage direction this file writes down as delivered was
        delivered to a socket and to nothing beyond it.

        Which is why the answer here has three values and not two. True is the
        platform acknowledging the update. False is the platform declining to,
        having been asked — a direction the actor demonstrably did not receive.
        None is this bridge being unable to tell, which is not the same as
        either and must not be recorded as one; an analyst reading a steering
        log has to be able to separate a direction that landed from one that
        did not, and neither from one nobody checked.

        The ack itself belongs to the bridge, not to the runner, so it is read
        off whatever the bridge offers: a truthy/falsey return from
        update_instructions, or a `last_update_acked` attribute beside it. A
        bridge with neither yields None, which is the honest answer for it.
        """
        ack = await rt.update_instructions(instructions)
        if not isinstance(ack, bool):
            ack = getattr(rt, "last_update_acked", None)
        if not isinstance(ack, bool):
            return None
        if ack is False:
            await self._report_unacked_steering()
        return ack

    async def _report_unacked_steering(
        self, *, agent_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Date, once, the moment this actor stopped receiving its directions.

        Once per encounter: on a model that acknowledges no mid-session update
        this is every direction, and a row per turn would bury the one fact it
        is there to carry. That fact is worth carrying because the encounter
        looks entirely healthy from every other angle — the actor talks, the
        participant is heard, the steering panel moves — while the planted
        beats that make the encounter scoreable are not reaching the actor and
        the session is being spent for nothing.

        Written to the record only. This belongs on the researcher's live
        channel as well, and _encounter_event is right there, but a kind with
        no label in static/researcher.html reaches a watching researcher as a
        raw event name — which two tests in test_final_console.py exist to
        prevent, from both sides. Adding `steer_unacked` to ENC_EVENT_LABEL is
        the other half of this, in a file this change does not own.

        WHICH MODEL, AND WHICH ACTOR. Both are arguments now, defaulting to
        what this method already assumed. The default answer — the runner's
        current character, and the model the PROCESS is on — is the right one
        on the 1:1 path and only there. A room is opened on ITS model, which
        need not be the process default (that is the whole of GroupRoom's
        `model=`), and the actor a direction failed to reach is a named member
        rather than whoever the runner happens to be pointing at between turns.
        A row that named the wrong model here would be the most misleading line
        in the file: it is the one an analyst reads to find out which family
        the encounter was unsteered on.
        """
        if self._unacked_reported:
            return
        self._unacked_reported = True
        model = model or realtime_model()
        self.session.store.event(
            "steer_unacked", agent_id=agent_id or self.agent_id,
            segment=self.segment, model=model,
        )
        # And onto the researcher's live strip, which is the half the docstring
        # above said was missing. The record alone is not enough for this one:
        # every other symptom of an unsteered encounter is invisible while it
        # runs — the audio is fine, the transcript is fine, the planted beats
        # are all logged as fired — so a researcher watching has nothing else
        # that could tell them the encounter they are collecting is worthless.
        # severity "error" rather than "warn" because there is no degraded
        # version of this: the independent variable is either reaching the actor
        # or it is not.
        self._encounter_event_soon(
            "steer_unacked", agent_id=agent_id or self.agent_id,
            severity="error",
            detail=f"{model} did not acknowledge the stage direction",
        )

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
        #
        # `held` is the transcript of a reply that was being suppressed when it
        # gained the floor — the head of a line whose body the participant is
        # about to hear. See the suppression branch below.
        state = {"announced": False, "suppressing": False, "barged_in": False,
                 "finalized": False, "settled": asyncio.Event(), "held": [],
                 "audio_bytes": 0, "held_audio": bytearray()}
        held_audio_cap = int(HELD_AUDIO_MAX_S * _realtime.CLIENT_RATE) * 2
        # Published for the duration of this pump so the barge-in path can
        # reach this member's open turn; dropped in the finally so a dead
        # pump's buffer is never finalized.
        self._member_turns[agent.id] = (buf, state)
        # See _pump_events: the bridge's audio-absent clock is held while the
        # participant is talking, and the runner's VAD is the only thing that
        # knows. Members share the one microphone, so they share the one hook.
        rt.participant_speaking = lambda: bool(self.vad.active_within())
        # origin/main's per-member hold state, kept ALONGSIDE ours rather than
        # instead of it. Ours is the pump; this object is what her grant path
        # (_grant / adopt_member / _cancel_stale_holds) and her playback clock
        # read, and it is written from the branches below. Without it those
        # three would be dead code that never fires and the native-audio route
        # would lose the stale-hold cancel, which is the difference between a
        # member answering the next turn and never answering again.
        st = self._member_states.get(agent.id)
        if st is None:
            st = _MemberState()
            self._member_states[agent.id] = st
        try:
            async for ev in rt.events():
                etype = ev["type"]
                has_floor = self.room is not None and self.room.speaking == agent.id

                # The bridge fires its own response after speech-plus-silence,
                # commit or not, on every session at once. Only the character
                # holding the floor may be heard; unsolicited responses are
                # cancelled and their events discarded, or the room becomes
                # three people talking over each other.
                #
                # `state["announced"]` counts as holding the floor, and that is
                # a fix, not a shortcut. `announced` is set on the first frame of
                # this reply that was RELAYED — so it can only be True for a
                # reply that had the floor when it started and that the
                # participant is, at this instant, listening to. Suppressing such
                # a reply because the floor moved underneath it does not stop
                # the character being heard: it amputates the tail of a sentence
                # already half-spoken, mid-word, and then drops the rest of the
                # text so the record does not even show what was lost. Measured
                # live on this branch: 2 of 19 relayed group turns, one of them
                # written to the record as "I just want to". The floor moves
                # between turns, so the overlap this allows is bounded by the
                # reply that was already playing, and the one path that MUST
                # still be able to cut a character off mid-sentence — the
                # participant interrupting — does not come through here at all
                # (see _client_to_model, which cancels the speaker directly).
                has_floor = (self.room is None
                             or self.room.speaking == agent.id
                             or state["announced"])

                # If this agent gained the floor mid-response, stop suppressing:
                # the rest of the response is legitimately theirs to relay, and
                # the matching response_done must then finalize rather than be
                # swallowed. Keeping the latch would behead the reply and stall
                # the room for the full turn timeout.
                if state["suppressing"] and has_floor:
                    state["suppressing"] = False
                    # Ours is about to splice this hold in itself (below), so
                    # the mirror is spent. Left at "holding" it would be
                    # adopted a second time by the next _grant and the
                    # participant would hear the opening words twice.
                    st.drop("adopted_by_pump")
                    st.mode = "live"
                    if etype not in ("agent_audio", "agent_transcript_delta",
                                     "agent_transcript"):
                        # The floor arrived on the way OUT of this reply — its
                        # response_done, or an error. Nothing more of it will be
                        # relayed, so there is no turn for the held words to
                        # belong to, and announcing one here would put a line
                        # the participant never heard into the transcript.
                        state["held"] = []
                    # The head of this line, spoken before the floor reached it.
                    # It used to be dropped, and the turn was then written with
                    # text="" and transcript_missing True while 1.5 s of the
                    # character's voice played — a flag that tells a rater "the
                    # audio played and its text was lost" hung on a line the
                    # runner was holding all along. Measured live: g3
                    # s_1789350511_879452 at t=66.98. Put it back at the front of
                    # the turn, and on the participant's screen, in the same
                    # order it was said.
                    held = "".join(state["held"])
                    state["held"] = []
                    held_audio = bytes(state["held_audio"])
                    state["held_audio"] = bytearray()
                    if etype not in ("agent_audio", "agent_transcript_delta",
                                     "agent_transcript"):
                        held_audio = b""
                    if held or held_audio:
                        if not state["announced"]:
                            state["announced"] = True
                            state["barged_in"] = False
                            state["finalized"] = False
                            state["settled"] = asyncio.Event()
                            await self._send({
                                "type": "assistant_started",
                                "agent_id": agent.id,
                                "agent_name": agent.name,
                            })
                    if held:
                        buf.append(held)
                        await self._send({
                            "type": "assistant_text_delta",
                            "text": held,
                            "agent_id": agent.id,
                        })
                    if held_audio:
                        # THE HALF-SECOND OF BEX (measured live 2026-09-14,
                        # S3C): the gateway delivers a reply many times faster
                        # than real time, so by the time the director's grant
                        # reached a member whose reply had auto-started, most
                        # of its audio had already gone past this pump and
                        # been thrown away — 13.0 s came from the gateway,
                        # 0.5 s reached the page (agent_audio_short, delivered
                        # fraction 0.051), under a 29-word caption. The head
                        # of the line is held now, like its words, and put out
                        # first, so the participant hears the sentence the
                        # caption shows.
                        self.session.store.append_assistant_audio(
                            held_audio, agent_id=agent.id)
                        state["audio_bytes"] = (state.get("audio_bytes", 0)
                                                + len(held_audio))
                        self._advance_play_cursor(agent.id, st, held_audio)
                        await self._send_bytes(held_audio)
                        if self.room:
                            await self.room.hear(held_audio, exclude=agent.id)
                        self.session.store.event(
                            "held_audio_relayed", agent_id=agent.id,
                            segment=self.segment,
                            audio_ms=len(held_audio) // 32)

                if (etype == "response_done" and state["suppressing"]
                        and st.mode == "holding"):
                    # The suppressed reply finished before the floor reached
                    # it. origin/main keeps it for HELD_REPLY_TTL so a slow
                    # routing decision can still play it instead of paying for
                    # a second generation; adopt_member is what reads this.
                    st.mode = "held_done"
                    st.done_at = time.time()
                    self.session.store.event(
                        "held_reply_available", agent_id=agent.id,
                        held_seconds=round(st.held_seconds(), 1))

                if etype in ("agent_audio", "agent_transcript_delta",
                             "agent_transcript") and not has_floor:
                    if not state["suppressing"]:
                        state["suppressing"] = True
                        # The same moment, in origin/main's vocabulary: this
                        # member is HOLDING a reply nobody asked for. _grant
                        # and _cancel_stale_holds read that, and
                        # hold_started_at is what tells them whether the reply
                        # is an answer to the participant's current utterance
                        # or a stale reaction to a colleague.
                        st.begin_hold(ev.get("response_id"))
                        self.session.store.event(
                            "unsolicited_response_suppressed", agent_id=agent.id
                        )
                        rt.pending_input = 0
                        try:
                            await rt.cancel_response()
                        except Exception:  # noqa: BLE001
                            pass
                    # Kept, not relayed: if the floor reaches this reply before
                    # it ends, this is the only copy of its opening words. The
                    # cap is there because a suppressed reply can run for as long
                    # as the gateway wants to talk, and an unbounded list on a
                    # per-member pump is a leak with a 45-minute encounter to
                    # grow in.
                    if etype in ("agent_transcript_delta", "agent_transcript"):
                        if etype == "agent_transcript":
                            state["held"] = [ev["text"]]
                        elif len(state["held"]) < 400:
                            state["held"].append(ev["text"])
                    elif len(state["held_audio"]) < held_audio_cap:
                        # The voice of the held words, for the same reason and
                        # under the same cap in seconds (HELD_AUDIO_MAX_S).
                        state["held_audio"] += ev["pcm"]
                    # And the same chunks into the hold, ordered, for the case
                    # ours cannot serve: a suppressed reply that FINISHES before
                    # the floor reaches it. Ours splices a head into a reply
                    # still in flight; origin/main replays a completed one
                    # (adopt_member's held_done path) rather than paying the
                    # gateway for a second generation. Capped with ours so the
                    # hold cannot outgrow it.
                    if st.mode == "holding" and len(st.held) < 800:
                        if (etype == "agent_audio"
                                and st.held_seconds() < HELD_AUDIO_MAX_S):
                            st.hold(ev)
                        elif etype != "agent_audio":
                            st.hold({"type": "agent_transcript_delta",
                                     "text": ev["text"]})
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
                    state["audio_bytes"] = state.get("audio_bytes", 0) + len(ev["pcm"])
                    self._advance_play_cursor(agent.id, st, ev["pcm"])
                    await self._send_bytes(ev["pcm"])
                    if self.room:
                        await self.room.hear(ev["pcm"], exclude=agent.id)

                elif etype == "agent_transcript_delta":
                    if ev.get("first") and state["finalized"] and buf:
                        # THE DOUBLED CAPTION (measured live 2026-09-14, S3C,
                        # Rafa): the gateway answered one participant turn
                        # twice, 1.5 s apart — the reply it auto-fired and the
                        # one the floor grant's commit drew — and the second
                        # reply's opening chunk landed while the first was
                        # still being written, so it was read as the first
                        # one's late transcript and appended to it: the page
                        # showed the line twice in one bubble, then an empty
                        # bubble under the second reply's audio. The bridge
                        # marks a reply's opening chunk (`first`); one that
                        # opens on a buffer a finalize already owns is a new
                        # reply, and gets a buffer and a turn of its own. The
                        # finalize keeps the list it was handed.
                        buf = []
                        self._member_turns[agent.id] = (buf, state)
                        state["finalized"] = False
                        state["announced"] = False
                        self.session.store.event(
                            "second_reply_split", agent_id=agent.id,
                            segment=self.segment)
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
                    text = _resume_seam(buf, ev)
                    buf.append(text)
                    await self._send({
                        "type": "assistant_text_delta",
                        "text": text,
                        "agent_id": agent.id,
                    })

                elif etype == "reply_missing":
                    # This member was asked to speak (a floor grant, the scene
                    # open) and the gateway never began the reply. See the 1:1
                    # branch in _pump_events. The floor is released when the
                    # turn is NOT re-asked for, so the room's 45 s wait is
                    # never spent on a reply that is known not to be coming.
                    holds = self.room is None or self.room.speaking == agent.id
                    retried = await self._reply_missing(
                        rt, agent.id, ev, has_floor=holds)
                    if holds and not retried:
                        self._response_done.set()

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
                    if state["announced"]:
                        # The line as the gateway says it was spoken, to the
                        # page as well as the record; see the 1:1 branch.
                        await self._send({
                            "type": "assistant_text_final",
                            "text": ev["text"], "agent_id": agent.id,
                        })
                    # The gateway has declared this reply's transcript complete,
                    # so a finalize already grace-waiting on it can stop now
                    # instead of inferring the same thing from a second of
                    # silence. In a room that second was charged per speaker per
                    # turn: the floor and the next speaker wait on the same
                    # signal. See _await_transcript.
                    state["settled"].set()

                elif etype == "transcript_restreamed":
                    self.session.store.event(
                        "transcript_restreamed", agent_id=agent.id,
                        segment=self.segment)

                elif etype == "user_transcript":
                    # Member sessions hear the other characters too, so their
                    # input transcription mixes agent speech into the "user"
                    # channel. The scribe pump owns the participant transcript.
                    #
                    # EXCEPT on a route where colleagues arrive as TEXT: there a
                    # member hears only the participant, so its transcription is
                    # a clean second source and the scribe is no longer the only
                    # channel that can hear a turn. That is origin/main's rule
                    # (it gated on accepts_text_items, which on the merged table
                    # is true of every family; relay_colleagues_as_text is the
                    # column that actually means "colleagues arrive as text",
                    # and it is the same set of routes she measured). Restored
                    # 2026-09-15: the merge had kept her near-duplicate filter
                    # in _record_user_turn while dropping the second source it
                    # exists to reconcile, which left the filter with nothing to
                    # do but delete real speech.
                    if relays_colleagues_as_text(rt.model):
                        await self._record_user_turn(
                            ev["text"], garbled=bool(ev.get("garbled")))
                    continue

                elif etype == "response_done":
                    # A reply boundary, which is the burst boundary for this
                    # member's console frames too: whatever went wrong during
                    # the reply is now a finished thing with a total, and the
                    # next reply's first fault is news again. Before any of the
                    # early continues below, because every one of them still
                    # ends this reply.
                    self._flush_console_repeats(f"room:{agent.id}")
                    if ev.get("stale"):
                        # A late done for a reply this pump has already closed
                        # out (see _pump's stale branch). It may clear a latch
                        # held on that reply; it never finalises, retries, or
                        # ends the reply now streaming.
                        if state["suppressing"]:
                            state["suppressing"] = False
                            buf.clear()
                            state["held"] = []
                            state["held_audio"] = bytearray()
                        elif state["barged_in"]:
                            state["barged_in"] = False
                            if self.room is None or self.room.speaking == agent.id:
                                self._response_done.set()
                        elif state["finalized"]:
                            state["finalized"] = False
                            if self.room is None or self.room.speaking == agent.id:
                                self._response_done.set()
                        continue
                    if state["suppressing"]:
                        state["suppressing"] = False
                        buf.clear()
                        # This reply ended while still suppressed, so its held
                        # opening words belong to nothing and must not be
                        # prepended to whatever this character says next.
                        state["held"] = []
                        state["held_audio"] = bytearray()
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
                    if ev.get("retry_reason") and state["announced"]:
                        # The gateway lost this member's voice. One retry, and
                        # only for the character who still holds the floor at
                        # this instant: `room.speaking` is read here, strictly,
                        # not the `announced` leniency has_floor allows above.
                        # That leniency lets a reply the participant is already
                        # hearing finish; a retry is a NEW response.create, and
                        # issued to a member the director has moved on from it
                        # would talk over whoever holds the floor now. A reply
                        # the participant barged in on never reaches here: the
                        # bridge withholds retry_reason for one it was told to
                        # cancel, and `barged_in` was swallowed above.
                        if await self._retry_reply(
                                rt, agent.id, ev,
                                has_floor=(self.room is None
                                           or self.room.speaking == agent.id)):
                            # The turn stays open on the page and in this pump
                            # (`announced` is left up); its text and audio start
                            # over, exactly as the 1:1 path does.
                            buf.clear()
                            state["audio_bytes"] = 0
                            state["held"] = []
                            continue
                    # Finalise OFF the pump: the gateway can deliver transcript
                    # deltas after response.done, and sleeping here would stop
                    # the async-for that fills buf. The task shares buf/state and
                    # observes those late deltas while the loop keeps consuming.
                    announced_now = state["announced"]
                    state["announced"] = False
                    state["finalized"] = True
                    audio_now = state.get("audio_bytes", 0)
                    state["audio_bytes"] = 0
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
                            audio_bytes=audio_now,
                            audio_unterminated=bool(
                                ev.get("audio_unterminated")),
                            retried=bool(ev.get("retried")),
                        )
                    )

                elif etype == "tool_call":
                    # END_SEGMENT_TOOL is on every room session whose family row
                    # allows member tools — which is every family EXCEPT
                    # native-audio, the one production runs (origin/main
                    # 5a45420: that route called end_conversation constantly and
                    # each call was an empty turn, so member_tools=False there
                    # and GroupRoom._member_tools hands those members nothing).
                    # This branch is therefore unreachable on the deployed
                    # route, and "an actor ending a group conversation advances
                    # the encounter" is not true there; the loss is recorded in
                    # REALTIME_FAMILIES and docs/migration-plan.md. Where the
                    # tool IS wired, an actor ending a group conversation must
                    # advance the encounter, not be dropped. Advance off-pump so
                    # _close_room cancelling this very pump cannot interrupt the
                    # advance mid-flight.
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
                    # Recorded per occurrence, reported to the console per
                    # burst, and never awaited here: this loop relays this
                    # character's audio to the participant a few lines above,
                    # so a researcher socket awaited here would sit on the
                    # participant's audio path once per member of the room.
                    self.session.store.event(
                        "voice_error", where=f"room:{agent.id}",
                        message=redact_key(ev["message"]),
                    )
                    self._voice_error_soon(
                        source=f"room:{agent.id}", gateway_text=ev["message"],
                        transient=bool(ev.get("transient")),
                        agent_id=agent.id,
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
            # A burst still being counted when this pump ends must not die with
            # it. The stream that was failing is exactly the one whose total a
            # researcher never got to see otherwise.
            self._flush_console_repeats(f"room:{agent.id}")

    async def _finalize_member_async(self, agent, buf: List[str], announced: bool,
                                     *, interrupted: bool = False,
                                     settled=None, audio_bytes: int = 0,
                                     audio_unterminated: bool = False,
                                     retried: bool = False) -> None:
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
        await self._finalize_member(agent, text, interrupted=interrupted,
                                    audio_bytes=audio_bytes,
                                    audio_unterminated=audio_unterminated,
                                    retried=retried)

    async def _advance_from_tool(self) -> None:
        """Advance the encounter from a room member's end_conversation call."""
        try:
            if not await self._advance_segment():
                await self._send({"type": "encounter_complete"})
        except Exception as exc:  # noqa: BLE001
            self.session.store.event(
                "voice_error", where="advance_from_tool",
                message=redact_key(str(exc)),
            )
            await self._encounter_event(
                "voice_error", detail=f"advance_from_tool: {exc}",
                severity="error",
            )

    def _advance_play_cursor(self, agent_id: str, st, pcm: bytes) -> None:
        """THE SERVER-SIDE PLAYBACK CLOCK (origin/main 169310c).

        The gateway delivers a reply many times faster than real time, so
        "sent" and "heard" are different quantities and the gap is most of a
        turn. This is the only thing in the runner that knows where the
        PARTICIPANT is in the audio: `_play_cursor` is the wall-clock moment
        the last byte handed to the page will finish playing, advanced by the
        real duration of every chunk relayed (16 kHz mono PCM16 = 32000
        bytes/s).

        It has no counterpart in ours and answers a question ours could not:
        when the participant interrupts after the server-side turn is already
        over, how much of that line did they actually hear? See the
        playback_cut branch in _client_to_model, and _heard_seconds.

        It does NOT truncate the recorded text. Ours deliberately records the
        model's whole line and flags a shortfall (agent_audio_short) so a rater
        can tell a truncated delivery from a bad one; heard_seconds/heard_text
        sit BESIDE the full text rather than replacing it.
        """
        if not pcm:
            return
        now = time.time()
        start = max(now, self._play_cursor)
        if st is not None:
            if st.play_start is None:
                st.play_start = start
            st.relayed_bytes += len(pcm)
        self._play_cursor = start + len(pcm) / 32000.0
        if st is not None:
            st.play_end = self._play_cursor
        self._last_played = {
            "agent_id": agent_id,
            "start": st.play_start if st is not None else start,
            "end": self._play_cursor,
            "text": "".join(st.text) if st is not None and st.text else
                    (self._last_played or {}).get("text", ""),
        }

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
        # True from the moment this reply has been told to stop until its
        # response_done. See the cancel branch below.
        stopping = False
        try:
            async for ev in rt.events():
                etype = ev.get("type")
                if etype == "user_transcript":
                    await self._record_user_turn(
                        ev["text"], garbled=bool(ev.get("garbled")))
                elif etype in ("agent_audio", "agent_transcript_delta",
                               "agent_transcript"):
                    # Discarded either way — the `elif` is what keeps the scribe
                    # out of the participant's ears. The cancel is only there to
                    # stop the gateway generating the rest of a reply nobody
                    # will hear, so it is sent while there is a reply to stop
                    # and not once per frame of one.
                    #
                    # Unguarded this was a no-op for as long as nothing ever
                    # closed the scribe's buffer: a channel that is never
                    # committed is never answered, so there was nothing to
                    # cancel and nothing to see. Now that the participant's turn
                    # IS closed here (see _client_to_model), the scribe answers
                    # every turn, and one cancel per transcript frame put four
                    # or five `response_cancel_not_active` errors per
                    # participant turn into events.jsonl and onto the
                    # researcher console — 16 of them across three turns,
                    # measured on gpt-realtime-2.1 — which is a real channel's
                    # error stream filled with noise from a channel that is
                    # working correctly.
                    #
                    # Two conditions, because the session's own flag is not
                    # enough on its own. `responding` is down for a whole-line
                    # `agent_transcript` that lands after response.done — there
                    # is genuinely no reply left to cancel there — but it comes
                    # straight back UP on the next delta, because events() sets
                    # it on every response delta, auto-fired or asked for,
                    # before the event is yielded. A cancelled reply keeps
                    # delivering deltas on BOTH families (44 more were measured
                    # after one cancel on Gemini; on gpt-realtime the tail is
                    # shorter but real), so the flag alone still sent a cancel
                    # per delta and still drew two or three errors per turn.
                    # `stopping` is therefore a per-reply latch, cleared at
                    # response_done below, exactly as _pump_member's
                    # `suppressing` latch is: one cancel per reply, and the
                    # rest of that reply is simply dropped on the floor, which
                    # is all the pump was ever relying on.
                    if rt.responding and not stopping:
                        stopping = True
                        try:
                            await rt.cancel_response()
                        except Exception:  # noqa: BLE001
                            pass
                elif etype == "response_done":
                    rt.clear_response_state()
                    # Reply boundary: this reply is over, so the next one is
                    # allowed its own single cancel. A latch that outlived its
                    # response would leave the scribe generating a whole reply
                    # unchecked.
                    stopping = False
                    # Reply boundary: close out any burst of console frames the
                    # scribe's stream was producing, the same way the other two
                    # pumps do.
                    self._flush_console_repeats("scribe")
                elif etype == "error":
                    # Per-occurrence in the record, per-burst on the console,
                    # and dispatched rather than awaited — the participant's
                    # transcript is relayed from this same loop.
                    self.session.store.event(
                        "voice_error", where="scribe",
                        message=redact_key(ev["message"]),
                    )
                    self._voice_error_soon(
                        source="scribe", gateway_text=ev["message"],
                        transient=bool(ev.get("transient")),
                    )
        except asyncio.CancelledError:
            cancelled = True
            return
        finally:
            # Whatever the stream was doing when it ended, the count goes out.
            self._flush_console_repeats("scribe")
            # A cancel is _close_room tearing the room down deliberately, which
            # is not a lost channel; anything else is the socket going away
            # under a live encounter, which is.
            if not cancelled and not self._closed:
                self._scribe_lost = True
                self.session.store.event(
                    "scribe_pump_ended", segment=self.segment,
                    interaction=self._interaction_id(),
                )
                # The participant is still audible to nobody but the WAV from
                # here on, and on the console their transcript simply stops. A
                # researcher who sees this can end the encounter; one who does
                # not watches a participant apparently fall silent for the rest
                # of it, which is a rateable ESCI behaviour.
                #
                # Dispatched, not awaited, so that it queues BEHIND the burst
                # total the flush above just dispatched. Awaiting it here put
                # the channel's death on the strip above the count of the
                # errors that killed the channel — researcher.html renders in
                # arrival order and the two frames share a `t`, so nothing
                # downstream could put them back in the order they happened.
                self._encounter_event_soon(
                    "scribe_pump_ended",
                    detail="the room's participant transcription channel ended; "
                           "no participant turns will be recorded from here",
                    severity="error",
                )
                await self._send({
                    "type": "error",
                    "message": "The transcription channel was lost.",
                })

    async def _finalize_member(self, agent, text: str,
                               *, interrupted: bool = False,
                               audio_bytes: int = 0,
                               audio_unterminated: bool = False,
                               retried: bool = False) -> None:
        """Close one character's turn in a group room."""
        text = _clean_agent_text(text)
        # origin/main df1ab83, and taken unconditionally: both are cheap, both
        # only fire on text, and both are the visible symptom of the deployed
        # route. _strip_context_echo removes a colleague note the model parroted
        # back ("[Jordan says]: It's fine." in front of the actual reply);
        # _is_stage_direction catches "[Priya remains quiet.]", which is the
        # model narrating instead of speaking and is recorded as no reply
        # rather than as a line the participant heard.
        text = _strip_context_echo(
            text, [line for aid, line in self._recent_told
                   if aid != agent.id])
        if _is_stage_direction(text):
            self.session.store.event("stage_direction_output",
                                     agent_id=agent.id, text=text)
            text = ""
        await self._finalize_member_inner(agent, text, interrupted=interrupted,
                                          audio_bytes=audio_bytes,
                                          audio_unterminated=audio_unterminated,
                                          retried=retried)

    async def _finalize_member_inner(self, agent, text: str,
                                     *, interrupted: bool = False,
                                     audio_bytes: int = 0,
                                     audio_unterminated: bool = False,
                                     retried: bool = False) -> None:
        """Close one character's turn in a group room.

        This was lost in a refactor once, and the symptom was total: every pump
        died with AttributeError at its first response.done, silently, so no
        reply ever reached the participant and every routed turn timed out.

        `interrupted` marks a turn the participant talked over, exactly as
        _finalize_turn marks it in a 1:1 encounter.
        """
        text, retry_head = self._retry_head_if_empty(agent.id, text, retried)
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
        # not greet again" — a one-off instruction, not a standing one. The
        # lead's folded opening (see _fold_opening) is spent the same way.
        self._scene_note = ""
        if self._opening_agent == agent.id:
            self._opening_note = ""
            self._opening_agent = None

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
            # The transcript broadcast above is inside `if text`, so a turn
            # that produced no text used to put nothing on the researcher's
            # screen at all: the character just went quiet, which reads as them
            # choosing not to speak rather than as the transcript being lost.
            # Say which it is while the encounter can still be stopped.
            await self._encounter_event(
                "transcript_missing", agent_id=agent.id,
                detail="the character spoke and no transcript arrived",
            )
        # Once the scribe is gone there is no participant channel, and repeating
        # the last utterance it managed to hear would make the record assert
        # that the participant said a specific sentence immediately before turns
        # they said nothing before. Say the channel was lost instead.
        self.session.store.event(
            "steering_pair",
            # Where this turn happened, stated on the TURN.
            #
            # It used to be carried only inside `direction`, and encounter_record
            # still reads it from there, so an actor turn that ran without a
            # stage direction arrived in the record with segment and interaction
            # both null. That is not a rare turn: an interaction's beats are
            # finite, and every turn after the last one is spent runs unsteered,
            # so the tail of every interaction loses its attribution — reliably,
            # and in the half of the record a rater reads by segment. Which
            # interaction a line belongs to is a fact about when it was spoken,
            # not about whether anyone was directing at the time.
            segment=self.segment,
            interaction=self._interaction_id(),
            direction=direction,
            actor={"agent_id": agent.id, "text": text,
                   # What the session was actually speaking with, not the
                   # scenario's `voice_id` — which on a group scenario is an
                   # ElevenLabs id that never reached the gateway, so the record
                   # named a voice the participant certainly did not hear.
                   "voice": self._member_voice(agent),
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
        delivered_ms = turn_audio.audio_ms(audio_bytes)
        # The playback clock's idea of what is currently in the participant's
        # ears. Written here because this is where the whole line is finally
        # known; the cursor itself was advanced chunk by chunk as it was sent.
        if (self._last_played or {}).get("agent_id") == agent.id:
            self._last_played["text"] = text
        self.session.store.event(
            "assistant_turn", agent_id=agent.id, text=text,
            segment=self.segment, transcript_missing=not text,
            interrupted=interrupted, retry_head=retry_head,
            # See _finalize_turn, and server/voice/turn_audio.py.
            audio_ms=delivered_ms,
        )
        if not interrupted:
            self._note_audio_shortfall(agent.id, text, delivered_ms,
                                       audio_unterminated)
        if retried:
            self._note_retry_outcome(agent.id, text, delivered_ms,
                                     interrupted, audio_unterminated)
        # origin/main ce8cdf4/df1ab83: tell the members that are NOT fanned this
        # character's audio what it just said, now that the line is finished.
        # GroupRoom.tell is itself gated on the family row, so on plain flash
        # (where colleagues are still fanned as audio) this is a no-op and the
        # measurement our fan-out counters were taken on is unchanged.
        # _recent_told is what _strip_context_echo checks a reply against.
        if self.room is not None and text:
            self._recent_told = (self._recent_told + [(agent.id, text)])[-6:]
            try:
                await self.room.tell(agent.name, text, exclude=agent.id)
            except Exception:  # noqa: BLE001 - a dead member must not eat a turn
                pass
        st = self._member_states.get(agent.id)
        if st is not None and st.mode in ("live", "held_done", "discarding"):
            st.drop("turn_finalized")
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

    def _second_transcript_source(self) -> bool:
        """True when more than one session can transcribe the participant.

        Only in a ROOM, and only on a family whose members are told what a
        colleague said in text instead of being fanned its audio: there a
        member's input holds the participant and nobody else, so _pump_member
        forwards its transcripts as a clean second source (origin/main's rule).
        Everywhere else — every 1:1 encounter, and every room on plain flash —
        the scribe is the only channel and two transcripts of one utterance
        cannot happen. The near-duplicate filter in _record_user_turn is gated
        on this; see the comment there for what it did without it.
        """
        if self.room is None:
            return False
        model = getattr(self.rt, "model", "") or realtime_model()
        return relays_colleagues_as_text(model)

    async def _record_user_turn(self, text: str, *, garbled: bool = False) -> None:
        """Record one participant utterance, once, as said.

        `garbled` is the bridge saying the transcriber dropped part of this
        line (its "{}" placeholder; see voice/realtime.py). A line that was
        NOTHING but placeholders arrives with empty text: the participant
        spoke and none of it was transcribed, which the record says as
        `user_turn_untranscribed` rather than saying nothing at all.
        """
        if not text:
            if garbled:
                self.session.store.event(
                    "user_turn_untranscribed", channel="voice",
                    segment=self.segment,
                )
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
        # Several sessions can transcribe the same utterance with slightly
        # different wording (the scribe plus a member on a route where
        # colleagues arrive as text, so the member hears only the participant):
        # within a short window, a near-match is the same turn. origin/main's
        # rule, kept whole — 60% of the shorter line's words, 5 s.
        #
        # TWO things about where it sits, both fixed on 2026-09-15.
        #
        # It is AFTER the echo guard now, not before. A line that is both an
        # echo of a character and a near-match of the last participant turn is
        # an echo: `echo_dropped` names the character it matched and is what a
        # retranscribe pass adjudicates, while `user_transcript_duplicate_dropped`
        # says only "we have seen this". Running the looser test first relabelled
        # the more specific finding as the vaguer one.
        #
        # And it only runs where a second transcriber EXISTS. It ran on every
        # family, and on plain flash — the route every measurement in this file
        # was taken on — _pump_member throws its members' user transcripts away,
        # so the scribe is the only channel and nothing can produce a duplicate.
        # All the rule could do there was delete real speech: probed live,
        # "yes" then "yes exactly" 0.2 s later, and "I think the deadline
        # slipped" then "...slipped a lot", were both dropped on plain flash.
        # A participant who builds on their own sentence does exactly that.
        if self._second_transcript_source() and norm and self._last_user_norm \
                and now - self._last_user_at < 5:
            a, b = set(norm.split()), set(self._last_user_norm.split())
            if a and b and len(a & b) / min(len(a), len(b)) >= 0.6:
                self.session.store.event("user_transcript_duplicate_dropped", text=text)
                return
        self._last_user_norm, self._last_user_at = norm, now
        self._last_user_text = text
        self._user_utterances += 1
        self.session.append_user(text)
        unclear = _script_mismatch(text)
        self.session.store.event(
            "user_turn", text=text, channel="voice", script_mismatch=unclear,
            utterance=self._user_utterances, garbled=garbled,
        )
        # The research record keeps the raw text (retranscribe repairs it
        # offline); the participant only sees a neutral caption, since a line
        # of foreign script reads as "the app is broken". `utterance` numbers
        # the line, so a page that consolidates captions can tell a second
        # utterance from the continuation of one.
        await self._send({
            "type": "user_transcript", "text": text, "final": True, "unclear": unclear,
            "utterance": self._user_utterances, "garbled": garbled,
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
        # A fresh scene: its framing goes into this connect brief on a family
        # that reads no other (see _fold_opening). Before the instructions are
        # built, or the note is not in them.
        self._fold_opening(agent, group=False)
        new_rt = self._new_session(
            instructions=self._instructions_for(agent),
            voice=self._voice_for(agent),
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
                agent_id=agent.id, message=redact_key(str(exc)),
            )
            await self._encounter_event(
                "voice_error", agent_id=agent.id,
                detail=f"switch_character: {exc}", severity="error",
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
                        acked = await self._deliver_brief(
                            new_rt, self._instructions_for(agent)
                        )
                    # `acked` here is the load-bearing one for a continued
                    # session: this re-brief is what tells the actor it is now
                    # in interaction 2 and must not greet again. Unacknowledged,
                    # the character carries on under interaction 1's brief while
                    # the record has already moved on, which is a segment
                    # boundary the transcript will not agree with.
                    self.session.store.event(
                        "realtime_session_continued",
                        agent_id=agent.id, agent_name=agent.name,
                        interaction=self._interaction_id(), acked=acked,
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
            # A line said to the previous character is not replayed to this
            # one.
            self._replay_pcm.clear()
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
                wanted=agent.id, kept=prev_id, message=redact_key(str(failure)),
            )
            await self._encounter_event(
                "voice_error", agent_id=agent.id,
                detail=f"enter: {failure}", severity="error",
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
                # The first interaction's framing, into the one brief the
                # configured family will ever read (see _fold_opening).
                self._fold_opening(self.agent, group=False)
                self.rt = self._new_session(
                    instructions=self._instructions(),
                    voice=self._voice(),
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
            # The roster this encounter's voices were chosen from, on the
            # record beside the model that dictated it. An analyst asking why
            # two characters sounded alike, or why one never spoke, is asking
            # about the interaction between a scenario's casting and a model's
            # roster, and that is not reconstructable after the fact from the
            # model name alone once the table has moved on.
            self.session.store.event(
                "realtime_session_started", model=self.rt.model,
                voices_offered=realtime_voices(),
                **provenance()
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
            # And the console frames still in flight, for the same reason. They
            # leave as tasks so a researcher's socket can never sit on the
            # participant's audio path — but a task that was only SCHEDULED is
            # not a frame that was sent, and run() returning takes the loop out
            # from under it. The frames spawned last are the burst total and
            # the terminal error that ended the encounter: the two a researcher
            # most needs, silently lost at any console round trip worth the
            # name. Placed after the teardown above, because the room pumps
            # flush their bursts as they are cancelled. Bounded, and by less
            # than the finalize wait, so a console that has stopped reading
            # cannot hold the encounter open either.
            if self._console_tasks:
                try:
                    await asyncio.wait(list(self._console_tasks), timeout=2.0)
                except asyncio.CancelledError:
                    cancelled_while_waiting = True
            if cancelled_while_waiting:
                raise asyncio.CancelledError

    async def _on_turn_ended(self) -> None:
        """The participant's turn is over: route a room, or brief and commit a
        1:1 reply. This is the block that used to sit under `turn_ended` in
        _client_to_model, unchanged; what changed is WHEN it runs on the family
        whose gateway window is longer than the runner's own bar (see
        _end_of_turn_confirm_ms)."""
        self._turn_started_at = time.time()
        if self.is_group() and self.room is not None:
            # Close the participant's turn on the room's
            # transcription channel FIRST, and await it.
            #
            # The scribe is the only participant channel a room has
            # — _pump_member throws its own user_transcript events
            # away on purpose, because a member's input buffer also
            # carries the other characters' fanned-out audio and the
            # bridge labels all of it "user" — and a buffer is only
            # transcribed when it is committed. give_floor commits
            # the member it is granting and nobody else, so on a
            # family whose own turn detection is switched off
            # (`floor_is_real`; see server/voice/realtime.py's
            # REALTIME_FAMILIES row for gpt-realtime) NOTHING closed
            # the scribe's buffer and the participant was never
            # transcribed at all. Measured on gpt-realtime-2.1
            # against api.ai.it.cornell.edu on a real S4A room, with
            # three synthetic participant utterances: five
            # assistant_turns, 2.18 MB of participant audio in
            # user_audio.wav, and participant_turns = 0 in the
            # record — characters answering somebody who, on paper,
            # said nothing. The participant's speech is the
            # dependent variable, so that encounter cannot be rated.
            #
            # THIS is the moment, and it is the only one the runner
            # has. The room is handed audio and never told when the
            # talking stopped; `turn_ended` is where the runner
            # knows, and it is the same mark that spawns the group
            # turn below. give_floor is the wrong place twice over:
            # it runs after routing, so the transcript would land a
            # turn late, and it does not run at all on a turn the
            # director answers with silence. close_participant_turn
            # is idempotent per turn (it zeroes its own byte counter
            # before committing) and refuses a buffer this room put
            # nothing in, so a doubled or empty commit — a
            # duplicated or fabricated participant turn, which is
            # worse than a missing one — cannot come out of here.
            #
            # Awaited rather than dispatched, because _run_group_turn
            # spends up to ROUTE_TRANSCRIPT_WAIT waiting for exactly
            # this transcript before it routes, and _named_in reads
            # it to see whether the participant addressed somebody
            # by name. One await here and the director routes on the
            # turn it is routing rather than on the previous one.
            #
            # A no-op on the configured Gemini family, by its own
            # first line: that gateway's turn detection closes the
            # participant's buffer itself, so the room commits
            # nothing, pays for nothing, and the path that works
            # today is not touched.
            await self.room.close_participant_turn()
            # Tracked (see _spawn_group_turn) so an interaction
            # switch can cancel whichever turn holds self._floor.
            #
            # Not twice for one turn. On the configured family the
            # runner's bar fires inside a lost participant's
            # mid-thought pause and again at the real end; the
            # first spawn is still waiting for the transcript the
            # gateway will only send at the real end, and routes
            # on it. A second routing of the same words is a
            # second speaker granted for one participant turn.
            if self._group_turn_waiting:
                self.session.store.event(
                    "turn_end_withdrawn", agent_id=self.agent_id,
                    segment=self.segment, group=True,
                )
            else:
                self._group_turn_waiting = True
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
            # Per family, not one number. This line read the raw env with a
            # 1.5 s default until 2026-09-15, which was the right bar for the
            # model every 1:1 measurement above was taken on and the wrong one
            # for the model production runs: the native-audio route fires its
            # own reply ~3.3 s after silence (docs/migration-plan.md), so a
            # 1.5 s wait expired ~1.8 s early on EVERY 1:1 turn and the commit
            # below landed on top of a reply that was still coming — the exact
            # double-reply this wait exists to prevent, on S1 and S2, i.e. the
            # whole one-to-one arm. AUTOFIRE_WAIT still overrides, because that
            # env knob is what this default was; see autofire_wait_for_model.
            # The group twin of this wait (_grant) was already family-aware.
            deadline = time.time() + autofire_wait_for_model(self.rt.model)
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
                    # The clock every held reply is judged against: a reply
                    # that BEGAN before this moment is a reaction to a
                    # colleague, not an answer to what the participant is
                    # saying now (adopt_member / _cancel_stale_holds read it).
                    self._speech_started_at = time.time()
                    self._barged = False
                    if self.room is not None:
                        # STALE-HOLD CANCEL (origin/main 6917d5b), taken as-is
                        # and with no counterpart in ours. On the native-audio
                        # route a member whose response is still active when
                        # the participant speaks NEVER answers the new turn:
                        # the speech is absorbed into the running response and
                        # the fallback request comes back empty. Cancelling at
                        # the bridge frees the model for the participant's
                        # turn. It is the difference between a member answering
                        # the next question and never answering again.
                        await self._cancel_stale_holds()
                # Cutting a character off is a SEPARATE decision from opening
                # the participant's turn, and it is taken on a separate,
                # stricter signal (see SilenceDetector.barge_in).
                #
                # It used to be taken here, on `speech_started` itself, and that
                # is the mechanism behind the report that "some of the dialogue
                # would sound really good then get cut off early". A
                # `speech_started` cancels the reply and tells the page
                # `assistant_interrupted`, and the page's handler stops every
                # audio buffer it has already scheduled — which, because the
                # gateway delivers a reply many times faster than real time, is
                # most of the reply. Measured on this branch: 87% and 65% of two
                # turns' delivered audio thrown away unheard, 79% across every
                # interrupted turn, with NOTHING on the participant's mic but
                # keyboard noise. The server transcript and the WAV are complete
                # for those turns, which is why nothing server-side and no test
                # ever saw it.
                #
                # `barge_in` is True only for speech that is both loud enough to
                # stand clear of this room's own noise floor and sustained
                # enough not to be a click, a breath or a chair. Offline on 60 s
                # of each bed, false cut-offs went 230 -> 0 (fan at RMS 600),
                # 30 -> 0 (typing), 15 -> 0 (breathing), 13 -> 0 (the page's own
                # playback bleeding into the mic). A real interjection still
                # lands, in 0.42-0.9 s of synthetic speech against 0.38-0.46 s
                # for the shipped gate: one beat later, and the beat is paid in
                # the safe direction — the character talks a moment longer
                # rather than the participant losing the rest of a sentence.
                # Live, against the same gateway, that cost did not show up at
                # all: 6 of 12 deliberate interjections cancelled the speaker
                # after, against 2 of 9 before, because the shipped detector
                # latched on the room's own noise and then could not fire again
                # until it had had 900 ms of unbroken quiet.
                barged = self.vad.barge_in and not self._was_barging
                self._was_barging = self.vad.barge_in
                if barged and self.room is not None and not self.room.speaking \
                        and time.time() < self._play_cursor:
                    # THE TURN IS OVER ON THE SERVER AND NOT IN THE ROOM
                    # (origin/main 169310c). Nobody holds the floor, so the two
                    # branches below have nothing to cancel -- but the page is
                    # still playing audio this runner sent minutes of wall
                    # clock ago in seconds of stream time, and the participant
                    # is talking over it. Without this the interruption is
                    # invisible: no event, no truncation, and a rater reads a
                    # participant reply to a line they were still hearing.
                    #
                    # The record gets heard_seconds / heard_text BESIDE the
                    # full line, never instead of it: ours deliberately keeps
                    # the model's whole reply and flags the shortfall, so a
                    # rater can tell a truncated DELIVERY from a bad reply.
                    self._barged = True
                    lp = self._last_played or {}
                    heard = 0.0
                    if lp.get("start") is not None:
                        heard = max(0.0, time.time() - lp["start"])
                    total = (lp.get("end") or 0) - (lp.get("start") or 0)
                    words = (lp.get("text") or "").split()
                    heard_words = (len(words) if total <= 0
                                   else min(len(words),
                                            int(len(words) * heard / total)))
                    self.session.store.event(
                        "playback_cut", agent_id=lp.get("agent_id"),
                        segment=self.segment,
                        heard_seconds=round(heard, 1),
                        total_seconds=round(total, 1),
                        heard_text=" ".join(words[:heard_words])
                                   + ("\u2026" if heard_words < len(words) else ""),
                    )
                    self._play_cursor = time.time()
                    await self._send({"type": "assistant_interrupted"})
                if barged:
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
                            audio_now = state.get("audio_bytes", 0)
                            state["audio_bytes"] = 0
                            self._spawn_finalize(
                                self._finalize_member_async(
                                    agent, buf, announced_now, interrupted=True,
                                    settled=state.get("settled"),
                                    audio_bytes=audio_now,
                                    # A barge-in on a reply being re-asked
                                    # for: the retry's outcome is written
                                    # (not recovered) rather than never.
                                    retried=bool(getattr(
                                        speaker, "_retry_in_flight", False)),
                                )
                            )
                        else:
                            # No live pump for the floor holder (its session
                            # died, or the room was rebuilt underneath us).
                            # There is no turn to write, but the floor still
                            # has to come back or the room goes quiet.
                            self._response_done.set()
                        # What the participant had actually heard of the line
                        # they cut off, from the playback clock. Recorded
                        # beside the turn, not in place of it.
                        cut_st = self._member_states.get(speaking_id)
                        if cut_st is not None and cut_st.play_start is not None:
                            self.session.store.event(
                                "playback_cut", agent_id=speaking_id,
                                segment=self.segment,
                                heard_seconds=round(
                                    self._heard_seconds(cut_st), 1),
                                total_seconds=round(
                                    (cut_st.play_end or cut_st.play_start)
                                    - cut_st.play_start, 1),
                            )
                        self._barged = True
                        self._play_cursor = time.time()
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
                            audio_bytes=self._take_turn_audio(),
                            # See the group branch above: a retry the
                            # participant talked over still gets its outcome.
                            retried=bool(getattr(self.rt, "_retry_in_flight",
                                                 False)),
                        ))
                        await self._send({"type": "assistant_interrupted"})

                if self.room is not None:
                    await self.room.hear(pcm)
                else:
                    await self.rt.send_audio(pcm)
                    self._keep_for_replay(pcm)

                if mark == "turn_ended":
                    confirm = self._end_of_turn_confirm_ms()
                    if confirm > 0 and not (self.is_group() and self.room is not None):
                        # THE PAUSE SPLIT, 1:1. The runner's bar (900 ms) fired
                        # inside a lost participant's 700-1300 ms mid-thought
                        # pause, and the commit that followed handed the
                        # gateway half a thought ("I need more context." /
                        # "What do you mean?", answered twice). The family's
                        # gateway window is 1500 ms (measured honoured), so the
                        # turn end is HELD until that much quiet has actually
                        # been heard: `confirm` more ms of sub-bar audio, counted
                        # in audio time like the detector itself. Speech that
                        # resumes inside it withdraws the end — nothing briefed,
                        # nothing committed — and the turn goes on. The VAD's
                        # own marks are untouched, so the barge-in gate, the
                        # "your turn" cue and the room path keep their timing.
                        self._turn_end_pending_ms = float(confirm)
                    else:
                        await self._on_turn_ended()
                elif self._turn_end_pending_ms is not None:
                    if mark == "speech_started" or self.vad.speaking:
                        self._turn_end_pending_ms = None
                        self.session.store.event(
                            "turn_end_withdrawn", agent_id=self.agent_id,
                            segment=self.segment,
                            confirm_ms=self._end_of_turn_confirm_ms(),
                        )
                    elif _realtime._rms(pcm) < self.vad.effective_threshold():
                        self._turn_end_pending_ms -= len(pcm) / 2 / self.vad.rate * 1000.0
                        if self._turn_end_pending_ms <= 0:
                            self._turn_end_pending_ms = None
                            await self._on_turn_ended()
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001, surfaced in the session log
            self.session.store.event(
                "voice_error", where="client_to_model",
                message=redact_key(str(exc)),
            )
            await self._encounter_event(
                "voice_error", detail=f"client_to_model: {exc}", severity="error",
            )

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
                    await self._encounter_event(
                        "voice_error", agent_id=self.agent_id,
                        detail="model_to_client: realtime session never "
                               "reconnected; the encounter is ending",
                        severity="error",
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
            if self._switching:
                self._switching = False
                continue
            if await self._reconnect_after_gateway_close(rt):
                continue
            return

    async def _pump(self, rt) -> None:
        """Relay one realtime session to the participant until its stream ends.

        A thin wrapper so the console's coalescing survives every way this pump
        can end — the generator finishing, the tool_call branch returning, and
        above all _model_to_client or run()'s teardown cancelling it. A burst of
        gateway errors still being counted when the stream dies must not die
        with it: the stream that was failing is precisely the one whose total
        the researcher never got to see.
        """
        try:
            await self._pump_events(rt)
        finally:
            self._flush_console_repeats("model")

    async def _pump_events(self, rt) -> None:
        # The bridge's audio-absent clock is held while the participant is
        # talking (see voice/realtime.py AUDIO_ABSENT_S); this is how it knows.
        rt.participant_speaking = lambda: bool(self.vad.active_within())
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
                self._turn_audio_bytes += len(ev["pcm"])
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
                text = _resume_seam(target, ev)
                target.append(text)
                await self._send({
                    "type": "assistant_text_delta",
                    "text": text,
                    "agent_id": self.agent_id,
                })

            elif etype == "reply_missing":
                # A reply this runner asked for and the gateway never began
                # (see voice/realtime.py REQUEST_UNANSWERED_S). No turn is open,
                # so nothing is finalised; the participant is otherwise sitting
                # in dead air with a character that has "stopped hearing" them.
                await self._reply_missing(rt, self.agent_id, ev, has_floor=True)

            elif etype == "agent_transcript":
                # The gateway's own end-of-transcript event carries the WHOLE
                # line. It was being ignored while the finalizer inferred
                # completeness from a stream of deltas; take it as authoritative
                # and replace the buffer, so a turn whose text arrived piecemeal
                # after response.done is recorded whole rather than as its first
                # fragment.
                target = await self._transcript_target(whole_line=True)
                target[:] = [ev["text"]]
                if target is self._agent_text:
                    # The page has the deltas; this is the line the gateway
                    # itself says was spoken, and the record keeps this one.
                    # Hand the page the same line, so a caption built from
                    # deltas the gateway then re-sent (see the bridge's
                    # transcript_restreamed) or garbled at a seam shows what
                    # the record shows.
                    await self._send({
                        "type": "assistant_text_final",
                        "text": ev["text"], "agent_id": self.agent_id,
                    })
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
                # The gateway has heard the participant up to here, so there
                # is nothing to replay (see REPLAY_KEEP_S).
                self._replay_pcm.clear()
                await self._record_user_turn(
                    ev["text"], garbled=bool(ev.get("garbled")))

            elif etype == "transcript_restreamed":
                # The gateway sent this reply's transcript a second time inside
                # the same response; the bridge swallowed the repeat. On the
                # record so a doubled caption, if one ever reappears, can be
                # told from a doubled line.
                self.session.store.event(
                    "transcript_restreamed", agent_id=self.agent_id,
                    segment=self.segment)

            elif etype == "response_done":
                # A reply boundary, and the one that fires even when EVERY audio
                # frame of the reply was corrupt (in which case no turn was ever
                # begun and _begin_agent_turn never ran). Rearming here is what
                # makes the transient-error notice above "one per reply" rather
                # than one per encounter.
                self._transient_error_notified = False
                # The console frame is latched per reply for the same reason and
                # on the same boundary, so the two surfaces agree about what a
                # reply's worth of corrupt audio was. This flush is what sends
                # the total; without it the count would be carried into the next
                # reply and reported against the wrong one.
                self._flush_console_repeats("model")
                if ev.get("stale"):
                    # The done of a reply the bridge had already closed out -
                    # cancelled at a barge-in, or ended by a watchdog - arriving
                    # late under its own id (see RealtimeVoiceSession
                    # ._response_created_id). It is not a boundary of whatever
                    # is streaming now: measured live, treated as one it ended
                    # a healthy reply's turn mid-stream and then had that reply
                    # judged truncated and re-spoken.
                    continue
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
                if ev.get("retry_reason") and await self._retry_reply(
                        rt, self.agent_id, ev):
                    # The gateway lost this reply's voice and has been asked
                    # for it again (see _retry_reply). The turn stays OPEN:
                    # `_speaking` is left up so the fresh reply's deltas and
                    # audio land in this same turn rather than opening a second
                    # assistant_started on the page, and its buffer and byte
                    # count start over so the record holds the line that was
                    # actually heard, not the abandoned head glued onto it. The
                    # pending stage direction is untouched; it pairs with the
                    # turn, whichever attempt delivers it.
                    self._agent_text = []
                    self._turn_audio_bytes = 0
                    self._agent_line_settled = False
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
                    audio_bytes=self._take_turn_audio(),
                    # Whether the GATEWAY closed this reply's audio stream or
                    # simply stopped sending; see voice/realtime.py. Carried so
                    # a short turn can say which side of the gateway lost it.
                    audio_unterminated=bool(ev.get("audio_unterminated")),
                    # Whether this reply was the turn's one retry, so the record
                    # can say whether the second attempt was heard whole.
                    retried=bool(ev.get("retried")),
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
                self.session.store.event(
                    "voice_error", where="model", message=redact_key(ev["message"])
                )
                # The console gets one frame per BURST, not one per chunk, and
                # gets it without this loop ever awaiting a researcher socket:
                # agent_audio is relayed from the same async-for a few branches
                # up, so an awaited console send here would let a researcher
                # whose browser has stopped reading backpressure the
                # participant's audio. That could not happen before the console
                # existed and must not become possible because of it.
                self._voice_error_soon(
                    source="model", gateway_text=ev["message"],
                    transient=bool(ev.get("transient")),
                    agent_id=self.agent_id,
                )
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
                # The gateway's own words go to the participant's browser, so
                # this is the one error string in the runner that leaves the
                # building verbatim; a 401 body that echoes the credential back
                # would otherwise be handed to a publicly recruited stranger.
                #
                # As `error` unless the bridge marked the fault `recoverable`
                # — a stall or a cut-off reply the encounter goes on past, or
                # a socket the gateway closed, which _model_to_client rebuilds
                # (and says `error` itself only when it cannot). Those used to
                # go out as `error` too, and the page reads ANY `error` frame
                # as "the server stated a failure": the next socket drop,
                # whatever its cause, is then the fatal kind, and the SECOND
                # fatal drop hides the Reconnect button. That is the
                # "connection lost and I couldn't retry": one 47 s gateway
                # stall earlier in the encounter had already spent the
                # participant's retry. A notice frame carries the same text
                # for a page that wants it and asserts nothing about whether
                # the encounter can continue. A fault with no such mark (a
                # refused key, a corrupt chunk) still reaches the participant
                # as it always did.
                await self._send({
                    "type": "voice_notice" if ev.get("recoverable") else "error",
                    "message": redact_key(ev["message"]),
                    "transient": bool(ev.get("transient")),
                })
                if (ev.get("retry_unanswered") and self.room is None
                        and not self._closed and rt is self.rt):
                    # The gateway ignored a request and then its retry. On
                    # every live measurement that socket is dead and the
                    # gateway drops it 22-38 s later with no close frame;
                    # waiting for that is dead air, and everything the
                    # participant says into it is lost. Close it now and let
                    # _model_to_client rebuild the session as this character —
                    # marked, because a close of our own is otherwise the
                    # deliberate kind it must not rebuild after — and replay
                    # the unanswered line into the new one.
                    self._rebuild_for_replay = True
                    self.session.store.event(
                        "gateway_socket_abandoned", agent_id=self.agent_id,
                        segment=self.segment, waited_s=ev.get("waited_s"),
                        replay_ms=len(self._replay_pcm) // 32,
                    )
                    await rt.close()
                    return

    def _keep_for_replay(self, pcm: bytes) -> None:
        """Hold the participant's 1:1 audio for a possible replay, bounded to
        the last REPLAY_KEEP_S seconds (see REPLAY_KEEP_S)."""
        self._replay_pcm += pcm
        cap = int(REPLAY_KEEP_S * self.vad.rate) * 2
        if len(self._replay_pcm) > cap:
            del self._replay_pcm[:len(self._replay_pcm) - cap]

    def _replay_speech(self) -> bytes:
        """The held participant audio, compacted for replay: leading quiet
        dropped, every run of quiet inside it cut to REPLAY_PAUSE_MS, the
        whole capped to the last REPLAY_MAX_S seconds. Empty when it holds
        less than REPLAY_MIN_SPEECH_MS of anything voice-like, so a stretch of
        room tone is never put in front of the model as a turn.

        Voice-like is the VAD's own hint bar (SilenceDetector.hint_threshold),
        the one that fires on a soft voice, so a quiet speaker's line is
        replayed and not trimmed away as silence."""
        pcm = bytes(self._replay_pcm)
        frame = int(self.vad.rate * 0.02) * 2
        if not pcm or frame <= 0:
            return b""
        bar = self.vad.hint_threshold()
        keep_quiet = max(1, REPLAY_PAUSE_MS // 20)
        out = bytearray()
        quiet_run = 0
        speech_ms = 0
        leading = True
        for i in range(0, len(pcm) - frame + 1, frame):
            f = pcm[i:i + frame]
            if _realtime._rms(f) >= bar:
                leading = False
                quiet_run = 0
                speech_ms += 20
                out += f
            elif not leading:
                quiet_run += 1
                if quiet_run <= keep_quiet:
                    out += f
        if speech_ms < REPLAY_MIN_SPEECH_MS:
            return b""
        cap = int(REPLAY_MAX_S * self.vad.rate) * 2
        return bytes(out[-cap:])

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
                "voice_error", where="finalize_turn", message=redact_key(str(exc))
            )
            self._encounter_event_soon(
                "voice_error", detail=f"finalize_turn: {exc}", severity="error"
            )

    async def _finalize_turn(self, agent_id: str, agent, rt,
                             buf: List[str], direction: Optional[dict],
                             *, interrupted: bool = False, stop=None,
                             settled=None, audio_bytes: int = 0,
                             audio_unterminated: bool = False,
                             retried: bool = False) -> None:
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
            text, retry_head = self._retry_head_if_empty(agent_id, text, retried)
            # See _instructions: the note is spent once the actor has spoken
            # under it, and the _steer() re-brief in this method's finally is
            # the first brief that should go out without it. The framing note
            # (_fold_opening) is a first-reply note and is spent the same way.
            self._scene_note = ""
            if self._opening_agent in (None, agent_id):
                self._opening_note = ""
                self._opening_agent = None
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
                # Same gap as the room's finalizer: the transcript broadcast is
                # in the `if text` arm, so a silent turn reached the console as
                # nothing whatsoever. Silence has to be legible as a fault.
                await self._encounter_event(
                    "transcript_missing", agent_id=agent_id,
                    detail="the character spoke and no transcript arrived",
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
                # The 1:1 half of the same gap; see _finalize_member_inner.
                segment=self.segment,
                interaction=self._interaction_id(),
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
            delivered_ms = turn_audio.audio_ms(audio_bytes)
            self.session.store.event(
                "assistant_turn", agent_id=agent_id, text=text,
                latency_s=latency, segment=self.segment, transcript_missing=missing,
                interrupted=interrupted, retry_head=retry_head,
                # How much of this line the participant was actually SENT. See
                # server/voice/turn_audio.py: without it, a turn delivered as
                # half a sentence is indistinguishable in every channel this
                # study records from one delivered whole.
                audio_ms=delivered_ms,
            )
            if not interrupted:
                self._note_audio_shortfall(agent_id, text, delivered_ms,
                                           audio_unterminated)
            if retried:
                self._note_retry_outcome(agent_id, text, delivered_ms,
                                         interrupted, audio_unterminated)
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

    def _take_turn_audio(self) -> int:
        """Detach the byte count of the audio this closing 1:1 turn delivered.

        Taken at the same instant as the buffer and for the same reason: the
        next reply's first chunk can arrive while this turn is still settling,
        and a counter read at the end of the grace would be that reply's.
        Separate from _take_turn_buffer only because that method's three-value
        return is a shape the tests already hold.
        """
        n, self._turn_audio_bytes = self._turn_audio_bytes, 0
        return n

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
        self._turn_audio_bytes = 0
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
            instructions = self._instructions() + self._director_note(direction)
            try:
                acked = await self._deliver_brief(self.rt, instructions)
            except Exception as exc:  # noqa: BLE001
                self.session.store.event(
                    "trigger_brief_failed",
                    trigger_id=trigger["id"],
                    interaction=self._interaction_id(),
                    segment=self.segment,
                    index=self._trigger_idx,
                    agent_id=self.agent_id,
                    probing=probing,
                    message=redact_key(str(exc)),
                )
                await self._encounter_event(
                    "trigger_brief_failed", agent_id=self.agent_id,
                    detail=f"beat {trigger['id']} was never briefed: {exc}",
                    severity="error",
                )
                return
            self._fire_trigger(trigger, probing=probing)
            self._pending_direction = {
                "acked": acked,
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
            instructions = self._instructions() + self._director_note(direction)
            try:
                acked = await self._deliver_brief(rt, instructions)
            except Exception as exc:  # noqa: BLE001
                self.session.store.event(
                    "trigger_brief_failed",
                    trigger_id=trigger["id"],
                    interaction=self._interaction_id(),
                    segment=self.segment,
                    index=self._trigger_idx,
                    agent_id=agent_id,
                    probing=probing,
                    message=redact_key(str(exc)),
                )
                await self._encounter_event(
                    "trigger_brief_failed", agent_id=agent_id,
                    detail=f"beat {trigger['id']} was never briefed: {exc}",
                    severity="error",
                )
                return
            self._fire_trigger(trigger, probing=probing)
            self._pending_direction = {
                "acked": acked,
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

    async def _direct_member(self, agent_id: str,
                             intent: str) -> Optional[bool]:
        """Carry the director's per-turn direction to one room member.

        THE GAP THIS FILLS. The director composes a one-line intent per
        speaker, every turn, for exactly this. In a room nothing carried it:
        _run_group_turn read `agent_id` out of each routed entry and dropped
        the rest, and `_speak_as` — the one function in this file that ever
        appended an intent to a brief — has no callers, which is why
        server/director.py's own docstring says the direction "existed in
        exactly one place, a local variable, for the length of one turn". The
        beat and persona re-briefs (_brief_member, rebrief) were already going
        out over the route that works; the per-turn intent was not going out at
        all. So this is deliberately not a new mechanism: it is the same
        session.update, through the same _deliver_brief, with the same
        three-valued honesty about whether it arrived.

        WHAT IT DOES ON THE CONFIGURED MODEL: nothing on the wire. `intent` is
        asked of the ROOM first, because the room is what knows which model its
        sessions were actually opened on, and on a family whose row says a
        mid-session session.update is not honoured (nto.gemini-live-2.5-flash:
        three frames, zero acks, an actor that went on ignoring the direction)
        this sends no frame at all. That is the close_participant_turn pattern
        and it is here for the same reason — an encounter on the configured
        model must cost exactly what it cost before this change, and the one
        thing worse than a direction that does not arrive is a session.update
        landing on a member mid-reply, which on that family is how a character
        goes silent for the rest of the encounter.

        Not sending is not the same as not reporting, and the difference is the
        whole value of the row. The direction is written to the record either
        way, carrying `delivered` — whether a frame was even attempted — beside
        the `acked` every brief already carries, and a family that cannot carry
        one goes through _report_unacked_steering, so the researcher's strip
        says this encounter is unsteered while it is still running. A silent
        skip would have made the configured model look identical to a model
        that delivers.

        Returns the ack (True/False/None) the way _deliver_brief does, or False
        where the family cannot carry a direction at all.
        """
        room = self.room
        if room is None or not intent:
            return None
        rt = room.session_for(agent_id)
        agent = next(
            (a for a in self._resolve_agents() if a.id == agent_id), None
        )
        if rt is None or agent is None:
            return None
        # Built in the member's own context (_instructions_for swaps the
        # runner's current character for the length of the call), so the
        # direction rides on that character's persona and not on whoever the
        # runner happens to be pointing at.
        instructions = self._instructions_for(agent) + self._director_note(intent)
        row = {
            "turn": self._turn_index,
            "segment": self.segment,
            "interaction": self._interaction_id(),
            "agent_id": agent_id,
            "agent_name": agent.name,
            "voice": getattr(rt, "voice", None),
            "stage_direction": intent,
            # No planted beat was spent for this: a per-turn intent is the
            # director's own reading of the turn, and an ESCI-scored beat is
            # not. Written as null rather than omitted so the two are
            # distinguishable in the record instead of merely different lengths.
            "trigger_id": None,
            "esci": [],
            "probing": False,
            "source": "director_intent",
            "instructions_sha256": hashlib.sha256(
                instructions.encode()).hexdigest()[:16],
            "director_model": (self.director.model if getattr(self, "director", None)
                               else provenance()["text_model"]),
        }
        if not room.steering_is_real:
            self.session.store.event(
                "stage_direction", acked=False, delivered=False, **row
            )
            await self._report_unacked_steering(
                agent_id=agent_id, model=room.model,
            )
            return False
        try:
            acked = await self._deliver_brief(rt, instructions)
        except Exception as exc:  # noqa: BLE001 - a dead member must not kill the turn
            # The same shape _brief_member uses for a brief that raised: the
            # direction did not land, and the turn goes on without it rather
            # than the room losing the speaker.
            self.session.store.event(
                "director_intent_failed", agent_id=agent_id,
                segment=self.segment, interaction=self._interaction_id(),
                intent=intent, message=redact_key(str(exc)),
            )
            return None
        # Paired with the turn this produces, exactly as a beat's direction is:
        # _finalize_member reads _pending_direction and writes the steering
        # pair, which is how a rater sees the line next to what was asked for.
        self._pending_direction = dict(row, acked=acked, delivered=True)
        self.session.store.event("stage_direction", **self._pending_direction)
        return acked

    def _drop_undelivered_intent(self, agent_id: str, reason: str) -> None:
        """Retract a per-turn direction whose speaker never got the floor.

        The same discipline the planted beats already have. A direction left
        pending is paired by _finalize_member with whichever turn finalises
        next, so a grant that failed after the brief went out would attach this
        turn's note to somebody else's line — and a rater comparing a direction
        to the reply it produced would be reading a pairing that never
        happened. Only ever drops a direction this method's own path put there:
        `source` is what tells a per-turn intent from a beat, and a beat has
        its own retraction beside its trigger_undelivered row.
        """
        pending = self._pending_direction or {}
        if pending.get("source") != "director_intent":
            return
        self._pending_direction = None
        self.session.store.event(
            "director_intent_undelivered", agent_id=agent_id,
            segment=self.segment, interaction=self._interaction_id(),
            intent=pending.get("stage_direction"), reason=reason,
        )

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
                    # A scored beat the participant never faced. Coverage is
                    # what decides whether this encounter is usable, so the
                    # researcher hears about it now rather than at analysis.
                    await self._encounter_event(
                        "trigger_undelivered", agent_id=speaker,
                        detail=f"probe beat {self._fired[-1] if self._fired else '?'} "
                               "was briefed and never spoken (floor_grant_failed)",
                        severity="error",
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
        # Through the same resolver as every other voice on this path, so a
        # character does not change voice between the room and this sequencer
        # and so this is not a third place an ElevenLabs id could get in.
        self.rt.voice = self._voice_of(agent)
        instructions = self._instructions()
        if intent:
            instructions += self._director_note(intent)
        acked = await self._deliver_brief(self.rt, instructions)

        # The steering log is part of the study record: what the director told
        # this actor, verbatim, before it spoke. Logged even when there is no
        # direction, so an unsteered turn is distinguishable from a lost one.
        self._pending_direction = {
            "acked": acked,
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
            # This turn has the transcript it will route on (or has given up
            # waiting): a later turn_ended is a new turn.
            self._group_turn_waiting = False

            # SCRIBE WATCHDOG (origin/main 169310c). The third way a scribe
            # dies, after the two GroupRoom already knows about: the socket is
            # fine, _went_deaf sees nothing, and it has simply stopped emitting
            # transcripts — seen after about six turns on native-audio. Two
            # consecutive participant turns with no transcript from anywhere is
            # the symptom, and reopen_scribe is the repair, the same repair
            # _went_deaf reaches for. Without a participant channel the
            # encounter records a perfect agent transcript and not one word the
            # participant said, which is the exact failure the scribe-commit
            # round existed to close.
            if fresh:
                self._turns_without_transcript = 0
            else:
                self._turns_without_transcript += 1
                if (self._turns_without_transcript >= 2
                        and self.room is not None
                        and self.room.scribe is not None):
                    self._turns_without_transcript = 0
                    try:
                        new_scribe = await self.room.reopen_scribe()
                        if self._scribe_pump is not None:
                            self._scribe_pump.cancel()
                        self._scribe_pump = asyncio.ensure_future(
                            self._pump_scribe(new_scribe))
                        self._pumps.append(self._scribe_pump)
                        self.session.store.event("scribe_reconnected")
                    except Exception as exc:  # noqa: BLE001
                        self.session.store.event(
                            "scribe_reconnect_failed",
                            message=redact_key(str(exc)))

            named_early = self._named_in(fresh)
            first = named_early
            # The director may return an ORDERED multi-speaker sequence (e.g.
            # [A, B, A]); capture it so the follow-up beats play it in order
            # instead of being re-decided by a second route() call. None when the
            # participant addressed someone directly (no director call was made).
            routed_seq = None
            # The per-speaker direction that came back with each of those ids,
            # positionally aligned with routed_seq. Held rather than dropped:
            # delivering it is the whole of _direct_member below.
            routed_intents: List[Optional[str]] = []
            # Set when the director produced no decision for this turn — the
            # gateway 401'd, timed out, or answered without a tool block — and
            # Director._fallback handed back cast[0] instead. That is not a
            # routing decision and must not be written as one: cast[0] is the
            # dominant character in every group spec, so a flaky gateway
            # recorded as judgement shows up in the analysis as inflated
            # dominance rather than as an outage, and group scenarios are half
            # the study. The entry carries its own reason and detail; keep them.
            route_fallback: Optional[dict] = None
            if first is None:
                try:
                    routed = await self.director.route(
                        self.session.shared_history, fresh
                    )
                    route_fallback = next(
                        (r for r in routed if r.get("fallback")), None
                    )
                    # The direction travels WITH the speaker from here on, as a
                    # pair. It used to be dropped on this very line — `routed`
                    # entries are {agent_id, intent?} and this read agent_id and
                    # nothing else, which is why server/director.py's own
                    # docstring says the intent "existed in exactly one place, a
                    # local variable, for the length of one turn". A dict keyed
                    # by agent id would not do: the director returns ORDERED
                    # sequences, and [A, B, A] is two different directions for A.
                    candidates = [
                        (r.get("agent_id"), r.get("intent"))
                        for r in routed if r.get("agent_id") in room.sessions
                    ]
                    # The director only dedupes CONSECUTIVE speakers against its
                    # static cast, but room.sessions can shrink mid-encounter (a
                    # member's gateway session dropped), so filtering to live
                    # members can make previously non-adjacent duplicates adjacent
                    # (e.g. [A, dead, A] -> [A, A]). Collapse consecutive repeats
                    # so one agent is never handed the floor twice in a row, while
                    # a genuine [A, B, A] rebuttal (separated by B) is preserved.
                    collapsed = []
                    collapsed_intents = []
                    for c, c_intent in candidates:
                        if not collapsed or collapsed[-1] != c:
                            collapsed.append(c)
                            collapsed_intents.append(c_intent)
                    candidates = collapsed
                    routed_seq = candidates
                    routed_intents = collapsed_intents
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
                    # route() contains its own gateway failures and degrades to
                    # _fallback, so reaching here means the call itself broke.
                    # Either way no decision was made, which is what the
                    # director_route line below has to say.
                    self.session.store.event(
                        "director_error", message=redact_key(str(exc))
                    )
                    route_fallback = {
                        "reason": "director_route_raised",
                        # Scrubbed where the gateway's words are captured, not
                        # only where they are written. This dict outlives the
                        # handler and is read again 200 lines below, so holding
                        # a raw 401 body in it leaves the record one new sink
                        # away from carrying a live credential.
                        "detail": redact_key(str(exc)),
                    }
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

            # The direction the director wrote for whoever is actually about to
            # speak, and only theirs. Looked up by identity rather than by
            # position, because `first` is not always routed_seq[0]: the
            # anti-dominance filter above skips a candidate who just spoke, and
            # the availability check can hand the turn to someone else again. A
            # reassigned speaker who was ALSO routed keeps their own direction
            # and never inherits the missing character's — the same rule the
            # planted beats have had since _trigger_agent, for the same reason:
            # a note written for Dan, performed by Priya, is a direction the
            # instrument never actually staged, recorded as though it had been.
            first_intent = None
            if routed_seq and first in routed_seq:
                first_intent = routed_intents[routed_seq.index(first)]

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
                # Deferring is correct, but a beat that keeps deferring because
                # its character never gets the floor is an interaction quietly
                # running out of scored moments, and only the console can catch
                # that while there is still time to advance the encounter.
                await self._encounter_event(
                    "trigger_deferred", agent_id=owner,
                    detail=f"beat {pending['id']} belongs to {owner}; "
                           f"this turn routed to {first}",
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

            # The director's own direction for this turn, delivered to the one
            # character it was written for. Nothing carried it before: this
            # method read agent_id out of each routed entry and dropped the
            # rest, and _speak_as — the only function that ever delivered an
            # intent — has no callers, so in a room the direction was composed,
            # validated, recorded and never spoken to anybody.
            #
            # Second to the planted beat, never alongside it. A beat is the
            # scored independent variable and it arrives by this same route, so
            # a turn that has just briefed one is a turn whose actor has already
            # been told what to do; appending a second DIRECTOR NOTE would put
            # two directions in one brief and leave a rater unable to say which
            # one the reply answered, and the second session.update would also
            # replace the first (a brief is whole-session, not a patch), so the
            # beat would be the one lost. `_trigger_idx` moving is _brief_member
            # spending a beat, and it is the only reliable sign of it — the
            # deferral branch above leaves it where it was.
            if first_intent and self._trigger_idx == spent_idx:
                await self._direct_member(first, first_intent)
            elif first_intent:
                self.session.store.event(
                    "director_intent_yielded", agent_id=first,
                    segment=self.segment, interaction=self._interaction_id(),
                    intent=first_intent, reason="planted_beat_briefed",
                )

            self._response_done.clear()
            # _grant (origin/main) first: it plays a reply this member has
            # ALREADY made rather than asking for a second one, and falls
            # through to room.give_floor when there is nothing to adopt.
            # Every failure path below is ours and is unchanged: _grant
            # returns give_floor's own None when the grant fails.
            granted = await self._grant(first)
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
                    await self._encounter_event(
                        "trigger_undelivered", agent_id=first,
                        detail=f"beat {self._fired[-1] if self._fired else '?'} "
                               "was briefed and never spoken (floor_grant_failed)",
                        severity="error",
                    )
                    self._trigger_idx = spent_idx
                    del self._fired[spent_fired:]
                    # The direction was never spoken, so it must not be left
                    # pending and paired with whichever turn finalises next.
                    self._pending_direction = None
                self._drop_undelivered_intent(first, "floor_grant_failed")
                return
            try:
                await asyncio.wait_for(self._response_done.wait(), timeout=45)
                if self.room is not None and self.room.speaking == first:
                    self.room.speaking = None
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
            # One direction per follow-up, positionally aligned with it, so a
            # speaker taken out of the sequence takes their own note with them
            # and never somebody else's. Every branch below fills both lists or
            # neither.
            followup_intents: List[Optional[str]] = []
            if named and named != first:
                # A name spoken THIS turn is a direct address and overrides the
                # director's planned sequence — and with it the direction the
                # director wrote for the speaker it displaced. Nothing was ever
                # written for this one, so they take the floor undirected rather
                # than inheriting a note meant for another character.
                followups.append(named)
                followup_intents.append(None)
            elif routed_seq is not None:
                # Honour the director's ORDERED sequence from the first route()
                # call (e.g. [A, B, A]) instead of discarding it and re-deciding
                # with a second LLM call: play the speakers that followed `first`
                # in the returned order, so an authored back-and-forth (g5's
                # [claire, arjun, claire]) actually plays. Bounded by the
                # director's own max sequence length.
                idx = routed_seq.index(first) if first in routed_seq else -1
                followups = routed_seq[idx + 1:][: DIRECTOR_MAX_SPEAKERS - 1]
                # Sliced identically, off the list built beside routed_seq, so
                # the pairing survives an [A, B, A] sequence where the same
                # character is routed twice with two different directions.
                followup_intents = routed_intents[idx + 1:][
                    : DIRECTOR_MAX_SPEAKERS - 1]
            else:
                # Direct-address opener: no director sequence was produced, so ask
                # for a single follow-up to keep the room responsive.
                try:
                    routed = await self.director.route(
                        self.session.shared_history, fresh
                    )
                except Exception as exc:  # noqa: BLE001, never break the room
                    self.session.store.event(
                        "director_error", message=redact_key(str(exc))
                    )
                    routed = []
                    route_fallback = route_fallback or {
                        "reason": "director_route_raised",
                        "detail": redact_key(str(exc)),
                    }
                else:
                    # `or`, not a plain assignment. This branch is also reached
                    # after the FIRST route() call raised and left `first` to
                    # the rotation above, and a second call that happens to
                    # succeed does not retroactively make that rotated speaker
                    # a director decision. Overwriting here would clear the
                    # flag and write exactly the false record this carries.
                    route_fallback = route_fallback or next(
                        (r for r in routed if r.get("fallback")), None
                    )
                served = [
                    (r.get("agent_id"), r.get("intent")) for r in routed
                    if r.get("agent_id") in room.sessions
                    and r.get("agent_id") != first
                ][:1]
                followups = [aid for aid, _ in served]
                followup_intents = [intent for _, intent in served]
            if self._closed or self.room is not room:
                return

            # `fallback` is the difference between "the director chose these
            # speakers" and "the director was unreachable and the room played
            # cast[0] anyway". Both used to be written as the former, so an
            # encounter whose director was dead throughout read in the record
            # exactly like a steered one, and the truth was only recoverable by
            # joining director_error on timestamp — a join nothing in this repo
            # performs. Director._fallback already tags its entry for exactly
            # this; carry the tag rather than dropping it.
            self.session.store.event(
                "director_route", speakers=[first] + followups, addressed=named,
                fallback=route_fallback is not None,
                fallback_reason=(route_fallback or {}).get("reason"),
                fallback_detail=redact_key(
                    str((route_fallback or {}).get("detail") or "")
                ) or None,
            )
            if route_fallback is not None:
                await self._encounter_event(
                    "director_fallback", agent_id=first,
                    detail=f"{route_fallback.get('reason')}: "
                           f"{route_fallback.get('detail')}; "
                           f"the room played {first} without a routing decision",
                    severity="error",
                )
            for aid, intent in zip(followups, followup_intents):
                if self._closed or self.room is not room or self.vad.speaking:
                    if self.vad.speaking:
                        self.session.store.event(
                            "followup_yielded", agent_id=aid,
                        )
                    break
                # Directed immediately before the grant and never mid-reply:
                # the previous speaker's response_done is what this loop has
                # just waited for, and a follow-up on a family where the floor
                # is real has produced nothing of its own in the meantime. That
                # is the same ordering _brief_member and give_floor already
                # rely on, and the room refuses to carry a direction at all on
                # the family where a session.update can land mid-response and
                # mute a member (GroupRoom.steering_is_real).
                if intent:
                    await self._direct_member(aid, intent)
                    if self._closed or self.room is not room:
                        return
                self._response_done.clear()
                if await self._grant(aid) is None:
                    self._drop_undelivered_intent(aid, "floor_grant_failed")
                try:
                    await asyncio.wait_for(self._response_done.wait(), timeout=45)
                    if self.room is not None and self.room.speaking == aid:
                        self.room.speaking = None
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
                "voice_error", where="group_maybe_advance",
                message=redact_key(str(exc)),
            )
            await self._encounter_event(
                "voice_error", detail=f"group_maybe_advance: {exc}",
                severity="error",
            )

    def _named_in(self, text: str) -> Optional[str]:
        """The character the participant addressed by name, if any.

        Match whole tokens, not substrings: a raw `in` test made 'Dan' fire on
        'abundant' and, because direct address overrides the director, seize the
        floor from whoever should have spoken."""
        if not text:
            return None
        # Whole tokens (ours) with last-position-wins (origin/main). Either
        # alone is wrong: a raw substring test made 'Dan' fire on 'abundant',
        # and first-match routed "Sorry Alex, but Jordan, how are you?" to Alex
        # when people address the target LAST.
        tokens = _norm_speech(text).split()
        best_at, best_id = -1, None
        for a in self._resolve_agents():
            name_tokens = _norm_speech(a.name).split()
            if not name_tokens:
                continue
            n = len(name_tokens)
            for i in range(len(tokens) - n + 1):
                if tokens[i:i + n] == name_tokens and i > best_at:
                    best_at, best_id = i, a.id
        return best_id

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
                # The steering panel shows the shift as made; only this says it
                # has not reached the actor yet. A researcher who has just moved
                # a gear and is watching for the effect otherwise reads the next
                # unchanged turn as the shift having no effect.
                await self._encounter_event(
                    "steer_deferred", agent_id=self.agent_id,
                    detail="a reply was in flight, so this turn's gear shifts "
                           "were not delivered; the next brief carries them",
                )
                return
            instructions = self._instructions()
            if self._pending_direction:
                # A direction that has not been performed yet must survive its
                # own steering pass, or the beat is briefed and then silently
                # un-briefed before the actor ever speaks it.
                instructions += self._director_note(
                    str(self._pending_direction.get("stage_direction") or "")
                )
            acked = await self._deliver_brief(self.rt, instructions)
            # The other half of steer_deferred. Those shifts went out as
            # delivered=None because nothing could know yet; this says they
            # were issued rather than held back, so the two outcomes of a 1:1
            # review are both stated and an analyst never has to read delivery
            # out of an absence.
            #
            # Issued, not received. This event used to be the whole answer, and
            # it was written the instant the send returned — so on a model that
            # acknowledges no mid-session update, which is the model the study
            # is configured for, it asserted a delivery the platform never
            # confirmed. `acked` is what separates the two: True the gateway
            # said so, False it declined to, None this bridge cannot tell. See
            # _deliver_brief.
            self.session.store.event(
                "steer_delivered", agent_id=self.agent_id,
                segment=self.segment, acked=acked,
                shifts=len(self.session.steering_log) - before,
            )

    # ── the researcher's live failure channel ──────────────────────────────
    def _event_t(self) -> Optional[float]:
        """Seconds into the encounter, on the clock events.jsonl already uses.

        The console lines a researcher reads mid-encounter and the rows an
        analyst reads afterwards have to be joinable, and the only shared
        timebase is the store's own started_at. None when there is no store
        clock to read rather than a second, unrelated origin."""
        started = getattr(self.session.store, "started_at", None)
        if not started:
            return None
        return round(time.time() - started, 3)

    async def _encounter_event(self, kind: str, *, agent_id: Optional[str] = None,
                               detail: Optional[str] = None,
                               severity: str = "warn") -> None:
        """Put one mid-encounter failure in front of the watching researcher.

        Until this existed the live channel carried state, transcript and
        steering only, so an encounter whose director was dead, whose steering
        errored every turn and whose planted beats were briefed and never spoken
        looked exactly like a healthy one for the full 7-12 minutes — the one
        window in which a researcher can still intervene in something
        unrepeatable. Every failure was written to events.jsonl and broadcast to
        nobody.

        `kind` is the events.jsonl event type wherever the failure has one, so
        the console and the record name the same thing. `detail` is
        gateway-derived and therefore goes out redacted: this frame leaves the
        process for a browser exactly as the participant's error frames do.
        """
        try:
            await self.session.broadcast({
                "type": "encounter_event",
                "kind": kind,
                "t": self._event_t(),
                "agent_id": agent_id,
                "detail": redact_key(detail) if detail else None,
                "severity": severity,
            })
        except Exception:  # noqa: BLE001
            # Reporting a fault must never become one. broadcast() already drops
            # sockets that have gone away; anything that gets past it belongs to
            # the console, and these calls sit inside the runner's error paths,
            # where raising would cost the encounter the failure was about.
            pass

    def _note_audio_shortfall(self, agent_id: str, text: str,
                              delivered_ms: int,
                              unterminated: bool = False) -> None:
        """Say so when a turn's audio was too short for its own words.

        This is the instrument that was missing, and its absence is why the
        clearest failure in this system was reported by a person wearing
        headphones rather than by anything the platform writes down. A reply
        whose audio stream the gateway abandons mid-cadence — measured at 21 of
        191 live replies on nto.gemini-live-2.5-flash through this gateway, and
        4 of 11 in the worst single encounter — arrives with a COMPLETE
        transcript and 0.4-0.9 s of voice for 7 to 13 words. Every channel this
        study records says that turn was fine: the text is whole, the WAV holds
        exactly what was sent, the event trail has no error in it. The only
        thing that can see the fault is this comparison, between a turn's own
        text and its own delivered audio, and nothing was making it.

        Recorded per turn, because the shape matters more than any single
        instance: one short turn is a glitch, and a third of an encounter's
        turns arriving half-spoken is an encounter that should not be paid for
        or rated. server/voice/turn_audio.scan_events reads exactly these turns
        back out of events.jsonl for whoever is deciding that.

        It is NOT sent to the researcher's live console, and that is a gap
        rather than a decision: the console's strip has a two-way contract with
        static/researcher.html's ENC_EVENT_LABEL map (every kind the server can
        emit needs a label, and every label needs an emitter — both halves are
        enforced by tests/test_final_console.py), and that map was outside the
        change that added this. Adding `agent_audio_short: 'this line was only
        half spoken'` there and an _encounter_event_soon call here is all it
        takes, and it is worth doing: a researcher watching an encounter fill up
        with these is watching an encounter not worth paying for.

        Not raised for interrupted turns (the caller filters those): a
        participant talking over a character is the behaviour two of the
        scenarios exist to score.
        """
        bad = turn_audio.shortfall(text, delivered_ms)
        if not bad:
            return
        self.session.store.event(
            "agent_audio_short", agent_id=agent_id, segment=self.segment,
            audio_ms=bad["audio_ms"], expected_ms=bad["expected_ms"],
            words=bad["words"], delivered_fraction=bad["delivered_fraction"],
            text=text,
            # True: the gateway stopped sending this reply's audio without ever
            # closing the stream, which is a fault upstream of this process and
            # cannot be repaired here. False: everything the gateway sent was
            # relayed, and the shortfall is ours to explain.
            gateway_abandoned_audio=unterminated,
        )

    async def _retry_reply(self, rt, agent_id: str, ev: dict, *,
                           has_floor: bool = True) -> bool:
        """Ask the gateway for a lost reply again, once, or write down why not.

        `ev` is the bridge's response_done carrying `retry_reason`: "truncated"
        (the audio stream stopped short of the reply's own transcript and was
        never closed) or "absent" (the words came and no voice followed for
        AUDIO_ABSENT_S). See server/voice/realtime.py for both detectors and
        for the recipe the retry sends. This method owns the three refusals the
        bridge cannot make, because their facts live here:

          * the floor. In a room a retry is a fresh response.create, and it
            goes only to the character who holds `room.speaking` at this
            instant; if the director moved the floor while the reply was dying,
            the retry is dropped rather than spoken over whoever has it now.
          * the participant. If they are talking when the verdict lands, a
            retry would commit whatever they have said so far as a turn and
            answer it; the reply is finalised as cut off instead, which is what
            their talking over it means anyway.
          * the budget. `retryable` is the bridge's per-turn counter; a second
            failure on the same turn is finalised as truncated by the existing
            detection and the encounter moves on.

        A reply the participant barged in on never arrives here with a reason at
        all — the bridge withholds it for a reply it was told to cancel — so a
        line they chose to cut off is never re-spoken at them.

        Every outcome is recorded: `audio_retry` when the gateway was asked
        again, `audio_retry_suppressed` with `why` when it was not. Both carry
        the abandoned reply's own numbers and text, so a wave can be checked for
        how often this happened and what was lost (tools/encounter_health.py).
        The page is told with `assistant_retry`, which replaces the broken
        reply's audio and caption rather than appending to them — deliberately
        not `assistant_interrupted`, whose handler freezes the caption at what
        was shown, which is the wrong record of a line about to be re-spoken.
        """
        reason = ev.get("retry_reason")
        if not reason:
            return False
        fields = dict(agent_id=agent_id, segment=self.segment, reason=reason,
                      words=ev.get("words"), audio_ms=ev.get("audio_ms"),
                      text=ev.get("text"))
        if ev.get("waited_s") is not None:
            fields["waited_s"] = ev.get("waited_s")
        why = None
        if not ev.get("retryable"):
            # The bridge's own refusal, and it says why: the turn's one retry
            # is spent, the reply is a runaway, or the verdict was overtaken
            # while it was held (the participant was heard, the gateway
            # resumed by itself, a new turn or a barge-in). See
            # voice/realtime.py AUDIO_RETRY_QUIET_S.
            why = ev.get("not_retryable_why") or "already_retried"
        elif self._closed:
            why = "closing"
        elif not has_floor:
            why = "floor_moved"
        elif self.vad.active_within():
            # `speaking` alone has holes a retry fell through live: it comes
            # up 250 ms after a voice starts and drops for 0.3-0.5 s inside an
            # utterance. The hint (any voice-like frame in the last second)
            # is the guard; see SilenceDetector.active_within.
            why = "participant_speaking"
        elif not await self._ask_again(rt, self._retry_nudge_for(ev.get("text"))):
            why = "bridge_refused"
        if why is not None:
            self.session.store.event("audio_retry_suppressed", why=why, **fields)
            return False
        # The prompt the model was given in place of the participant's voice,
        # on the record: the line that follows answers it, not the participant.
        self.session.store.event(
            "audio_retry", nudge=getattr(_realtime, "AUDIO_RETRY_NUDGE", None),
            **fields)
        # Kept until the retried turn closes, so the outcome can say whether
        # what came back was THIS line again or merely a line.
        self._retry_lost[agent_id] = {"text": ev.get("text") or "",
                                      "words": ev.get("words") or 0,
                                      "reason": reason, "at": time.time()}
        await self._send({"type": "assistant_retry", "agent_id": agent_id})
        return True

    @staticmethod
    async def _ask_again(rt, nudge: Optional[str]) -> bool:
        """rt.retry_response with the quoted-line nudge where the bridge takes
        one; the bare call where it does not (a bridge built before the nudge
        was a parameter, or a test's fake of one)."""
        default = getattr(_realtime, "AUDIO_RETRY_NUDGE", None)
        try:
            takes_nudge = "nudge" in inspect.signature(rt.retry_response).parameters
        except (TypeError, ValueError):
            takes_nudge = False
        if nudge and nudge != default and takes_nudge:
            return bool(await rt.retry_response(nudge=nudge))
        return bool(await rt.retry_response())

    @staticmethod
    def _retry_nudge_for(lost_text) -> Optional[str]:
        """The prompt an audio-absent retry puts in front of the model.

        The bare nudge asks for "it" again, and live the model answered with
        a different, shorter line: Drew's "I sent it because it needs to be
        said. It wasn't getting fixed." came back as "It's been an issue
        twice." (2026-09-14, S1B, 5 words for 13, at 9 s). The words the
        gateway lost the voice of are known — they are on the audio_retry
        event — so the nudge quotes them: the retry then re-speaks the line
        the record already holds instead of replacing it. Left bare for a
        line too long to quote (a runaway is never re-asked anyway)."""
        base = getattr(_realtime, "AUDIO_RETRY_NUDGE", None)
        text = " ".join(str(lost_text or "").split())
        if not base or not text or len(text.split()) > 60:
            return base
        return f"{base} You were saying: \"{text}\""

    async def _reply_missing(self, rt, agent_id: str, ev: dict, *,
                             has_floor: bool = True) -> bool:
        """A reply that was asked for and never began. Ask once more, with a
        prompt, or write down why not.

        The bridge's `reply_missing` (see REQUEST_UNANSWERED_S) arrives after
        ten seconds of nothing behind a commit + response.create. The three
        refusals are _retry_reply's, for the same reasons: the floor may have
        moved in a room, the participant may be talking (in which case they
        are about to prompt the reply themselves), and the turn's one retry
        may be spent. The retry is a user TEXT item (UNANSWERED_NUDGE) and a
        response.create, the one thing measured to draw a reply out of this
        gateway after it has ignored a create; the record carries the prompt
        so a rater can see that the line which follows answers it.

        Returns True when the gateway was asked again.
        """
        fields = dict(agent_id=agent_id, segment=self.segment,
                      interaction=self._interaction_id(),
                      waited_s=ev.get("waited_s"))
        self.session.store.event("reply_missing", **fields)
        why = None
        if not ev.get("retryable"):
            why = "already_retried"
        elif self._closed:
            why = "closing"
        elif not has_floor:
            why = "floor_moved"
        elif self.vad.active_within():
            why = "participant_speaking"
        how = "nudge"
        nudge = getattr(_realtime, "UNANSWERED_NUDGE", None)
        if why is None:
            # The participant's own line first: it is what the gateway failed
            # to answer, and answered, it needs no explaining to a rater. The
            # text nudge stays behind it for a room (no per-member replay
            # buffer) and for a request that had no speech behind it — the
            # scene open, a probe.
            speech = self._replay_speech() if self.room is None else b""
            if speech and hasattr(rt, "replay_input") and \
                    await rt.replay_input(speech):
                how = "replay"
                nudge = None
                fields["replay_ms"] = len(speech) // 32
            elif not await rt.retry_response(nudge=nudge):
                why = "bridge_refused"
        self.session.store.event(
            "reply_retry", why=why, asked=why is None, how=how,
            nudge=nudge if why is None else None, **fields)
        # One console line, through the channel the console already labels.
        self._voice_error_soon(
            source="model", agent_id=agent_id, transient=True,
            gateway_text=(
                f"no reply from the gateway after {ev.get('waited_s')}s; "
                + ("asked again" if why is None else f"not asked again ({why})")),
        )
        return why is None

    async def _reconnect_after_gateway_close(self, rt) -> bool:
        """Rebuild a 1:1 session the GATEWAY closed under the encounter.

        _pump returning with the runner neither switching character nor
        closing means the socket ended on the other side — a gateway
        recycling a connection, a session hitting a limit, a stall the
        gateway gave up on. That used to end run(): the participant's socket
        closed behind it, the page put up "connection lost", and Reconnect
        started the part again with a stranger. Twice, and the card hides
        the retry ("couldn't retry"). The participant's socket is fine, so
        keep it: open a fresh session as the same character, with a scene
        note that says the line dropped and what has been said so far, and
        carry on in the same encounter. Bounded by RECONNECT_LIMIT per
        encounter; past it the old ending stands, with an `error` frame that
        the page is right to read as a stated failure.

        Returns True when a session is live again behind self.rt.
        """
        limit = int(getattr(_realtime, "RECONNECT_LIMIT", 0) or 0)
        for_replay, self._rebuild_for_replay = self._rebuild_for_replay, False
        if (self._closed or self.room is not None or rt is not self.rt
                or (getattr(rt, "_closing", False) and not for_replay)):
            return False
        if self._reconnects >= limit:
            self.session.store.event(
                "voice_error", where="reconnect", agent_id=self.agent_id,
                message=redact_key(
                    f"the gateway closed the session again; {limit} "
                    "reconnect(s) already spent, ending the encounter"),
            )
            await self._send({
                "type": "error",
                "message": "The connection to the conversation was lost.",
            })
            return False
        attempt = self._reconnects + 1
        self._transitioning = True
        try:
            try:
                await rt.close()
            except Exception:  # noqa: BLE001 - it is already gone
                pass
            self._scene_note = self._reconnect_note()
            new_rt = self._new_session(
                instructions=self._instructions(), voice=self._voice(),
            )
            new_rt.participant_speaking = lambda: bool(self.vad.active_within())
            try:
                await new_rt.connect()
            except Exception as exc:  # noqa: BLE001
                await new_rt.close()
                self.session.store.event(
                    "voice_error", where="reconnect", agent_id=self.agent_id,
                    attempt=attempt, message=redact_key(str(exc)),
                )
                await self._encounter_event(
                    "voice_error", agent_id=self.agent_id, severity="error",
                    detail=f"reconnect {attempt}: {exc}",
                )
                await self._send({
                    "type": "error",
                    "message": "The connection to the conversation was lost.",
                })
                return False
            self._reconnects += 1
            new_rt.reconnects = attempt
            self.rt = new_rt
            self.vad.reset()
            self._speaking = False
            self._agent_text = []
            self._settling_text = None
            self._settling_late_from = None
            self._settling_stop = None
            self._settling_settled = None
        finally:
            self._transitioning = False
        # The line the participant was saying when the gateway went — the
        # one the old page note told them to say again — goes into the new
        # session before anything else, so the character answers it. Nothing
        # is replayed when they said nothing since the gateway last heard
        # them, and the buffer is spent either way: a fresh socket is not
        # handed a line the old one already answered.
        speech = self._replay_speech()
        self._replay_pcm.clear()
        replayed_ms = 0
        if speech and hasattr(new_rt, "replay_input"):
            try:
                if await new_rt.replay_input(speech):
                    replayed_ms = len(speech) // 32
            except Exception as exc:  # noqa: BLE001 - the session stands
                self.session.store.event(
                    "voice_error", where="replay", agent_id=self.agent_id,
                    message=redact_key(str(exc)))
        self.session.store.event(
            "realtime_session_reconnected", agent_id=self.agent_id,
            agent_name=self.agent.name, attempt=attempt, segment=self.segment,
            interaction=self._interaction_id(), for_replay=for_replay,
            replayed_ms=replayed_ms,
        )
        if replayed_ms:
            self.session.store.event(
                "participant_turn_replayed", agent_id=self.agent_id,
                segment=self.segment, replay_ms=replayed_ms, attempt=attempt,
            )
        await self._encounter_event(
            "voice_error", agent_id=self.agent_id, severity="warn",
            detail=f"the gateway closed the session; reconnected as "
                   f"{self.agent.name} (attempt {attempt} of {limit})",
        )
        await self._send({"type": "voice_notice", "kind": "reconnected",
                          "agent_id": self.agent_id, "attempt": attempt,
                          "replayed": bool(replayed_ms)})
        return True

    def _reconnect_note(self) -> str:
        """The scene note a rebuilt session opens under: the same conversation,
        already in progress, with what has been said so far — a fresh socket
        has heard none of it. Spent after one reply, like every scene note."""
        note = (
            "\n\nSCENE: The line dropped for a moment and is back. This is the "
            "same conversation, already in progress: do not greet again, do "
            "not restart, do not mention the drop; carry on from where it was."
        )
        lines = []
        names = {a.id: a.name for a in self.cast}
        for entry in (self.session.shared_history or [])[-8:]:
            speaker = entry.get("speaker")
            if speaker == "user":
                who = "Them"
            elif speaker == self.agent_id:
                who = "You"
            else:
                # Another character's line, labelled as theirs. Every agent
                # line used to be "You", and a session rebuilt after a room
                # (S3C's Rafa one-on-one, live 2026-09-14) opened by saying
                # Bex's line back as its own.
                who = names.get(speaker, str(speaker))
            what = str(entry.get("text") or "").strip()
            if what:
                lines.append(f"- {who}: {what}")
        if lines:
            note += " What has been said so far, most recent last:\n" + "\n".join(lines)
        return note

    def _retry_head_if_empty(self, agent_id: str, text: str, retried: bool):
        """The text to record for a retried turn that closed with none.

        A barge-in on a reply being re-asked for finalises the buffer the
        retry emptied, so the turn was written as text="" and flagged
        transcript_missing - the flag that tells a rater "audio played, its
        text was lost" - although the participant had seen (and heard the
        first beat of) the lost line. Record that line instead, marked
        `retry_head` so it is not read as a delivery. Only for a turn with
        nothing of its own: a retried line that arrived, however short, is
        the turn."""
        if text or not retried:
            return text, False
        head = (self._retry_lost.get(agent_id) or {}).get("text") or ""
        return (head, True) if head else (text, False)

    def _note_retry_outcome(self, agent_id: str, text: str, delivered_ms: int,
                            interrupted: bool, unterminated: bool) -> None:
        """Say whether a retried turn brought the lost line back.

        Two questions, answered separately because they fail separately and a
        wave-level number that conflated them overstated the fix once already:

          * `delivered_whole`: the retry's audio stream was closed by the
            gateway, was not cut off, is a plausible delivery of its own words
            by the live detector's rule, and actually contains words and
            audio. An empty turn is NOT whole - turn_audio.shortfall answers
            None for no audio or under four words, and that None was being
            read as "fine".
          * `overlap`: the share of the lost line's words that the retried
            line contains. The retry is prompted by a user text item, and the
            model can answer the prompt instead of repeating itself ("I said
            understood"; or a fresh sentence). `recovered` requires
            RETRY_RECOVERED_OVERLAP of the lost words to be back; the number
            itself is on the event so an analyst can set their own bar.
        """
        lost = self._retry_lost.pop(agent_id, None) or {}
        lost_text = lost.get("text") or ""
        if text == lost_text and delivered_ms == 0:
            # The head put back by _retry_head_if_empty, not a delivery.
            text = ""
        words = turn_audio.word_count(text)
        short = turn_audio.shortfall(text, delivered_ms)
        delivered_whole = bool(words and delivered_ms > 0
                               and not (interrupted or unterminated or short))
        overlap = _word_overlap(lost_text, text)
        recovered = delivered_whole and (
            overlap is None or overlap >= RETRY_RECOVERED_OVERLAP)
        self.session.store.event(
            "audio_retry_outcome", agent_id=agent_id, segment=self.segment,
            recovered=recovered, delivered_whole=delivered_whole,
            overlap=overlap, lost_text=lost_text, lost_reason=lost.get("reason"),
            audio_ms=delivered_ms, words=words,
            interrupted=interrupted, gateway_abandoned_audio=unterminated,
            text=text,
        )

    def _encounter_event_soon(self, kind: str, *,
                              coalesce_key: Optional[str] = None,
                              **fields) -> None:
        """_encounter_event without awaiting the researcher's socket.

        Two kinds of caller, one reason each.

        Synchronous ones — a task done-callback — cannot await at all, and they
        are where a dead pump or a dead group turn is noticed, which is
        precisely what a researcher needs to see. Nothing is scheduled when no
        loop is running, since the frame would then have no socket to reach and
        ensure_future would raise into the callback and lose the store event
        beside it.

        The pumps use it for the other reason. Session.broadcast awaits
        ws.send_json once per attached researcher socket, so awaiting a console
        frame inside _pump's async-for — the loop that also relays agent_audio
        to the participant — puts a researcher's browser on the participant's
        audio path, where a console that has stopped reading its socket
        backpressures the conversation it is watching. A researcher's console
        must never be able to degrade a participant's encounter, so the frame
        leaves as its own task and the pump moves straight to the next event.

        `coalesce_key` names the stream a frame belongs to (one gateway, one
        pump); the burst is that key AND the frame's own message, so two
        different faults on one stream stay two frames. The first frame of a
        burst goes out immediately; identical repeats are counted and reported
        as a single frame when the burst ends
        — see _flush_console_repeats. Audio deltas arrive every few tens of
        milliseconds, so a corrupt stream produced one frame per chunk, and
        researcher.html re-renders the strip on every frame and shows a
        monotonic total: sixty discarded chunks read as sixty separate
        failures, and app.py's ENCOUNTER_EVENT_REPLAY_LIMIT bounds only the
        replay, never the live socket. A console a researcher learns to
        discount is worth less than no console. The per-chunk events.jsonl rows
        are deliberately untouched: the record keeps every occurrence, the
        console shows the shape.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if coalesce_key is not None:
            # The message is half the key. Keyed on the source alone, a second
            # DIFFERENT fault on the same stream was never shown at all and was
            # counted into a total published against the LAST message's text,
            # under the words "N repeats were kept off the console" — a
            # confident false statement about the data, which is the failure
            # this console exists to catch. Distinct texts are the normal case
            # here, not a corner: the only transient producer in the tree
            # (voice/realtime.py's corrupt-frame branch) quotes binascii, whose
            # message carries the failing chunk's own character count.
            coalesce_key = (coalesce_key, fields.get("detail"))
            burst = self._console_repeats.get(coalesce_key)
            if burst is not None:
                # Already reported for this burst. Count it — the total is what
                # the closing frame carries — and send nothing.
                burst["count"] += 1
                burst["fields"] = fields
                return
            self._console_repeats[coalesce_key] = {
                "kind": kind, "fields": fields, "count": 0,
            }
        self._spawn_console_frame(self._encounter_event(kind, **fields))

    def _spawn_console_frame(self, coro) -> None:
        """Dispatch one console frame as a tracked task.

        Retained in a set (and self-removing on completion) exactly as
        Session.spawn_auto_steer retains its steering tasks, and for the same
        reason: asyncio holds only a weak reference to a task nobody keeps, so
        a bare ensure_future can be collected before it has sent anything. On
        this path that loses a failure report and leaves the console asserting
        health by omission.
        """
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:
            # The loop is on its way out (this runs from pump finallys, which
            # is exactly where teardown lands). Reporting a fault must never
            # become one, and a raise here would come out of a `finally` and
            # REPLACE the exception the pump was already carrying — hiding the
            # real failure behind the report of a smaller one. The events.jsonl
            # row for every occurrence is already written either way.
            coro.close()
            return
        self._console_tasks.add(task)
        task.add_done_callback(self._console_tasks.discard)

    def _flush_console_repeats(self, coalesce_key: Optional[str] = None) -> None:
        """Close a burst out with one frame naming how many repeats it stood for.

        Called wherever a burst ends — a reply boundary, a pump ending, a
        terminal error arriving behind a run of survivable ones — because
        suppression that is never totalled is the bug class this file argues
        against everywhere else: a recoverable fault turned into a permanent
        silence. "discarded a corrupt audio frame (x60)" is more useful to a
        researcher than the first frame alone, and very much more useful than
        sixty frames; the point is to report the shape of the fault once, not
        to hide it.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Nothing to send it on. Keep the counts rather than dropping them,
            # so a later flush on a live loop still reports the burst.
            return
        # Callers name a stream ("model", "room:<id>", "scribe"); the bursts
        # under it are keyed by (stream, message), so one stream ending closes
        # every distinct fault it was carrying.
        keys = ([k for k in self._console_repeats if k[0] == coalesce_key]
                if coalesce_key is not None
                else list(self._console_repeats))
        for key in keys:
            burst = self._console_repeats.pop(key, None)
            if burst is None or burst["count"] < 1:
                # No repeats: the frame already sent said everything there was
                # to say, and a second frame reporting "x1" would be the same
                # inflation by a quieter route.
                continue
            fields = dict(burst["fields"])
            total = burst["count"] + 1
            detail = fields.get("detail") or burst["kind"]
            fields["detail"] = (
                f"{detail} (x{total}; {burst['count']} repeats were kept off "
                f"the console, events.jsonl has every one)"
            )
            self._spawn_console_frame(
                self._encounter_event(burst["kind"], **fields)
            )

    def _voice_error_soon(self, *, source: str, gateway_text: str,
                          transient: bool,
                          agent_id: Optional[str] = None) -> None:
        """The console half of a gateway `error` frame, for all three pumps.

        One helper rather than three copies because the three sit on the same
        audio path and had already drifted: the 1:1 pump throttled its
        PARTICIPANT notice to one per reply and still emitted a console frame
        per chunk, while the room and scribe pumps throttled neither — and a
        room multiplies that by every member in it.
        """
        if not transient:
            # Close any open burst first, so the strip reads in the order the
            # stream actually failed: the discarded chunks, then the fault that
            # ended it. A terminal error is never itself coalesced. Burying one
            # behind a run of survivable ones is exactly the failure the
            # participant-side throttle takes care to avoid, and the console
            # owes the researcher the same care.
            self._flush_console_repeats(source)
        self._encounter_event_soon(
            "voice_error", agent_id=agent_id,
            detail=f"{source}: {gateway_text}",
            # A transient fault is one the session survived (a discarded audio
            # chunk); an error that ended something costs the encounter a turn,
            # so the two must not read the same on a console being scanned at a
            # glance.
            severity="warn" if transient else "error",
            coalesce_key=source if transient else None,
        )

    def _watch_store_for_swallowed_failures(self) -> None:
        """Tee the session store so failures written outside this file are seen.

        Session.auto_steer catches its own exception and writes auto_steer_error
        by design — a steering outage must never break a live encounter — which
        also means _steer cannot tell a review that failed from a review that
        found nothing to change. A steering pass that errors every turn means no
        gears moved for the whole encounter, and that is exactly the kind of
        quiet false success the console exists to expose, so the runner listens
        at the store instead of guessing. Only the types below are forwarded;
        everything else is written through untouched, and the wrapper delegates
        unconditionally so a failure here can never cost the record an event.
        """
        store = self.session.store
        original = store.event
        # A store can only be teed once. Wrapping a wrapper would report the
        # same steering outage twice per turn, and a console whose counts are
        # inflated by its own plumbing is one a researcher learns to discount —
        # which costs more than the strip is worth.
        if getattr(original, "_rf_encounter_tee", False):
            return
        forwarded = {"auto_steer_error": "error"}

        def tee(type_: str, **fields):
            try:
                severity = forwarded.get(type_)
                if severity is not None:
                    self._encounter_event_soon(
                        type_, agent_id=fields.get("agent_id"),
                        detail=fields.get("message"), severity=severity,
                    )
            except Exception:  # noqa: BLE001
                pass
            return original(type_, **fields)

        tee._rf_encounter_tee = True
        store.event = tee

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
