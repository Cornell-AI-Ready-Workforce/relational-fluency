"""The group room, held to what the gateway was measured to do.

Every assertion here comes from a live round against api.ai.it.cornell.edu on
2026-09-10: a real three-member GroupRoom plus scribe, one synthesized
participant utterance (Windows SAPI, generated on the machine, no participant
data), on both `nto.gemini-live-2.5-flash` and `gpt-realtime-2.1`. What that
round found about the ROOM specifically:

  * `voice="Puck"` on gpt-realtime-2.1 comes back
    `invalid_value: Invalid value: 'Puck'. Supported values are: 'alloy', 'ash',
    'ballad', 'coral', 'echo', 'sage', 'shimmer', 'verse', 'marin', 'cedar'`
    and NO `session.updated` follows. The refusal is of the WHOLE
    `session.update`, so the character brief in the same frame goes with it and
    the session plays the gateway's stock assistant. The room built its scribe
    with a hardcoded "Puck", so the channel briefed "Never speak" was never
    briefed at all — which is how it came to speak. An ElevenLabs voice_id is
    refused by BOTH families; on Gemini the session then stays silent for good.
  * Both families answer on EVERY open session after speech plus silence,
    committed or not: three members, three replies, two of them discarded by the
    runner's pump after being generated and billed. On gpt that stops the moment
    the family's own turn detection is switched off; on Gemini nothing stops it,
    and `response.cancel` does not either — one cancelled reply went on to
    deliver 44 more audio deltas.
  * Asking the floor-holder for a reply the gateway is already producing gets a
    second reply spoken onto the end of the first, inside one recorded turn:
    "...get back on track.It's a tough situation but".
  * `pending_input` is a local counter of what we appended. The gateway's own
    commit consumes the buffer without resetting it, so give_floor's
    `pending_input < 3200` test read "full" on exactly the grant whose buffer
    was empty.

None of that is asserted here by opening a socket. tests/conftest.py refuses
every outbound connection by design, and a research instrument's test suite has
no business spending gateway credit or depending on what today's network
answers. So the gateway is modelled by `MeasuredGateway`, whose behaviour is the
list above and nothing else, and whose per-family answers come from the same
REALTIME_FAMILIES row the room reads. The live verification itself runs outside
pytest, against the real gateway, and is reported with its session count.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import group_room as gr  # noqa: E402
from server.voice.realtime import capabilities_for  # noqa: E402


# The two families, by the names REALTIME_MODEL actually carries. GEMINI is what
# config/consent.yaml tells participants their voice goes to, so it is the one
# that has to work.
GEMINI = "nto.gemini-live-2.5-flash"
GPT = "gpt-realtime-2.1"
FAMILIES = (GEMINI, GPT)

ELEVENLABS_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

# What the runner's _voice() hands out today: GEMINI_VOICES by cast position,
# whatever REALTIME_MODEL says. On gpt every one of them is refused.
SHIPPED_VOICES = ("Puck", "Charon", "Kore")


@pytest.fixture(autouse=True)
def _short_adoption_wait(monkeypatch):
    """give_floor waits up to AUTOFIRE_WAIT to see whether the gateway answered
    the commit on its own. These tests assert on what was sent and in what
    order, never on how patient the room is, so the ceiling is pinned short —
    the same 0.05 the runner's own group tests pin, so one knob stays one knob.
    """
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.05")


class Agent:
    def __init__(self, aid: str, name: str):
        self.id = aid
        self.name = name


CAST = [Agent("dan", "Dan"), Agent("priya", "Priya"), Agent("mel", "Mel")]


class Session:
    """One session on MeasuredGateway's wire.

    `briefed` is the whole point: False whenever the gateway refused the
    `session.update`, which is what a voice off the family's roster does, and a
    session that was never briefed is a character that is not being played.
    """

    def __init__(self, gateway, instructions, voice, tools):
        self.gateway = gateway
        self.caps = gateway.caps
        self.instructions = instructions
        # An empty voice means the family default, which is what
        # realtime.resolve_voice() settles at connect.
        self.voice = voice
        self.tools = tools
        self.briefed = False
        self.ws = object()
        self.pending_input = 0
        self.autofire_active = False
        self._last_output_at = 0.0
        self._responding = False
        self.audio_in = b""
        self.commits = 0
        # How much audio each commit actually had to close. A commit that had
        # nothing is what came back `input_audio_buffer_commit_empty` on gpt and
        # produced no turn at all on Gemini.
        self.committed_bytes: list = []
        # `input_audio_buffer_commit_empty` on gpt; no turn at all on Gemini.
        self.empty_commits = 0
        self.responses_requested = 0
        self.replies = 0
        self.cancels = 0
        self.closed = False
        # _send counts a closed socket and drops the frame rather than raising,
        # so a dead member cannot stop the room hearing. That is the right
        # behaviour for a send and the reason a dead member is invisible.
        self.send_failures = 0
        self.last_send_error = ""
        self.raise_on_send = None
        self.rebriefs: list = []
        # How long after the participant stops before the gateway's own reply
        # starts. Zero is the easy case. The live round found the realistic one:
        # at 1.2 s of director latency the reply had NOT begun yet, so a grant
        # that only looks once sees nothing and commits — and on Gemini the
        # commit is what produces the second reply.
        self.autofire_delay = 0.0
        self._pending: list = []

    # -- what the gateway does with the opening session.update ---------------
    async def connect(self, *, open_conversation=True):
        if not self.voice:
            self.voice = self.caps.voices[0]
        elif not self.caps.accepts_voice(self.voice):
            # Measured: an error frame, no session.updated, and the instructions
            # in the same frame never take effect.
            self.briefed = False
            return
        self.briefed = True

    @property
    def _silenced(self) -> bool:
        """True when this session will not answer a turn it was not given."""
        return self.caps.needs_turn_detection_null

    async def send_audio(self, pcm):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        if self.ws is None:
            return
        if self.send_failures or self.closed:
            self.send_failures += 1
            return
        self.audio_in += pcm
        self.pending_input += len(pcm)
        if not self._silenced and len(self.audio_in) > 16000 and not self.replies:
            # Speech plus silence: the gateway answers, whatever we asked for.
            if self.autofire_delay <= 0:
                self._fire(autofire=True)
            else:
                self._pending.append(asyncio.ensure_future(self._fire_soon()))

    async def _fire_soon(self):
        await asyncio.sleep(self.autofire_delay)
        self._fire(autofire=True)

    def kill_socket(self, why="the gateway closed it"):
        """The quietest of the three deaths: the socket is closed but the
        attribute is still there, and every send is counted and dropped."""
        self.send_failures += 1
        self.last_send_error = why

    def _fire(self, *, autofire: bool):
        self.replies += 1
        self._responding = True
        self.autofire_active = autofire
        self._last_output_at = time.time()
        # The gateway's own commit consumes the buffer. It does NOT tell the
        # client, so pending_input keeps whatever it was.
        self.audio_in = b""

    @property
    def responding(self):
        return self._responding

    def clear_response_state(self):
        self._responding = False
        self.autofire_active = False

    async def commit_input(self):
        self.commits += 1
        self.committed_bytes.append(len(self.audio_in))
        self.pending_input = 0
        if not self.audio_in:
            self.empty_commits += 1
        elif self.audio_in.strip(b"\x00") or self.caps.needs_turn_detection_null:
            # Both families start a reply off the commit alone — see the
            # `conversation_already_has_active_response` branch in
            # server/voice/realtime.py, where an explicit response.create sent
            # behind one of these is refused. The exception, measured, is a
            # commit holding nothing but the silence pad on Gemini: it produces
            # no turn and no reply, which is why a scene never opens there.
            self._fire(autofire=True)

    async def request_response(self):
        self.responses_requested += 1
        self._fire(autofire=False)

    async def cancel_response(self):
        self.cancels += 1
        if self.caps.needs_turn_detection_null:
            self._responding = False
            self.autofire_active = False
        # On Gemini cancel is inert: the reply keeps coming. Deliberately no
        # state change here, so a test cannot pass by assuming otherwise.

    async def update_instructions(self, instructions):
        self.instructions = instructions
        self.rebriefs.append(instructions)
        if self.ws is None or self.send_failures:
            return False
        # A mid-session session.update is acknowledged only where the family's
        # row says it is.
        return bool(self.caps.honours_session_update)

    async def close(self):
        self.closed = True
        self.ws = None


class MeasuredGateway:
    """A session factory that behaves the way the gateway was measured to."""

    def __init__(self, model):
        self.model = model
        self.caps = capabilities_for(model)
        self.made: list = []

    def __call__(self, *, instructions="", voice="", tools=None):
        s = Session(self, instructions, voice, tools)
        self.made.append(s)
        return s


def make_room(monkeypatch, model, *, voice_for=None, on_lost=None):
    gateway = MeasuredGateway(model)
    monkeypatch.setattr(gr, "RealtimeVoiceSession", gateway)
    room = gr.GroupRoom(
        list(CAST),
        instructions_for=lambda a: f"You are {a.name}.",
        voice_for=voice_for or (lambda a: SHIPPED_VOICES[CAST.index(a)]),
        tools=[],
        model=model,
        on_lost=on_lost,
    )
    return room, gateway


def run_room(model, body, *, voice_for=None, on_lost=None):
    """Open a room on `model`, run `body(room)`, hand the room back."""
    mp = pytest.MonkeyPatch()

    async def scenario():
        room, gw = make_room(mp, model, voice_for=voice_for, on_lost=on_lost)
        await room.open()
        await body(room)
        return room
    try:
        return asyncio.run(scenario())
    finally:
        mp.undo()


async def a_participant_speaks(room):
    await room.hear(b"\x01\x02" * 12000)


# --------------------------------------------------------------------------
# 1. The voice, and the brief it can take down with it.
# --------------------------------------------------------------------------

def test_the_scribe_takes_the_familys_voice_instead_of_a_hardcoded_one():
    """A transcription channel has nothing to gain from a particular voice and
    its whole brief to lose. The room hardcoded `voice="Puck"`, which
    gpt-realtime-2.1 refuses outright — and the refusal is of the entire
    session.update, so "Never speak" went with it and the scribe played the
    gateway's stock assistant in a room the participant could hear."""
    async def nothing(room):
        return None

    for model in FAMILIES:
        room = run_room(model, nothing)
        caps = capabilities_for(model)
        scribe = room.scribe
        assert scribe is not None, f"{model}: the room opened without a scribe"
        assert caps.accepts_voice(scribe.voice), (
            f"{model}: the scribe asked for voice={scribe.voice!r}, which this "
            f"family refuses; a voice it never uses can only cost it its brief"
        )
        assert scribe.briefed is True, (
            f"{model}: the scribe was never briefed, so 'Never speak' never "
            f"reached it"
        )


def test_every_member_is_actually_briefed_on_both_families():
    """The room is three characters or it is nothing. A voice the family refuses
    does not degrade one member's delivery, it deletes the character: no
    session.updated, no persona, the gateway's stock assistant answering in a
    study encounter.

    The voices here are the ones the runner really hands out — GEMINI_VOICES by
    cast position, whatever REALTIME_MODEL says — so this is the shipped
    configuration and not a contrived one. On gpt-realtime-2.1 it costs all
    three characters their briefs at once."""
    async def nothing(room):
        return None

    for model in FAMILIES:
        room = run_room(model, nothing)
        assert len(room.sessions) == len(CAST)
        for aid, rt in room.sessions.items():
            assert rt.briefed is True, f"{model}: {aid} was never briefed"


def test_a_voice_the_family_refuses_never_reaches_the_wire():
    """Group scenarios still hand the room an ElevenLabs voice_id, left over
    from the retired v1 cascade. Both families refuse it and drop the brief with
    it, and connect() now refuses it before the socket exists — which for a room
    means no group interaction opens at all. The room is the last place that can
    catch it, so it does, substituting this family's voice at the same cast
    position rather than the family default, because a room where every
    character shares one voice is not a room."""
    async def nothing(room):
        return None

    for model in FAMILIES:
        room = run_room(model, nothing, voice_for=lambda a: ELEVENLABS_VOICE_ID)
        caps = capabilities_for(model)
        voices = [rt.voice for rt in room.sessions.values()]
        assert ELEVENLABS_VOICE_ID not in voices, (
            f"{model}: a voice this family refuses was sent anyway"
        )
        assert all(caps.accepts_voice(v) for v in voices)
        assert all(rt.briefed for rt in room.sessions.values()), (
            f"{model}: a refused voice cost a character its brief"
        )
        assert len(set(voices)) == len(CAST), (
            f"{model}: the cast collapsed onto one voice: {voices}"
        )
        # And it says so, rather than a character quietly changing voice.
        assert set(room.voice_substitutions) == {a.id for a in CAST}
        assert set(room.voice_substitutions.values()) == {ELEVENLABS_VOICE_ID}


def test_a_voice_this_family_does_accept_is_left_alone():
    """The guard is a net under a fall, not a policy. A voice the family
    recognises goes through exactly as the caller cast it."""
    async def nothing(room):
        return None

    room = run_room(GEMINI, nothing, voice_for=lambda a: "Kore")
    assert [rt.voice for rt in room.sessions.values()] == ["Kore"] * len(CAST)
    assert room.voice_substitutions == {}


# --------------------------------------------------------------------------
# 2. The floor.
# --------------------------------------------------------------------------

def test_where_the_family_switches_its_turn_detection_off_the_floor_is_real():
    """gpt-realtime-2.1. Measured live: with turn_detection null the two members
    that were not committed produced no response, no commit, nothing to pay for,
    and no `input_audio_buffer_commit_empty` on the one that was."""
    async def a_turn(room):
        await a_participant_speaks(room)
        room.granted = await room.give_floor("priya")

    room = run_room(GPT, a_turn)

    assert room.floor_is_real is True
    assert room.granted is not None
    spoke = {aid: rt.replies for aid, rt in room.sessions.items()}
    assert spoke == {"dan": 0, "priya": 1, "mel": 0}, (
        f"the room paid for replies nobody hears: {spoke}"
    )
    assert room.scribe.replies == 0, "the scribe answered the participant"
    assert room.session_for("priya").committed_bytes[-1] > 0, (
        "the floor holder's turn was closed on an empty buffer"
    )


def test_where_it_is_not_the_room_says_so_rather_than_pretending():
    """Gemini. Every member answers every turn; the key that fixes it on gpt is
    accepted there and changes nothing; response.cancel does not stop a reply
    that has started. There is no floor to build on that family, so the room
    builds none — and the docstring that used to promise one is what this test
    is really guarding."""
    async def a_turn(room):
        await a_participant_speaks(room)
        room.granted = await room.give_floor("priya")

    room = run_room(GEMINI, a_turn)

    assert room.floor_is_real is False
    assert room.granted is not None
    # Three replies for one participant turn. That is the truth about this
    # family, and the room's cost model has to be written from it.
    assert sum(rt.replies for rt in room.sessions.values()) == len(CAST)

    # And the docstring says so. It may still quote the mechanism it used to
    # promise — "keep the turn in their (uncommitted) buffer" — but only in
    # order to withdraw it. A docstring asserting a floor the gateway does not
    # implement is what kept this defect alive through two rounds of review.
    doc = gr.GroupRoom.give_floor.__doc__ or ""
    assert "They do not" in doc, (
        "give_floor documents a floor mechanism without saying that neither "
        "family implements it as shipped"
    )


def test_the_floor_holder_is_not_asked_for_a_reply_the_gateway_is_already_giving():
    """The doubled turn. On an auto-firing family the gateway has already begun
    this member's reply by the time the director has chosen them, and the
    commit-and-ask underneath produced a SECOND one, spoken onto the end of the
    first inside a single recorded assistant turn — and the first had never seen
    the stage direction. Live: "...get back on track.It's a tough situation
    but"."""
    async def a_turn(room):
        await a_participant_speaks(room)
        priya = room.session_for("priya")
        assert priya.autofire_active is True, "the fake did not model the gateway"
        room.before = priya.replies
        room.granted = await room.give_floor("priya")

    room = run_room(GEMINI, a_turn)
    priya = room.session_for("priya")

    assert room.granted is priya, "the floor was still granted"
    assert priya.replies == room.before, (
        "a second reply was requested on top of the one already in flight"
    )
    assert priya.responses_requested == 0
    assert room.autofire_grants == 1, "and the room counts what it declined to ask"


def test_a_reply_that_starts_after_the_floor_is_granted_is_adopted_too():
    """The version of the doubling that a bare "is it already talking?" check
    does not catch, and the one that actually happens.

    Live, in a three-member Gemini room with 1.2 s of director latency, the
    gateway's reply had NOT begun by the time the floor was granted. So the
    grant committed — and on that family the COMMIT is what produces the second
    reply, not the response.create behind it: with the wait placed after the
    commit the character still produced two response.dones and a transcript
    reading "...prevent it in the future.I think it's a concern and we need
    to". Nothing may be sent to a session whose turn is already being answered,
    and finding that out is worth waiting AUTOFIRE_WAIT for."""
    async def a_turn(room):
        for rt in room.sessions.values():
            rt.autofire_delay = 0.02        # inside the wait, after the grant
        await a_participant_speaks(room)
        priya = room.session_for("priya")
        assert priya.autofire_active is False, "the fake did not model the delay"
        room.granted = await room.give_floor("priya")

    room = run_room(GEMINI, a_turn)
    priya = room.session_for("priya")

    assert room.granted is priya, "the floor was still granted"
    assert priya.commits == 0, (
        "the turn was committed to a session the gateway was already answering"
    )
    assert priya.responses_requested == 0
    assert priya.replies == 1, "the character answered twice in one turn"
    assert room.autofire_grants == 1


def test_a_member_that_has_heard_nothing_is_not_made_to_wait_for_a_reply_to_it(
        monkeypatch):
    """The scene open, where the whole point is to make a session that has heard
    nothing speak. There is no turn there for the gateway to answer unprompted,
    so waiting AUTOFIRE_WAIT to find that out is a second and a half of silence
    at the top of every group interaction, on the path least able to afford it.

    The room knows this and the session cannot: pending_input counts what was
    appended and a suppressing pump zeroes it, while the gateway's own commit
    consumes the buffer without saying so. `_fanned_since_grant` is the room's
    own books."""
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.5")

    async def open_the_scene(room):
        started = time.time()
        room.granted = await room.give_floor("dan")
        room.elapsed = time.time() - started

    room = run_room(GEMINI, open_the_scene)
    dan = room.session_for("dan")

    assert room.granted is dan
    # One wait, after the commit, not two. Nothing was going to answer a commit
    # nobody had spoken into, so the wait in front of it is pure silence.
    assert room.elapsed < 0.9, (
        f"a grant to a member that has heard nothing spent {room.elapsed:.2f}s "
        f"waiting twice for a reply to audio nobody sent"
    )
    assert dan.committed_bytes == [len(gr._SILENCE_PAD)]
    assert dan.responses_requested == 1, "and it did ask the character to speak"


def test_a_grant_does_not_ask_for_the_reply_its_own_commit_already_started():
    """Both families answer the commit itself, so the response.create behind it
    is either refused or granted, and both are damage. Refused on gpt: a
    transient error frame on every single group turn, which reaches the
    participant's page as "Something went wrong" — seen in the live three-member
    room, once per grant. Granted on Gemini: the doubled turn.

    So the grant waits AUTOFIRE_WAIT to see whether anyone started, and asks only
    if nobody did. It is the decision _client_to_model already makes on the 1:1
    path, on the same knob."""
    async def a_turn(room):
        await a_participant_speaks(room)
        room.granted = await room.give_floor("priya")

    room = run_room(GPT, a_turn)
    priya = room.session_for("priya")

    assert room.granted is priya
    assert priya.commits == 1, "the turn was never closed"
    assert priya.replies == 1, "the character spoke once, or not at all"
    assert priya.responses_requested == 0, (
        "a response.create went out behind a reply the commit had already "
        "started"
    )
    assert room.autofire_grants == 1


def test_the_silence_pad_is_not_skipped_because_a_local_counter_is_stale():
    """pending_input counts what WE appended. The gateway's own commit consumes
    the buffer and does not reset it, so the old `pending_input < 3200` test
    read "full" on precisely the grant whose buffer was empty — and that commit
    is the one that came back `input_audio_buffer_commit_empty` on gpt and
    produced no turn at all on Gemini."""
    async def a_turn(room):
        mel = room.session_for("mel")
        # What a gateway auto-commit leaves behind: an empty buffer and a
        # counter that still says 48000 bytes are waiting in it.
        mel.pending_input = 48000
        mel.audio_in = b""
        await room.give_floor("mel")

    room = run_room(GPT, a_turn)
    mel = room.session_for("mel")

    assert mel.commits == 1, "the turn was never closed"
    # The pad went out BEFORE the commit, so the commit had something to close.
    # [0] here is the live `input_audio_buffer_commit_empty`.
    assert mel.committed_bytes == [len(gr._SILENCE_PAD)], (
        f"the commit closed a buffer holding {mel.committed_bytes} bytes; the "
        f"stale counter made give_floor skip the pad on exactly the grant that "
        f"needed it"
    )
    assert mel.empty_commits == 0
    assert mel.replies == 1, "the character never spoke"


def test_a_grant_to_a_member_whose_socket_is_gone_reports_failure():
    """_send counts a closed socket and drops the frame rather than raising, so
    that one dead member cannot stop a whole room hearing. Right for a send, and
    it means a grant cannot learn from an exception that it failed: without the
    check the runner is told the floor was granted and then waits out the full
    45 s turn timeout on a session that never received the request."""
    async def a_turn(room):
        room.session_for("dan").kill_socket("input_audio_buffer.append: 1006")
        room.granted = await room.give_floor("dan")

    room = run_room(GEMINI, a_turn)
    assert room.granted is None, "a dead member was handed the floor"
    assert room.session_for("dan") is None
    assert "dan" in room.lost and "1006" in room.lost["dan"]


def test_a_grant_to_a_member_that_raises_still_fails_the_way_it_always_did():
    """Unchanged behaviour, kept under test because everything around it moved:
    a member that raises drops out of the room and the grant reports failure, or
    `speaking` names somebody who will never answer and the room is mute for the
    rest of the interaction."""
    async def a_turn(room):
        room.session_for("dan").raise_on_send = ConnectionResetError("dropped")
        room.granted = await room.give_floor("dan")

    room = run_room(GEMINI, a_turn)
    assert room.granted is None
    assert room.session_for("dan") is None


# --------------------------------------------------------------------------
# 3. A member that stopped hearing the room.
# --------------------------------------------------------------------------

def test_a_member_whose_socket_is_gone_is_seen_rather_than_fanned_to_forever():
    """The quietest half of the fault, and the one no exception handling could
    have caught: _send returns early on a socket that is None and counts-and-
    drops a ConnectionClosed on one that is closed. A character deaf since the
    first minute and one listening are the same object from here unless the
    socket and that counter are read."""
    seen = []

    async def two_turns(room):
        room.session_for("mel").ws = None              # dropped to None
        room.session_for("dan").kill_socket("1006")    # closed, counted
        await room.hear(b"\x01\x02" * 100)
        await room.hear(b"\x01\x02" * 100)             # reported once each

    room = run_room(GEMINI, two_turns, on_lost=lambda cid, why: seen.append(cid))

    assert "mel" in room.lost and "websocket" in room.lost["mel"]
    assert "dan" in room.lost and "1006" in room.lost["dan"]
    assert room.session_for("mel") is None and room.session_for("dan") is None, (
        "a member that cannot hear must not still be seated: session_for is how "
        "every caller finds out"
    )
    assert sorted(seen) == ["dan", "mel"], f"reported {seen}"
    assert room.session_for("priya").audio_in, "the rest of the room went deaf too"


def test_a_member_that_raises_on_send_is_reported_not_swallowed():
    """`return_exceptions=True` put this one in a list nobody read, so a member
    whose socket the gateway tore down stayed in the room, was fanned audio for
    the rest of the encounter, and never answered."""
    async def one_turn(room):
        room.session_for("dan").raise_on_send = ConnectionResetError("gone")
        await room.hear(b"\x01\x02" * 100)

    room = run_room(GEMINI, one_turn)
    assert "dan" in room.lost and "ConnectionResetError" in room.lost["dan"]
    assert room.session_for("dan") is None
    assert room.session_for("priya").audio_in


def test_the_scribe_going_deaf_is_reported_through_the_same_door():
    """The scribe is the ONLY participant transcript channel in a room. A scribe
    that stopped receiving is the study's primary measurement stopping, so it
    cannot be the one loss the room does not mention."""
    async def one_turn(room):
        room.scribe.ws = None
        await room.hear(b"\x01\x02" * 100)

    room = run_room(GEMINI, one_turn)
    assert gr.SCRIBE_ID in room.lost
    assert room.scribe is None, "a deaf scribe must not go on being fanned to"


def test_agent_audio_still_never_reaches_the_scribe():
    """The reason the scribe exists. Its input transcription is the clean
    participant channel only because it never hears a character."""
    async def one_turn(room):
        await room.hear(b"\x07\x07" * 50, exclude="dan")

    room = run_room(GEMINI, one_turn)
    assert room.scribe.audio_in == b"", "the scribe heard a character"
    assert room.session_for("dan").audio_in == b"", "the speaker heard itself"
    assert room.session_for("priya").audio_in, "the room did not hear the speaker"


# --------------------------------------------------------------------------
# 4. Re-briefing, and whether it arrived.
# --------------------------------------------------------------------------

def test_a_rebrief_that_the_family_will_not_acknowledge_is_recorded_as_such():
    """Every stage direction in a group interaction travels by mid-session
    session.update. On the family the study is configured for, that frame is not
    acknowledged and the actor does not obey it — so a rebrief there is a thing
    the record must be able to say did not land. gather(return_exceptions=True)
    used to drop the answer entirely."""
    async def rebrief(room):
        await room.rebrief(lambda a: f"You are {a.name}, in the closing beat.")

    gemini = run_room(GEMINI, rebrief)
    assert set(gemini.last_rebrief) == {a.id for a in CAST}
    assert all(v is False for v in gemini.last_rebrief.values()), (
        f"a re-brief nobody acknowledged was recorded as fine: "
        f"{gemini.last_rebrief}"
    )
    # It was still SENT, and the floor was still released.
    assert all(rt.rebriefs for rt in gemini.sessions.values())
    assert gemini.speaking is None

    gpt = run_room(GPT, rebrief)
    assert all(v is True for v in gpt.last_rebrief.values())


# --------------------------------------------------------------------------
# 5. The table the room reads.
# --------------------------------------------------------------------------

def test_the_room_reads_the_one_table_rather_than_keeping_a_second_copy():
    """Per-family behaviour belongs in REALTIME_FAMILIES, in one place. A room
    that carried its own copy would be a second place to update and a second
    place to be wrong."""
    assert gr.capabilities_for is not None
    gemini = capabilities_for(GEMINI)
    gpt = capabilities_for(GPT)
    assert "Puck" in gemini.voices and not gemini.accepts_voice("alloy")
    assert "alloy" in gpt.voices and not gpt.accepts_voice("Puck")

    async def nothing(room):
        return None

    assert run_room(GEMINI, nothing).caps is gemini
    assert run_room(GPT, nothing).caps is gpt
    # floor_is_real is a reading of the row, not an opinion of its own.
    assert run_room(GEMINI, nothing).floor_is_real is gemini.needs_turn_detection_null
    assert run_room(GPT, nothing).floor_is_real is gpt.needs_turn_detection_null


def test_a_family_the_table_does_not_cover_leaves_the_callers_voice_alone():
    """The room guesses at nothing. With no row it passes the caller's choice
    through — connect() is where an unknown model is refused, loudly, and the
    room must not pre-empt that with an invention of its own."""
    class Bare:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.ws = object()
            self.pending_input = 0
            self.autofire_active = False
            self.send_failures = 0

        async def connect(self, **kw):
            return None

        async def close(self):
            self.ws = None

    mp = pytest.MonkeyPatch()

    async def scenario():
        mp.setattr(gr, "RealtimeVoiceSession", Bare)
        room = gr.GroupRoom(
            list(CAST), instructions_for=lambda a: "x",
            voice_for=lambda a: "whatever-they-chose", tools=[],
            model="some.model.nobody.has.tried",
        )
        await room.open()
        return room
    try:
        room = asyncio.run(scenario())
    finally:
        mp.undo()

    assert room.caps is None
    assert room.floor_is_real is False
    assert all(rt.voice == "whatever-they-chose" for rt in room.sessions.values())
    assert room.voice_substitutions == {}
