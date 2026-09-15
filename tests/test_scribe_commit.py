"""The participant's own words, in a group encounter.

A room's scribe is the ONLY channel that transcribes the participant:
_pump_member throws its own `user_transcript` events away on purpose, because a
member's input buffer also carries the other characters' fanned-out audio and
the bridge labels all of it "user". And a buffer is transcribed only when it is
committed. `GroupRoom.give_floor` commits the member it is granting and nobody
else, so on a family whose own turn detection has to be switched off — see
`needs_turn_detection_null` in server/voice/realtime.py's REALTIME_FAMILIES,
which is the gpt-realtime row — NOTHING ever closed the scribe's buffer and the
participant was never transcribed at all.

Measured live on gpt-realtime-2.1 against api.ai.it.cornell.edu, on the real
S4A room with three synthetic participant utterances: five assistant_turns,
2.18 MB of participant audio in user_audio.wav, `participant_turns: 0` in the
record, and tools/encounter_health.py failing the encounter with "no user_turn
events at all". Characters answering somebody who, on paper, said nothing. The
participant's speech is the study's dependent variable, so such an encounter
cannot be rated at all, and this one bug is what stopped group encounters
working on the family that DOES honour a mid-session stage direction.

The scribe's fake here behaves the way that gateway behaves with its own turn
detection off: it transcribes what it was given when, and only when, its buffer
is committed. That is what makes these tests measure the commit rather than
restate it.

Two failure modes are worse than a missing turn, and both are pinned below,
because a duplicated or fabricated participant line looks like data:
  * committing twice around one utterance (a doubled participant turn), and
  * committing a buffer this room put nothing in (a transcript of nothing,
    attributed to the participant).

No network and no credentials. The scenario is the real S4A, loaded through the
real loader, because the room shape being exercised is scenario-authored.
"""

from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import group_room as gr  # noqa: E402
from server import realtime_voice_session as rvs  # noqa: E402
from server.group_room import SCRIBE_ID, GroupRoom  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402
from server.voice.realtime import capabilities_for  # noqa: E402

# The two families the deployment can actually be pointed at. Named as models,
# not as rows, so that a table edit which retired one of them would fail here
# instead of quietly leaving a family untested.
GPT = "gpt-realtime-2.1"                 # floor is real: its VAD is switched off
GEMINI = "nto.gemini-live-2.5-flash"     # the configured model; its VAD closes turns

GROUP_SCENARIO = "S4A"


def in_a_loop(fn):
    """Run an async test body. This suite carries no pytest-asyncio; every
    other async test here opens its own loop the same way."""

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return wrapper

# 20 ms of 16 kHz PCM16. SilenceDetector's default threshold is an RMS of 500,
# so `LOUD` is speech to it and `QUIET` is not.
FRAME = 320
LOUD = (b"\x00\x20" * FRAME)
QUIET = b"\x00\x00" * FRAME


# --------------------------------------------------------------------------
# Fakes, in the shapes the room and the pumps actually use.
# --------------------------------------------------------------------------

class FakeRT:
    """A realtime session that records what was sent to it."""

    def __init__(self, instructions="", voice="", tools=None):
        self.ws = object()
        self.voice = voice
        self.instructions = [instructions]
        self.autofire_active = False
        self.pending_input = 0
        self.send_failures = 0
        self.last_send_error = ""
        self.audio_in = b""
        self.commits = 0
        self.cancels = 0
        self.responses = 0
        self.closed = False
        self._responding = False
        self._q: asyncio.Queue = asyncio.Queue()

    # -- what the room drives -------------------------------------------
    @property
    def responding(self):
        return self._responding

    def clear_response_state(self):
        self._responding = False

    async def connect(self, *, open_conversation=True):
        return None

    async def close(self):
        self.closed = True
        self.ws = None
        self._q.put_nowait(None)

    async def send_audio(self, pcm):
        self.audio_in += pcm
        self.pending_input += len(pcm)

    async def commit_input(self):
        self.commits += 1
        self.pending_input = 0

    async def request_response(self):
        self.responses += 1
        self._responding = True

    async def cancel_response(self):
        self.cancels += 1
        self._responding = False

    async def update_instructions(self, instructions):
        self.instructions.append(instructions)
        return True

    # -- what a pump drives ---------------------------------------------
    def feed(self, ev):
        self._q.put_nowait(ev)

    def end(self):
        self._q.put_nowait(None)

    async def events(self):
        while True:
            ev = await self._q.get()
            if ev is None:
                return
            yield ev


class ScribeRT(FakeRT):
    """The transcription channel, behaving as the measured gateway does with
    its own turn detection switched off: a committed buffer is transcribed, an
    uncommitted one is not, and nothing at all comes back until the commit."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.transcripts: list = []
        # What the room has appended since the last commit, minus the 300 ms
        # silence pad the room adds itself.
        self.heard = 0

    async def send_audio(self, pcm):
        await super().send_audio(pcm)
        if any(pcm):                       # the pad is pure zeroes
            self.heard += len(pcm)

    async def commit_input(self):
        await super().commit_input()
        if self.heard:
            self.heard = 0
            # One transcript per closed turn, exactly as the bridge delivers
            # it, and a discarded reply behind it — the price the room's own
            # docstring says this costs on a family that answers every commit.
            self.feed({"type": "user_transcript",
                       "text": f"participant utterance {len(self.transcripts) + 1}"})
            self.transcripts.append(len(self.transcripts) + 1)


class Gateway:
    """Hands out sessions; the first one built is the scribe (GroupRoom.open
    starts it first), which is the only one that needs the transcribing fake."""

    def __init__(self):
        self.made = []

    def __call__(self, instructions="", voice="", tools=None):
        cls = ScribeRT if not self.made else FakeRT
        rt = cls(instructions, voice, tools)
        self.made.append(rt)
        return rt


class FakeStore:
    def __init__(self):
        self.events = []
        self.user_audio = b""
        self.audio = {}

    def event(self, type_, **fields):
        self.events.append(dict(type=type_, **fields))

    def append_user_audio(self, pcm):
        self.user_audio += pcm

    def append_assistant_audio(self, pcm, agent_id=None):
        self.audio[agent_id] = self.audio.get(agent_id, b"") + pcm

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeEngine:
    def __init__(self, agent):
        self.agent = agent

    def _system_prompt(self, branches, note, group=False):
        return f"SYSTEM PROMPT for {self.agent.id}"


class FakeDirector:
    model = "fake-director"

    async def route(self, history, text):
        return []


class FakeSession:
    def __init__(self, scenario_id=GROUP_SCENARIO):
        self.scenario = load_scenario(scenario_id, "p_test")
        self.is_group = self.scenario.mode == "group"
        self.engines = {a.id: FakeEngine(a) for a in self.scenario.cast}
        self.store = FakeStore()
        self.director = FakeDirector()
        self.triggered_branches = []
        self.shared_history = []
        self.steering_log = []

    def append_user(self, text):
        self.shared_history.append({"speaker": "user", "text": text})

    def append_agent(self, agent_id, text):
        self.shared_history.append({"speaker": agent_id, "text": text})

    async def broadcast(self, payload):
        pass

    async def auto_steer(self, *, delivered=None):
        pass


class ScriptedWS:
    """The browser's end of the socket: a fixed list of inbound frames, then a
    disconnect. Everything sent back is kept."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.json = []
        self.binary = []

    async def receive(self):
        if self._frames:
            return {"type": "websocket.receive", "bytes": self._frames.pop(0)}
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        self.binary.append(payload)

    def frames(self, type_):
        return [f for f in self.json if f.get("type") == type_]


def utterance(speech_frames=20, silence_frames=60):
    """One participant turn as the worklet delivers it: speech, then enough
    quiet for SilenceDetector to call the turn ended (900 ms by default)."""
    return [LOUD] * speech_frames + [QUIET] * silence_frames


async def build(model, *, frames, floor_holder=None):
    """A runner with a real GroupRoom of fake sessions on `model`, driven
    through the real _client_to_model with `frames` as the participant.

    Returns (runner, room, scribe, order) where `order` records the scribe's
    commit count at the instant each group turn was spawned — which is how the
    ordering claim ("the transcript is closed BEFORE routing") is checked
    rather than assumed.
    """
    session = FakeSession()
    ws = ScriptedWS(frames)
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)

    gateway = Gateway()
    real = gr.RealtimeVoiceSession
    gr.RealtimeVoiceSession = gateway
    try:
        agents = runner._resolve_agents()
        room = GroupRoom(
            agents,
            instructions_for=lambda a: f"You are {a.name}.",
            voice_for=lambda a: "",
            tools=[],
            model=model,
        )
        await room.open()
    finally:
        gr.RealtimeVoiceSession = real

    runner.room = room
    runner.rt = room.session_for(agents[0].id)
    if floor_holder is not None:
        room.speaking = floor_holder

    scribe = room.scribe
    order = []

    async def probe_turn():
        order.append(scribe.commits)

    runner._run_group_turn = probe_turn
    await runner._client_to_model()
    # Let any spawned probe run.
    for _ in range(3):
        await asyncio.sleep(0)
    return runner, room, scribe, order


# --------------------------------------------------------------------------
# The bug: a participant whose words never reach the record.
# --------------------------------------------------------------------------

@in_a_loop
async def test_the_participants_turn_is_closed_on_the_scribe():
    """On a family whose own turn detection is off, the end of a participant
    turn must close that turn on the transcription channel. Nothing else in the
    room does: give_floor commits the member it is granting and no one else."""
    runner, room, scribe, order = await build(GPT, frames=utterance())

    assert room.floor_is_real, "the gpt-realtime row is the one with VAD off"
    assert scribe.audio_in, "the scribe heard the participant"
    assert scribe.commits == 1, (
        "the participant's turn was never closed on the scribe, so the gateway "
        "never transcribed it and the encounter has no participant speech in it"
    )
    assert room.scribe_commits == 1


@in_a_loop
async def test_the_participants_words_reach_the_record():
    """The end of it: a `user_turn` in the store, with the participant's text.

    This is the assertion the study cares about. participant_turns is what a
    rater reads, what scoring indexes, and what tools/encounter_health.py
    requires; an encounter without it cannot be rated at all.
    """
    runner, room, scribe, order = await build(GPT, frames=utterance())
    # The scribe's transcript arrives on its own stream, as it does live.
    pump = asyncio.ensure_future(runner._pump_scribe(scribe))
    await asyncio.sleep(0.05)
    scribe.end()
    await pump

    turns = [e["text"] for e in runner.session.store.of("user_turn")]
    assert turns == ["participant utterance 1"], (
        "the record shows characters talking to a participant who appears to "
        "have said nothing"
    )


@in_a_loop
async def test_the_turn_is_closed_before_the_director_routes():
    """Ordering, not merely occurrence.

    _run_group_turn waits up to ROUTE_TRANSCRIPT_WAIT for THIS turn's
    transcript before it routes, and _named_in reads it to see whether the
    participant addressed somebody by name. A commit that happened after the
    spawn would leave the director routing on the previous turn's words.
    """
    runner, room, scribe, order = await build(GPT, frames=utterance())
    assert order == [1], (
        "the group turn was spawned before the participant's turn was closed, "
        "so the director routes on stale text"
    )


@in_a_loop
async def test_every_participant_turn_is_closed_exactly_once():
    """Three utterances, three transcripts. Not two, and not six.

    A doubled commit around one utterance is worse than a missing one: it puts
    a participant turn in the record that the participant did not take.
    """
    frames = utterance() + utterance() + utterance()
    runner, room, scribe, order = await build(GPT, frames=frames)
    assert scribe.commits == 3
    assert order == [1, 2, 3]
    assert len(scribe.transcripts) == 3


# --------------------------------------------------------------------------
# The configured family must not change.
# --------------------------------------------------------------------------

@in_a_loop
async def test_the_configured_family_is_not_committed_by_the_room():
    """On nto.gemini-live-2.5-flash the gateway's own turn detection closes the
    participant's buffer, and the room must keep its hands off it.

    A commit here would buy nothing and cost a generated-and-discarded reply on
    every participant turn — on the family where response.cancel is inert, so
    the reply is produced and billed in full whatever the room does about it.
    """
    runner, room, scribe, order = await build(GEMINI, frames=utterance())

    assert not room.floor_is_real
    assert scribe.audio_in, "the scribe still hears the participant"
    assert scribe.commits == 0, "the room committed a buffer that was not its to close"
    assert room.scribe_commits == 0
    # The turn is still routed: the fix adds an await, not a branch.
    assert order == [0]


def test_the_two_families_disagree_about_this_for_a_reason():
    """The switch is `needs_turn_detection_null` in one table, not a second
    copy of the finding in the room."""
    assert capabilities_for(GPT).needs_turn_detection_null is True
    assert capabilities_for(GEMINI).needs_turn_detection_null is False


# --------------------------------------------------------------------------
# The neighbouring cases.
# --------------------------------------------------------------------------

@in_a_loop
async def test_a_participant_who_says_nothing_is_never_transcribed():
    """Silence produces no turn boundary, so no commit, so no transcript.

    An empty commit is `input_audio_buffer_commit_empty` on gpt and no turn at
    all on Gemini; a commit holding only the room's silence pad would hand the
    record a transcript of nothing, attributed to the participant.
    """
    runner, room, scribe, order = await build(GPT, frames=[QUIET] * 200)
    assert scribe.commits == 0
    assert order == []
    assert not scribe.transcripts


@in_a_loop
async def test_a_room_that_has_heard_only_quiet_refuses_to_close_a_turn():
    """The same guard, asked directly, and the reason it has to be more than a
    byte count.

    The participant's page streams continuously: 20 ms frames of quiet reach
    the scribe the whole time nobody is talking, so "this room has appended
    something" is true within 20 ms of any close and stays true. A commit on
    that buffer is a transcription model being handed several seconds of
    silence, and such a model does not return nothing — it invents, and the
    invention lands in the record as participant speech.
    """
    runner, room, scribe, order = await build(GPT, frames=[QUIET] * 10)
    assert scribe.audio_in, "the quiet did reach the scribe"
    assert await room.close_participant_turn() is False
    assert scribe.commits == 0


@in_a_loop
async def test_closing_the_same_turn_twice_commits_once():
    """Idempotent per turn. A second commit on one utterance is a duplicated
    participant turn, which nothing downstream can detect."""
    runner, room, scribe, order = await build(GPT, frames=utterance())
    assert scribe.commits == 1
    assert await room.close_participant_turn() is False
    assert scribe.commits == 1


@in_a_loop
async def test_a_turn_nobody_is_given_the_floor_for_is_still_transcribed():
    """The director may answer a participant turn with silence — no member is
    granted the floor, no member's buffer is committed. The participant still
    spoke, so the record must still say so."""
    session = FakeSession()
    ws = ScriptedWS(utterance())
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)

    gateway = Gateway()
    real = gr.RealtimeVoiceSession
    gr.RealtimeVoiceSession = gateway
    try:
        room = GroupRoom(runner._resolve_agents(),
                         instructions_for=lambda a: "x",
                         voice_for=lambda a: "", tools=[], model=GPT)
        await room.open()
    finally:
        gr.RealtimeVoiceSession = real

    # Every member's session has gone: there is nobody to give the floor to,
    # which is the shape _run_group_turn reports as group_turn_no_members.
    members = list(room.sessions.values())
    room.sessions.clear()
    runner.room = room
    runner.rt = members[0]

    async def noop():
        return None

    runner._run_group_turn = noop
    await runner._client_to_model()

    assert room.scribe.commits == 1
    assert all(m.commits == 0 for m in members)


@in_a_loop
async def test_an_interrupted_member_does_not_cost_the_participants_turn():
    """Barge-in: the participant talks over a character. The speaker's reply is
    cancelled and closed out, and the participant's own turn must still be
    closed when they stop."""
    agents = load_scenario(GROUP_SCENARIO, "p_test").cast
    runner, room, scribe, order = await build(
        GPT, frames=utterance(), floor_holder=agents[0].id,
    )
    assert runner.ws.frames("assistant_interrupted"), "the barge-in path ran"
    assert scribe.commits == 1
    assert order == [1]


@in_a_loop
async def test_a_scribe_whose_socket_is_gone_is_reported_not_swallowed():
    """A commit on a dead channel does not raise — _send counts a closed socket
    and drops the frame so one dead member cannot stop a room — so silence here
    would leave the runner believing it still had a participant transcript."""
    runner, room, scribe, order = await build(GPT, frames=[QUIET] * 10)
    await room.hear(LOUD)                       # something to close
    scribe.ws = None
    scribe.last_send_error = "ConnectionClosedError"

    assert await room.close_participant_turn() is False
    assert room.lost.get(SCRIBE_ID), "the lost channel was named"
    assert room.scribe is None
    assert room.scribe_commits == 0


@in_a_loop
async def test_a_segment_that_ends_mid_utterance_loses_nothing_already_closed():
    """Interaction boundaries do not close a participant turn, and must not.

    The runner learns where a turn ended from its own VAD, and an interaction
    boundary is not that moment: a buffer holding only the quiet between turns
    would be transcribed as words the participant never said. So a turn that
    HAS ended is already in the record before the boundary, and speech still in
    flight stays in the scribe's buffer and is closed by the turn_ended that
    follows it — on a kept room (S4's working session then its close) that is
    the same scribe, and the utterance arrives whole rather than in halves.
    """
    frames = utterance() + [LOUD] * 10          # a turn, then talking again
    runner, room, scribe, order = await build(GPT, frames=frames)
    assert scribe.commits == 1, "the finished turn is closed; the live one is not"
    assert scribe.heard > 0, "the unfinished utterance is still in the buffer"

    # The boundary itself commits nothing.
    await room.rebrief()
    assert scribe.commits == 1
    assert room.scribe_commits == 1


# --------------------------------------------------------------------------
# What closing the turn costs, and what it must not cost.
# --------------------------------------------------------------------------

@in_a_loop
async def test_the_scribes_discarded_reply_is_cancelled_once_per_reply():
    """A committed scribe is answered — both families accepted "Never speak"
    and then answered anyway — and _pump_scribe drops that reply.

    The cancel that stops the gateway generating the rest of it belongs once
    per reply, not once per frame of one. Unlatched it sent one per transcript
    delta, and every one after the first came back
    `response_cancel_not_active`: 16 of them across three participant turns,
    measured on gpt-realtime-2.1, filling a real channel's error stream with
    noise from a channel that is working. tools/encounter_health.py reads
    voice_error as degrading, so that is every group encounter reported
    degraded for a reply nobody was ever going to hear.
    """
    session = FakeSession()
    runner = rvs.RealtimeVoiceSessionRunner(session, ScriptedWS([]))
    rt = FakeRT()
    pump = asyncio.ensure_future(runner._pump_scribe(rt))

    for _ in range(5):                          # one reply, five frames
        rt.feed({"type": "agent_transcript_delta", "text": "..."})
        rt._responding = True                   # events() re-arms on every delta
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    assert rt.cancels == 1, "one cancel for one reply"

    rt.feed({"type": "response_done"})          # the reply ends
    await asyncio.sleep(0.05)
    rt.feed({"type": "agent_transcript_delta", "text": "..."})
    rt._responding = True
    await asyncio.sleep(0.05)
    assert rt.cancels == 2, "the next reply gets its own cancel"

    rt.end()
    await pump
    assert not session.store.of("voice_error")


@in_a_loop
async def test_a_late_whole_line_finds_nothing_to_cancel():
    """A whole-line `agent_transcript` after response.done has no reply behind
    it. Cancelling there is the error, not the cure."""
    session = FakeSession()
    runner = rvs.RealtimeVoiceSessionRunner(session, ScriptedWS([]))
    rt = FakeRT()
    pump = asyncio.ensure_future(runner._pump_scribe(rt))

    rt.feed({"type": "response_done"})
    await asyncio.sleep(0.02)
    rt.feed({"type": "agent_transcript", "text": " "})
    await asyncio.sleep(0.05)
    assert rt.cancels == 0

    rt.end()
    await pump
