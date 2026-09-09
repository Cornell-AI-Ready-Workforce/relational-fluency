"""Regression tests for the realtime voice runner's go-live blockers.

Every test here stands for a defect that failed SILENTLY: the encounter kept
running, the page kept looking alive, and the record came out internally
consistent and wrong. That is the class this study cannot absorb — a lost
encounter can be re-run, a corrupted one cannot be detected — so each test
asserts on what was WRITTEN, not on whether the code survived.

No network and no credentials. Gateway failures are simulated by raising the
exception types the real bridge raises: websockets.ConnectionClosedError from a
dropped socket, and the RuntimeError the gateway client surfaces for an HTTP
429 or a revoked key at connect time. Scenario specs are the real ones from
scenarios/v3, loaded through the real loader, because the shapes that matter —
S1A's two one-to-one scenes, S3A's group-then-series, S4A's kept room — are
scenario-authored and a synthetic cast would not exercise them.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosedError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import realtime_voice_session as rvs  # noqa: E402
from server import group_room as gr  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402


# A dropped gateway socket, raised by RealtimeVoiceSession._send once the
# reader has seen the close frame (voice/realtime.py leaves self.ws set).
def dropped_socket() -> Exception:
    return ConnectionClosedError(None, None)


# What a 429 or a revoked key looks like coming out of connect().
def refused_connect() -> Exception:
    return RuntimeError("HTTP 429 Too Many Requests")


# --------------------------------------------------------------------------
# Fakes, in the shapes the runner actually uses.
# --------------------------------------------------------------------------

class FakeRT:
    """A stand-in for RealtimeVoiceSession with a drivable event stream."""

    def __init__(self, instructions="", voice="Puck", tools=None):
        self.ws = object()
        self.voice = voice
        self.model = "fake-realtime"
        self.autofire_active = False
        self.pending_input = 0
        self.debug_log = []
        self.instructions = [instructions]
        self.closed = False
        self.audio_sent = b""
        self.fail_update = None
        self._responding = False
        self._q: asyncio.Queue = asyncio.Queue()

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

    async def update_instructions(self, instructions):
        if self.fail_update is not None:
            raise self.fail_update
        self.instructions.append(instructions)

    async def send_audio(self, pcm):
        self.audio_sent += pcm
        self.pending_input += len(pcm)

    async def commit_input(self):
        self.pending_input = 0

    async def commit_turn(self):
        self.pending_input = 0

    async def request_response(self):
        self._responding = True

    async def cancel_response(self):
        self._responding = False

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


class FakeStore:
    def __init__(self):
        self.events = []
        self.audio = {}
        self.user_audio = b""
        self.closed = False
        self.dropped_after_close = 0

    def event(self, type_, **fields):
        if self.closed:
            self.dropped_after_close += 1
            return
        self.events.append(dict(type=type_, **fields))

    def append_assistant_audio(self, pcm, agent_id=None):
        self.audio[agent_id] = self.audio.get(agent_id, b"") + pcm

    def append_user_audio(self, pcm):
        self.user_audio += pcm

    def types(self):
        return [e["type"] for e in self.events]

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeEngine:
    def __init__(self, agent):
        self.agent = agent

    def _system_prompt(self, branches, note, group=False):
        return f"SYSTEM PROMPT for {self.agent.id}"


class FakeDirector:
    model = "fake-director"

    def __init__(self, sequence=None):
        self.sequence = sequence or []

    async def route(self, history, text):
        return [{"agent_id": a} for a in self.sequence]


class FakeSession:
    def __init__(self, scenario_id):
        self.scenario = load_scenario(scenario_id, "p_test")
        self.is_group = self.scenario.mode == "group"
        self.engines = {a.id: FakeEngine(a) for a in self.scenario.cast}
        self.store = FakeStore()
        self.director = FakeDirector()
        self.triggered_branches = []
        self.shared_history = []
        self.steering_log = []
        self.broadcasts = []
        self.steer_calls = 0
        self.steer_delivered = []

    def append_user(self, text):
        self.shared_history.append({"speaker": "user", "text": text})

    def append_agent(self, agent_id, text):
        self.shared_history.append({"speaker": agent_id, "text": text})

    async def broadcast(self, payload):
        self.broadcasts.append(payload)

    async def auto_steer(self, *, delivered=None):
        # `delivered` is the runner's answer to "was the actor actually told?",
        # forwarded to set_knob for every shift this review makes. Recorded
        # rather than dropped so a test can assert on it. B42.
        self.steer_calls += 1
        self.steer_delivered.append(delivered)


class FakeWS:
    def __init__(self):
        self.json = []
        self.binary = []

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        self.binary.append(payload)

    def frames(self, type_):
        return [f for f in self.json if f.get("type") == type_]


def make_runner(scenario_id):
    session = FakeSession(scenario_id)
    ws = FakeWS()
    runner = rvs.RealtimeVoiceSessionRunner(session, ws)
    return runner, session, ws


class Gateway:
    """Hands out FakeRTs, and can be told to refuse the next connect."""

    def __init__(self, refuse=False):
        self.refuse = refuse
        self.made = []

    def __call__(self, instructions="", voice="Puck", tools=None):
        rt = FakeRT(instructions, voice, tools)
        if self.refuse:
            async def boom(*a, **k):
                raise refused_connect()
            rt.connect = boom
        self.made.append(rt)
        return rt


@pytest.fixture(autouse=True)
def _fast_and_quiet(monkeypatch):
    """Short waits: these tests assert on ordering, not on wall-clock patience."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.6")
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.1")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.05")


# --------------------------------------------------------------------------
# The character switch: B8 / B14 / B28, and the containment half of B31 / B34.
# --------------------------------------------------------------------------

def test_refused_switch_keeps_the_character_that_is_actually_speaking():
    """A refused connect must not make the runner, the UI or the record become
    a character that has no session. B8 / B14 / B28."""
    runner, session, ws = make_runner("S1A")
    riley = FakeRT()
    runner.rt = riley

    gateway = Gateway(refuse=True)
    rvs.RealtimeVoiceSession = gateway
    try:
        handled = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            _advance(runner)
        )
    finally:
        rvs.RealtimeVoiceSession = _REAL_RT
    assert handled is True          # handled, not "encounter over"

    # Identity, wire, banner and record all still name Riley.
    assert runner.agent_id == "riley"
    assert runner.agent.name == "Riley"
    assert runner.rt is riley
    assert riley.ws is not None
    assert not session.store.of("segment_start")
    assert not ws.frames("segment_start")

    # The advance itself is rolled back, so nothing written afterwards is
    # stamped with an interaction that never opened.
    assert runner.segment == 0

    # And the failure is on the record and on the participant's screen.
    aborted = session.store.of("segment_start_aborted")
    assert aborted and aborted[0]["wanted"] == "sam" and aborted[0]["kept"] == "riley"
    assert [e for e in session.store.of("voice_error") if e.get("where") == "enter"]
    assert ws.frames("error")


def test_refused_switch_does_not_misattribute_the_old_actors_audio():
    """The corruption this exists to stop: the previous actor's voice recorded
    into the NEXT character's WAV and transcript. B8 / B14 / B28."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        riley = FakeRT()
        runner.rt = riley
        gateway = Gateway(refuse=True)
        rvs.RealtimeVoiceSession = gateway
        try:
            await runner._advance_segment()
        finally:
            rvs.RealtimeVoiceSession = _REAL_RT

        # Riley keeps talking, because Riley's session is the one still open.
        riley.feed({"type": "agent_audio", "pcm": b"\x01\x02" * 80})
        riley.feed({"type": "agent_transcript_delta", "text": "I said what I said."})
        riley.feed({"type": "response_done"})
        riley.end()
        await runner._pump(riley)
        await asyncio.sleep(1.0)
        return session

    session = asyncio.run(scenario())
    assert set(session.store.audio) == {"riley"}
    turns = session.store.of("assistant_turn")
    assert turns and all(t["agent_id"] == "riley" for t in turns)


async def _advance(runner):
    return await runner._advance_segment()


_REAL_RT = rvs.RealtimeVoiceSession


# --------------------------------------------------------------------------
# The room open: B15 / B27 / B31 / B34.
# --------------------------------------------------------------------------

def test_failed_room_open_leaves_no_empty_room():
    """A room that fails to open must not be published. An empty non-None room
    passes every liveness check in the runner and dead-airs the encounter for
    its full remaining length. B15 / B27 / B31 / B34."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        room = gr.GroupRoom(
            runner._resolve_agents(),
            instructions_for=lambda a: "x",
            voice_for=lambda a: "Puck",
        )
        for a in runner._resolve_agents():
            room.sessions[a.id] = FakeRT()
        runner.room = room
        # A member session dropped, so the next boundary cannot keep the room.
        room.sessions.pop("priya")

        gateway = Gateway(refuse=True)
        gr.RealtimeVoiceSession = gateway
        try:
            handled = await runner._advance_segment()
        finally:
            gr.RealtimeVoiceSession = _REAL_GR_RT
        return runner, session, ws, handled

    runner, session, ws, handled = asyncio.run(scenario())
    assert handled is True
    assert runner.room is None, "a failed open must not publish a room"
    assert runner.segment == 0, "the advance must roll back"
    assert not session.store.of("segment_start")
    assert [e for e in session.store.of("voice_error") if e.get("where") == "open_room"]
    assert ws.frames("error"), "the participant must be told, not left in silence"
    assert runner._closed is True


_REAL_GR_RT = gr.RealtimeVoiceSession


def test_group_turn_gives_up_instead_of_burning_the_floor_timeout():
    """A failed floor grant must end the turn, not wait 45 s with `speaking`
    naming a member that is no longer in the room. B35."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        room = gr.GroupRoom(
            runner._resolve_agents(),
            instructions_for=lambda a: "x",
            voice_for=lambda a: "Puck",
        )
        for a in runner._resolve_agents():
            rt = FakeRT()

            async def boom(pcm, _rt=rt):
                raise ConnectionResetError("gateway dropped the member")
            rt.send_audio = boom
            room.sessions[a.id] = rt
        runner.room = room
        runner._last_user_text = "Priya, what do you think?"

        started = time.time()
        await asyncio.wait_for(runner._run_group_turn(), timeout=10)
        return runner, session, time.time() - started

    runner, session, elapsed = asyncio.run(scenario())
    assert elapsed < 5, "a failed grant must not wait out the 45 s turn timeout"
    assert runner.room.speaking is None
    assert session.store.of("floor_grant_failed")
    # Whatever beat was briefed is retracted, never left standing as coverage.
    fired = [e["trigger_id"] for e in session.store.of("trigger_fired")]
    retracted = [e["trigger_id"] for e in session.store.of("trigger_undelivered")]
    assert sorted(fired) == sorted(retracted)


# --------------------------------------------------------------------------
# Pumps that die quietly: B24 (member), B11 / B26 / B30 (scribe).
# --------------------------------------------------------------------------

def test_member_pump_survives_its_own_end_conversation():
    """end_conversation advances the encounter; it must not also mute the
    character that called it, which a KEPT room carries into the next
    interaction. B24."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        runner.room = gr.GroupRoom(
            runner._resolve_agents(),
            instructions_for=lambda a: "x", voice_for=lambda a: "Puck",
        )
        dan = runner._resolve_agents()[0]
        rt = FakeRT()
        runner.room.sessions[dan.id] = rt
        runner.room.speaking = dan.id

        async def no_advance():
            return None
        runner._advance_from_tool = no_advance

        pump = asyncio.ensure_future(runner._pump_member(dan, rt))
        rt.feed({"type": "tool_call", "name": "end_conversation"})
        await asyncio.sleep(0.1)
        alive = not pump.done() and dan.id in runner._member_turns
        # And it is still relaying: a reply after the tool call still lands.
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        await asyncio.sleep(0.1)
        pump.cancel()
        return alive, session

    alive, session = asyncio.run(scenario())
    assert alive, "the pump must keep relaying after end_conversation"
    assert session.store.audio.get("dan")


def test_kept_room_respawns_a_member_that_lost_its_pump():
    """Belt and braces for the same defect: a member with a live socket and no
    relay is mute for the whole next interaction while give_floor still reports
    success. B24."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        agents = runner._resolve_agents()
        runner.room = gr.GroupRoom(
            agents, instructions_for=lambda a: "x", voice_for=lambda a: "Puck",
        )
        for a in agents:
            runner.room.sessions[a.id] = FakeRT()
        # Two of the three have pumps; Dan's is gone.
        for a in agents[1:]:
            runner._member_turns[a.id] = ([], {})
        runner._respawn_member_pumps()
        await asyncio.sleep(0.05)
        # Read the registry here: asyncio.run cancels the pump on the way out,
        # and its finally unregisters, so a check after the loop would see the
        # teardown rather than the respawn.
        return sorted(runner._member_turns), session

    registered, session = asyncio.run(scenario())
    assert "dan" in registered
    assert [e["agent_id"] for e in session.store.of("member_pump_respawned")] == ["dan"]


def test_scribe_death_is_recorded_and_stops_the_record_asserting_speech():
    """The scribe is the only participant channel in a room. Its death used to
    be written nowhere, and every later turn then repeated the last utterance it
    managed to hear as though the participant had just said it. B11 / B26 / B30."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        agents = runner._resolve_agents()
        runner.room = gr.GroupRoom(
            agents, instructions_for=lambda a: "x", voice_for=lambda a: "Puck",
        )
        for a in agents:
            runner.room.sessions[a.id] = FakeRT()
        scribe = FakeRT()
        runner.room.scribe = scribe

        pump = asyncio.ensure_future(runner._pump_scribe(scribe))
        scribe.feed({"type": "user_transcript",
                     "text": "I would rather we settled this now."})
        await asyncio.sleep(0.05)
        # The socket drops: voice/realtime.py yields one error, then ends.
        scribe.feed({"type": "error", "message": "realtime connection lost: 1006"})
        scribe.end()
        await asyncio.wait_for(pump, timeout=2)

        await runner._finalize_member_inner(agents[0], "Then let us settle it.")
        return runner, session, ws

    runner, session, ws = asyncio.run(scenario())
    assert [e for e in session.store.of("voice_error") if e.get("where") == "scribe"]
    assert session.store.of("scribe_pump_ended")
    assert ws.frames("error")
    pair = session.store.of("steering_pair")[-1]
    assert pair["participant"] is None
    assert pair["participant_channel"] == "lost"


def test_scribe_teardown_with_the_room_is_not_reported_as_a_loss():
    """_close_room cancelling the pump is deliberate, not a lost channel."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        runner.room = gr.GroupRoom(
            runner._resolve_agents(),
            instructions_for=lambda a: "x", voice_for=lambda a: "Puck",
        )
        scribe = FakeRT()
        pump = asyncio.ensure_future(runner._pump_scribe(scribe))
        await asyncio.sleep(0.05)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        return runner, session

    runner, session = asyncio.run(scenario())
    assert not session.store.of("scribe_pump_ended")
    assert runner._scribe_lost is False


# --------------------------------------------------------------------------
# Turn boundaries: B25, B33, B37, B13, B18.
# --------------------------------------------------------------------------

def test_grace_wait_settles_instead_of_stopping_at_the_first_delta():
    """A transcript that streams in after response.done is one turn, not a
    one-word turn plus a tail glued onto the next one. B25."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        rt.feed({"type": "response_done"})
        for chunk in ["Great, ", "we're aligned. ", "I'll write this up ",
                      "and present it myself."]:
            await asyncio.sleep(0.08)
            rt.feed({"type": "agent_transcript_delta", "text": chunk})
        await asyncio.sleep(1.2)
        rt.end()
        await asyncio.wait_for(pump, timeout=2)
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1, f"one reply must be one turn, got {[t['text'] for t in turns]}"
    assert turns[0]["text"] == (
        "Great, we're aligned. I'll write this up and present it myself."
    )
    assert turns[0]["transcript_missing"] is False


def test_gateway_end_of_transcript_event_is_taken_as_the_whole_line():
    """The bridge's own response.*_transcript.done carries the complete text and
    was being dropped on the floor. B25."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "agent_transcript_delta", "text": "Great,"})
        rt.feed({"type": "response_done"})
        await asyncio.sleep(0.05)
        rt.feed({"type": "agent_transcript",
                 "text": "Great, we're aligned. I'll write this up."})
        await asyncio.sleep(1.2)
        rt.end()
        await asyncio.wait_for(pump, timeout=2)
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1
    assert turns[0]["text"] == "Great, we're aligned. I'll write this up."


def test_a_second_reply_is_its_own_turn_and_keeps_its_own_words():
    """A reply that starts during the previous turn's grace wait must not have
    its transcript recorded as the previous turn's line, and must not be
    silently discarded. B33."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        # Reply A: audio, then response.done with no transcript at all.
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        rt.feed({"type": "response_done"})
        await asyncio.sleep(0.15)
        # Reply B arrives while A is still inside its grace wait.
        rt.feed({"type": "agent_audio", "pcm": b"\x02\x03" * 40})
        rt.feed({"type": "agent_transcript_delta",
                 "text": "That is not what I said."})
        rt.feed({"type": "response_done"})
        await asyncio.sleep(1.5)
        rt.end()
        await asyncio.wait_for(pump, timeout=2)
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("assistant_turn")
    assert len(turns) == 2, "neither reply may be swallowed"
    texts = [t["text"] for t in turns]
    assert "" in texts, "the reply with no transcript is recorded as missing"
    assert "That is not what I said." in texts
    a_turn = [t for t in turns if not t["text"]][0]
    assert a_turn["transcript_missing"] is True
    assert session.store.of("transcript_missing")


def test_direction_is_paired_with_the_turn_it_was_issued_for():
    """The steering trail must not pair turn N's line with turn N+1's stage
    direction while turn N+1 records itself as unsteered. B37."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        first = {"turn": 0, "segment": 0, "interaction": "i1",
                 "agent_id": "riley", "stage_direction": "FIRST",
                 "trigger_id": "t1"}
        runner._pending_direction = dict(first)
        runner._agent_text = ["The credit was mine."]
        runner._speaking = True
        buf, stop, settled = runner._take_turn_buffer()
        task = asyncio.ensure_future(runner._finalize_turn(
            "riley", runner.agent, rt, buf, runner._take_direction(), stop=stop,
        ))
        # A new brief lands while the first turn is still settling.
        await asyncio.sleep(0.1)
        runner._pending_direction = {"turn": 1, "segment": 0, "interaction": "i1",
                                     "agent_id": "riley",
                                     "stage_direction": "SECOND",
                                     "trigger_id": "t2"}
        await asyncio.wait_for(task, timeout=3)
        return session

    session = asyncio.run(scenario())
    pair = session.store.of("steering_pair")[-1]
    assert pair["direction"]["stage_direction"] == "FIRST"
    assert pair["actor"]["text"] == "The credit was mine."


def test_empty_member_reply_does_not_hand_its_beat_to_the_next_speaker():
    """A direction written for one character must never be paired with another
    character's line: the rater packet keys on the actor and drops the
    direction's own agent id, so the swap is invisible downstream. B13."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        agents = runner._resolve_agents()
        dan, priya = agents[0], agents[1]
        runner.room = gr.GroupRoom(
            agents, instructions_for=lambda a: "x", voice_for=lambda a: "Puck",
        )
        for a in agents:
            runner.room.sessions[a.id] = FakeRT()
        runner.room.speaking = dan.id
        runner._pending_direction = {
            "turn": 0, "segment": 0, "interaction": "i1", "agent_id": dan.id,
            "stage_direction": "Restate Chris's idea as your own.",
            "trigger_id": "t2_idea_relabelled",
            "esci": ["supportive"],
        }
        # Dan says nothing at all.
        await runner._finalize_member_async(dan, [], False)
        # Priya, a follow-up speaker, takes the floor with no re-brief.
        await runner._finalize_member_inner(priya, "I did have a point.")
        return runner, session

    runner, session = asyncio.run(scenario())
    assert runner._pending_direction is None
    assert session.store.of("stage_direction_unperformed")
    pair = session.store.of("steering_pair")[-1]
    assert pair["actor"]["agent_id"] == "priya"
    assert pair["direction"] is None, "Priya's line must not carry Dan's beat"


def test_a_mismatched_direction_is_recorded_rather_than_mispaired():
    """Defence in depth for the same rule. B13 / B37."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        agents = runner._resolve_agents()
        runner._pending_direction = {
            "agent_id": "dan", "stage_direction": "Dan's beat",
            "trigger_id": "t2_idea_relabelled",
        }
        await runner._finalize_member_inner(agents[1], "Something else.")
        return runner, session

    runner, session = asyncio.run(scenario())
    unmatched = session.store.of("steering_pair_unmatched")
    assert unmatched and unmatched[0]["direction_agent_id"] == "dan"
    assert unmatched[0]["actor_agent_id"] == "priya"
    assert session.store.of("steering_pair")[-1]["direction"] is None
    # And the slot is left standing for the character it was written for.
    assert runner._pending_direction is not None


def test_the_last_turn_is_written_before_the_store_goes_away():
    """A finalize task sleeps out its grace BEFORE it writes the turn, and the
    store is closed the moment run() returns. The turn this cost was always the
    last one — the one a rater most needs. B18."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt

        async def client_to_model():
            await asyncio.sleep(0.05)     # the participant hangs up

        async def watchdog():
            await asyncio.sleep(30)

        async def model_to_client():
            await runner._pump(rt)

        runner._client_to_model = client_to_model
        runner._silence_watchdog = watchdog
        runner._model_to_client = model_to_client
        rvs.RealtimeVoiceSession = lambda **k: rt
        try:
            # The closing reply: audio plays, and its transcript is one the
            # gateway never delivers. The finalize task must sleep out the whole
            # grace before it can write the turn and mark the gap — and the
            # participant hangs up during that sleep.
            rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
            rt.feed({"type": "response_done"})
            await asyncio.wait_for(runner.run(), timeout=10)
        finally:
            rvs.RealtimeVoiceSession = _REAL_RT
        # app.py drops the session synchronously the moment run() returns,
        # which closes events.jsonl. Anything the runner writes after this
        # point is discarded in silence.
        session.store.closed = True
        await asyncio.sleep(1.5)
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("assistant_turn")
    assert turns, "the last agent turn was dropped while its audio was kept"
    assert turns[-1]["transcript_missing"] is True
    assert session.store.of("steering_pair"), "its stage direction paired with nothing"
    assert session.store.audio.get("riley"), "the audio is in the WAV either way"
    assert session.store.dropped_after_close == 0


# --------------------------------------------------------------------------
# Turn boundaries, round two: the settle rule (B25 / R3), the settling
# window (B33 / R2), and the teardown wait (R5).
# --------------------------------------------------------------------------

def test_a_transcript_that_pauses_is_not_declared_settled(monkeypatch):
    """Quiescence must be a duration proportional to the grace, not two polls.
    Two 0.15 s polls is ~0.30 s whatever TRANSCRIPT_GRACE_SECONDS says, so a
    transcript pausing half a second — well inside a 3 s budget — was recorded
    as its first fragment with transcript_missing False and the rest of the line
    appended to a buffer already read and cleared, i.e. lost outright. B25 / R3.
    """
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "2")

    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        rt.feed({"type": "response_done"})
        # 0.4 s between chunks: longer than the old two-poll window, far
        # shorter than the grace the deployment actually asked for.
        for chunk in ["Great, ", "we're aligned. ", "I'll write this up."]:
            await asyncio.sleep(0.4)
            rt.feed({"type": "agent_transcript_delta", "text": chunk})
        await asyncio.sleep(1.6)
        rt.end()
        await asyncio.wait_for(pump, timeout=3)
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1, f"one reply must be one turn, got {[t['text'] for t in turns]}"
    assert turns[0]["text"] == "Great, we're aligned. I'll write this up."
    assert turns[0]["transcript_missing"] is False
    # Nothing was left behind in a buffer nobody reads again.
    assert not session.store.of("transcript_late")


def test_a_late_whole_line_transcript_still_replaces_the_fragment(monkeypatch):
    """The gateway's authoritative whole-line event arrives as
    `buf[:] = [text]`, which does not change len(buf) — so a settle test that
    watched only the length could not see it, returned on the deltas' silence,
    and threw the complete line away. B25 / R3."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "2")

    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "agent_transcript_delta", "text": "Great,"})
        rt.feed({"type": "response_done"})
        await asyncio.sleep(0.6)     # well past the old ~0.30 s settle
        rt.feed({"type": "agent_transcript",
                 "text": "Great, we're aligned. I'll write this up."})
        await asyncio.sleep(2.0)
        rt.end()
        await asyncio.wait_for(pump, timeout=3)
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1
    assert turns[0]["text"] == "Great, we're aligned. I'll write this up."


def test_text_that_lands_after_the_turn_is_written_is_recorded_not_dropped():
    """Text can still arrive between the finalizer reading the buffer and the
    turn being written. That buffer is never read again, so the text is gone —
    which is exactly the class of loss this file exists to stop. It has to leave
    a mark. R3."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        runner._agent_text = ["The credit was mine."]
        runner._speaking = True
        buf, stop, settled = runner._take_turn_buffer()

        # A delta arrives during the writes that follow the buffer read. The
        # broadcast is the first await after buf.clear(), so this is the real
        # window, injected deterministically rather than raced for.
        async def broadcast(payload):
            buf.append(" And you know it.")
            session.broadcasts.append(payload)
        session.broadcast = broadcast

        await asyncio.wait_for(runner._finalize_turn(
            "riley", runner.agent, rt, buf, None, stop=stop,
        ), timeout=3)
        return session

    session = asyncio.run(scenario())
    assert session.store.of("assistant_turn")[0]["text"] == "The credit was mine."
    late = session.store.of("transcript_late")
    assert late, "the tail vanished with nothing in the record marking the loss"
    assert late[0]["text"] == " And you know it."
    assert late[0]["recorded_text"] == "The credit was mine."


def test_a_new_replys_opening_delta_is_not_recorded_as_the_previous_turns_line():
    """Deltas arrive before audio, so a reply starting while the previous turn
    is still settling had its opening sentence recorded as the PREVIOUS turn's
    line, kept only its own tail, and neither turn was flagged. Audio is the one
    event that can only belong to a live reply, so it is what ends the settling
    window and takes the provisional text back. B33 / R2."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        # Reply A: audio, then response.done with no transcript at all — the
        # ordinary transcript_missing case, which holds the full grace.
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        rt.feed({"type": "response_done"})
        await asyncio.sleep(0.15)
        # Reply B opens with text, as the gateway normally does.
        rt.feed({"type": "agent_transcript_delta",
                 "text": "That is not what I said."})
        rt.feed({"type": "agent_audio", "pcm": b"\x02\x03" * 40})
        rt.feed({"type": "agent_transcript_delta", "text": " At all."})
        rt.feed({"type": "response_done"})
        await asyncio.sleep(1.5)
        rt.end()
        await asyncio.wait_for(pump, timeout=3)
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("assistant_turn")
    assert len(turns) == 2, "neither reply may be swallowed"
    # Reply A said nothing that was ever transcribed, and says so.
    assert turns[0]["text"] == ""
    assert turns[0]["transcript_missing"] is True
    # Reply B keeps its OWN opening sentence, not just its tail.
    assert turns[1]["text"] == "That is not what I said. At all."
    assert turns[1]["transcript_missing"] is False
    # And they are written in the order they happened. encounter_record sorts
    # the transcript by write time, so an inverted pair inverts record.json.
    assert [t["text"] for t in turns] == ["", "That is not what I said. At all."]
    assert session.store.of("transcript_reattributed"), (
        "text moved between turns must not move silently"
    )


def test_a_settling_turn_stops_waiting_once_the_next_reply_starts(monkeypatch):
    """The steal window was the whole grace whenever the settling turn's buffer
    was empty. A turn that will never get a transcript must stop waiting the
    moment a new reply is demonstrably speaking, or every second of the grace is
    a second in which it can consume that reply's text. B33."""
    grace = 2.0
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", str(grace))

    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        # Reply A: audio, response.done, and no transcript ever.
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        rt.feed({"type": "response_done"})
        await asyncio.sleep(0.1)
        opened = time.time()
        # Reply B starts speaking. Reply A's transcript is not coming.
        rt.feed({"type": "agent_audio", "pcm": b"\x02\x03" * 40})
        for _ in range(40):
            if session.store.of("assistant_turn"):
                break
            await asyncio.sleep(0.05)
        waited = time.time() - opened
        rt.feed({"type": "response_done"})
        await asyncio.sleep(0.3)
        rt.end()
        await asyncio.wait_for(pump, timeout=3)
        return waited, session

    waited, session = asyncio.run(scenario())
    assert waited < grace / 2, (
        f"the settling turn held its grace for {waited:.2f}s after the next "
        f"reply had already started speaking"
    )
    assert session.store.of("assistant_turn")[0]["transcript_missing"] is True


def test_member_finalize_waits_the_grace_the_teardown_wait_is_bounded_by():
    """run()'s teardown waits TRANSCRIPT_GRACE_SECONDS + 1 s for the settling
    turns. _finalize_member_async waited a hardcoded 2.5 s, so any deployment
    lowering the grace bounded the wait BELOW the task it was waiting for and
    dropped a room's last turn again. R5."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        agents = runner._resolve_agents()
        runner.room = gr.GroupRoom(
            agents, instructions_for=lambda a: "x", voice_for=lambda a: "Puck",
        )
        for a in agents:
            runner.room.sessions[a.id] = FakeRT()
        runner.room.speaking = agents[0].id
        started = time.time()
        # Announced, and no transcript ever arrives: the full grace is spent.
        await asyncio.wait_for(
            runner._finalize_member_async(agents[0], [], True), timeout=5
        )
        return time.time() - started, session

    elapsed, session = asyncio.run(scenario())
    # The fixture sets TRANSCRIPT_GRACE_SECONDS=0.6; run()'s bound is 1.6 s.
    grace = float(os.environ["TRANSCRIPT_GRACE_SECONDS"])
    assert elapsed < grace + 1.0, (
        f"the member finalize spent {elapsed:.2f}s against a teardown bound of "
        f"{grace + 1.0:.2f}s, so run() closes the store out from under it"
    )
    assert session.store.of("assistant_turn")


def test_teardown_closes_the_gateway_even_when_run_is_cancelled():
    """The finalize wait is the first await in run()'s finally. A cancellation
    delivered there used to skip _close_room() and rt.close() entirely, leaking
    every gateway socket the encounter held. R5."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt

        async def client_to_model():
            await asyncio.sleep(0.05)

        async def watchdog():
            await asyncio.sleep(30)

        async def model_to_client():
            await runner._pump(rt)

        runner._client_to_model = client_to_model
        runner._silence_watchdog = watchdog
        runner._model_to_client = model_to_client
        rvs.RealtimeVoiceSession = lambda **k: rt
        try:
            # A reply whose transcript never comes, so run() is inside the
            # finalize wait when the cancellation lands.
            rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
            rt.feed({"type": "response_done"})
            task = asyncio.ensure_future(runner.run())
            await asyncio.sleep(0.3)
            task.cancel()
            cancelled = False
            try:
                await task
            except asyncio.CancelledError:
                cancelled = True
        finally:
            rvs.RealtimeVoiceSession = _REAL_RT
        return cancelled, rt

    cancelled, rt = asyncio.run(scenario())
    assert rt.closed, "the gateway socket was leaked when run() was cancelled"
    assert cancelled, "the cancellation must still reach the caller"


def test_a_member_reply_delivered_only_as_a_whole_line_is_announced():
    """The client drops assistant text that arrives before assistant_started, so
    a member reply delivered only as a whole-line transcript — no deltas, no
    audio — appeared on screen as the character saying nothing, while the record
    had the line. _finalize_member_async also skips its grace wait when the turn
    was never announced. R6."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        dan = runner._resolve_agents()[0]
        runner.room = gr.GroupRoom(
            runner._resolve_agents(),
            instructions_for=lambda a: "x", voice_for=lambda a: "Puck",
        )
        rt = FakeRT()
        runner.room.sessions[dan.id] = rt
        runner.room.speaking = dan.id
        pump = asyncio.ensure_future(runner._pump_member(dan, rt))
        rt.feed({"type": "agent_transcript",
                 "text": "I think Chris's framing is the right one."})
        rt.feed({"type": "response_done"})
        await asyncio.sleep(1.2)
        pump.cancel()
        return session, ws

    session, ws = asyncio.run(scenario())
    started = ws.frames("assistant_started")
    assert started and started[0]["agent_id"] == "dan", (
        "the character spoke and the page was never told who was speaking"
    )
    turns = session.store.of("assistant_turn")
    assert turns and turns[0]["text"] == "I think Chris's framing is the right one."


# --------------------------------------------------------------------------
# Beats: B9 / B29 / B36 / B39, and the steer race B10.
# --------------------------------------------------------------------------

def test_a_beat_whose_brief_never_left_is_not_recorded_as_fired():
    """trigger_fired is coverage, and coverage decides whether an encounter is
    scoreable. It must not be claimed for a direction that never left the
    process. B9 / B29 / B36 / B39, 1:1 path."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        rt.fail_update = dropped_socket()
        runner.rt = rt
        await runner._brief_next_beat(probing=False)
        return runner, session

    runner, session = asyncio.run(scenario())
    assert not session.store.of("trigger_fired")
    assert not session.store.of("stage_direction")
    assert runner._trigger_idx == 0, "the beat stays available to retry"
    assert runner._fired == []
    assert runner._pending_direction is None
    failed = session.store.of("trigger_brief_failed")
    assert failed and failed[0]["trigger_id"] == "t1_retaliation_fork"


def test_a_group_beat_whose_brief_never_left_is_not_recorded_as_fired():
    """The same rule on the room path, where the callers' retraction is gated on
    the floor grant and so never runs when the brief itself throws. B9 / B39."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        agents = runner._resolve_agents()
        runner.room = gr.GroupRoom(
            agents, instructions_for=lambda a: "x", voice_for=lambda a: "Puck",
        )
        for a in agents:
            rt = FakeRT()
            rt.fail_update = dropped_socket()
            runner.room.sessions[a.id] = rt
        await runner._brief_member(agents[0].id)
        return runner, session

    runner, session = asyncio.run(scenario())
    assert not session.store.of("trigger_fired")
    assert runner._trigger_idx == 0
    assert runner._fired == []
    assert session.store.of("trigger_brief_failed")


def test_a_beat_that_is_delivered_is_recorded_as_fired():
    """The counterpart: the netting rule must not be starved of legitimate
    firings. B9 / B36."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        runner.rt = FakeRT()
        await runner._brief_next_beat(probing=False)
        return runner, session

    runner, session = asyncio.run(scenario())
    fired = session.store.of("trigger_fired")
    assert [e["trigger_id"] for e in fired] == ["t1_retaliation_fork"]
    assert runner._trigger_idx == 1
    assert session.store.of("stage_direction")
    assert "DIRECTOR NOTE" in runner.rt.instructions[-1]


def test_steering_does_not_withdraw_a_direction_mid_reply():
    """A steering review can take eleven seconds, so it routinely lands inside
    the next reply. Landing it there replaced the planted beat's stage direction
    — the one the record says was delivered — with an unsteered brief. B10."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        await runner._brief_next_beat(probing=False)
        briefed = rt.instructions[-1]
        rt._responding = True             # a reply is in flight

        async def auto_steer(*, delivered=None):
            session.steering_log.append({"note": "shifted"})
        session.auto_steer = auto_steer
        await runner._steer()
        return runner, session, rt, briefed

    runner, session, rt, briefed = asyncio.run(scenario())
    assert rt.instructions[-1] is briefed, "the live brief must not be replaced"
    assert session.store.of("steer_deferred")


def test_steering_between_turns_keeps_an_unperformed_direction():
    """Deferred is not the only case: a steer that DOES land between turns must
    carry a still-pending direction rather than silently un-briefing it. B10."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        await runner._brief_next_beat(probing=False)

        async def auto_steer(*, delivered=None):
            session.steering_log.append({"note": "shifted"})
        session.auto_steer = auto_steer
        await runner._steer()
        return runner, rt

    runner, rt = asyncio.run(scenario())
    assert "DIRECTOR NOTE" in rt.instructions[-1]
    assert runner._pending_direction["stage_direction"] in rt.instructions[-1]


# --------------------------------------------------------------------------
# The echo guard: B32.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("participant", [
    "So what you are saying is the decision is already made and nobody asked us.",
    "I want to make sure I have this right, there is no budget for it this cycle.",
    "We should push it back until the team has actually seen the numbers.",
])
def test_paraphrasing_the_character_back_is_not_deleted(participant):
    """Mirroring the counterpart is the behaviour several ESCI items exist to
    observe. Set containment scored it as playback echo and deleted the turn
    from the record while the participant's voice stayed in the WAV. B32."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        runner._recent_agent_texts = [(
            time.time(),
            "riley",
            "The decision is already made, and there is no budget this cycle, "
            "so we are not pushing it back.",
        )]
        await runner._record_user_turn(participant)
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("user_turn")
    assert turns and turns[0]["text"] == participant
    assert not turns[0].get("echo_suspected")
    assert not session.store.of("echo_dropped")


@pytest.mark.parametrize("participant", [
    # Verbatim.
    "Right, no, not at the moment, I think we have covered the main points "
    "for today.",
    # One word inserted mid-utterance — what a transcriber does to speaker
    # playback more often than not. The contiguous-block test scored this 0.53
    # and let the character's own sentence through as participant speech.
    "Right, no, not at the moment, I um think we have covered the main points "
    "for today.",
    # One word dropped mid-utterance.
    "Right, no, not at the moment, I think we covered the main points for today.",
    # A leading filler word.
    "Uh right, no, not at the moment, I think we have covered the main points "
    "for today.",
])
def test_real_playback_echo_is_flagged_even_when_the_transcriber_slips(participant):
    """Playback echo comes back re-transcribed, so it is rarely word-perfect. A
    detected echo is written ONLY as echo_dropped: never as a user_turn, because
    encounter_record keeps t/role/text and drops echo_suspected, so a user_turn
    reaches the rater packet as the participant saying the counterpart's words.
    B32 / R1."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        line = ("Right, no, not at the moment, I think we have covered the "
                "main points for today.")
        runner._recent_agent_texts = [(time.time(), "riley", line)]
        await runner._record_user_turn(participant)
        return runner, session

    runner, session = asyncio.run(scenario())
    dropped = session.store.of("echo_dropped")
    assert dropped, "an echo of the character's own line was recorded as speech"
    assert dropped[0]["matches"] == "riley"
    assert dropped[0]["text"] == participant, "the audit trail keeps the words"
    assert not session.store.of("user_turn"), (
        "a user_turn carrying the character's own sentence reaches the rater "
        "packet as participant speech; echo_suspected does not survive "
        "encounter_record"
    )
    assert runner._last_user_text == "", "an echo must not steer routing"
    assert session.shared_history == [], "an echo must not enter shared history"


@pytest.mark.parametrize("participant", [
    # Quotes most of the line back, but only a PART of it, and frames it as a
    # question. Scores 0.92 against itself and 0.63 against the line, so the
    # one-directional test suppressed it. Checking understanding is an ESCI
    # behaviour; suppressing it is the deletion B32 exists to stop.
    "Wait, the decision is already made and there is no budget this cycle?",
    "You just said there is no budget this cycle so we are not pushing it back.",
    "You are saying there is no budget this cycle and we are not pushing it "
    "back, correct?",
])
def test_quoting_part_of_the_line_back_is_not_taken_for_echo(participant):
    """The other direction of the same rule: an echo replays the WHOLE
    utterance, a participant checking their understanding quotes a piece of it
    and adds their own frame. B32."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        runner._recent_agent_texts = [(
            time.time(),
            "riley",
            "The decision is already made, and there is no budget this cycle, "
            "so we are not pushing it back.",
        )]
        await runner._record_user_turn(participant)
        return session

    session = asyncio.run(scenario())
    assert not session.store.of("echo_dropped")
    turns = session.store.of("user_turn")
    assert turns and turns[0]["text"] == participant


def test_the_echo_window_does_not_reach_back_across_the_conversation():
    """Playback echo arrives inside one buffer, not five turns later. B32."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        line = ("Right, no, not at the moment, I think we have covered the "
                "main points for today.")
        runner._recent_agent_texts = [
            (time.time() - 60, "riley", line)
        ]
        await runner._record_user_turn(line)
        return session

    session = asyncio.run(scenario())
    assert not session.store.of("echo_dropped")
    assert session.store.of("user_turn")[0]["text"]


# --------------------------------------------------------------------------
# The fixture wave: every scenario shape in the 27-encounter wave, driven
# through every interaction boundary with the gateway refusing.
# --------------------------------------------------------------------------

def _fixture_scenarios():
    root = os.getenv("RF_FIXTURE_DIR") or (
        Path(os.getenv("TEMP", "/tmp")) / "rf-fixture-not-present"
    )
    sessions = Path(root) / "sessions"
    if not sessions.is_dir():
        return []
    out = []
    for d in sorted(sessions.iterdir()):
        rec = d / "record.json"
        if not rec.is_file():
            continue
        try:
            sid = json.loads(rec.read_text(encoding="utf-8")).get("scenario")
        except ValueError:
            continue
        if sid:
            out.append((d.name, sid))
    return out


@pytest.mark.parametrize("encounter,scenario_id", _fixture_scenarios())
def test_every_wave_scenario_holds_the_identity_invariant(encounter, scenario_id):
    """Across every encounter in the wave: at every interaction boundary, with
    the gateway refusing, the runner must never end up naming a character it
    does not have a live session for, must never announce one, and must never
    leave the advance half-applied. The three boundary shapes in the bank are
    all exercised — a fresh 1:1 session (S1/S3 series), a rebuilt room
    (S3/S4), and the same-character continuation that re-briefs a live socket
    (S2) — because each reaches the gateway a different way. B8 / B14 / B28 /
    B15 / B27 / B31 / B34."""
    async def scenario():
        runner, session, ws = make_runner(scenario_id)
        runner.rt = FakeRT()
        if runner.is_group():
            runner.room = gr.GroupRoom(
                runner._resolve_agents(),
                instructions_for=lambda a: "x", voice_for=lambda a: "Puck",
            )
            for a in runner._resolve_agents():
                runner.room.sessions[a.id] = FakeRT()
            runner.rt = runner.room.session_for(runner.agent_id)

        results = []
        for _ in range(len(runner.interactions) + 2):
            before_id = runner.agent_id
            before_seg = runner.segment
            before_series = runner._series_idx
            n_banners = len(session.store.of("segment_start"))
            # Make the boundary fail however THIS scenario reaches the gateway:
            # a refused connect for a fresh session or a rebuilt room, and a
            # dropped socket for the continuation branch's re-brief.
            runner._member_turns.clear()
            if runner.room is not None:
                runner.room.sessions.pop(
                    next(iter(runner.room.sessions), None), None
                )
            if runner.rt is not None:
                runner.rt.fail_update = dropped_socket()
            gw = Gateway(refuse=True)
            rvs.RealtimeVoiceSession = gw
            gr.RealtimeVoiceSession = gw
            try:
                more = await runner._advance_segment()
            finally:
                rvs.RealtimeVoiceSession = _REAL_RT
                gr.RealtimeVoiceSession = _REAL_GR_RT
            banners = session.store.of("segment_start")
            results.append({
                "more": more,
                "announced": len(banners) > n_banners,
                "banner_id": banners[-1]["agent_id"] if banners else None,
                "agent_id": runner.agent_id,
                "kept_id": runner.agent_id == before_id,
                "kept_seg": runner.segment == before_seg,
                "kept_series": runner._series_idx == before_series,
                "live_rt": runner.rt is not None and runner.rt.ws is not None,
                "no_empty_room": runner.room is None or bool(runner.room.sessions),
            })
            if not more or runner._closed:
                break
        return results, session, ws

    results, session, ws = asyncio.run(scenario())
    assert results, "the runner must at least attempt one boundary"
    for r in results:
        assert r["no_empty_room"], "an empty room was published"
        if r["announced"]:
            # A boundary that WAS announced must be one the runner can play.
            assert r["banner_id"] == r["agent_id"]
            assert r["live_rt"], "segment_start announced a character with no session"
        else:
            # A boundary that failed leaves nothing half-applied.
            assert r["kept_id"], "the runner adopted a character it could not open"
            assert r["kept_seg"] and r["kept_series"], \
                "the advance was not rolled back"
    # At least one boundary was made to fail, and a failed boundary is never
    # silent: it is on the record and on the participant's screen.
    assert any(not r["announced"] for r in results)
    assert session.store.of("segment_start_aborted")
    assert ws.frames("error")


# --------------------------------------------------------------------------
# Round three: P5 / P6 / P7 / P9 / R15 / B42.
# --------------------------------------------------------------------------

def _room_with(runner, agent):
    room = gr.GroupRoom(
        runner._resolve_agents(),
        instructions_for=lambda a: "x", voice_for=lambda a: "Puck",
    )
    runner.room = room
    rt = FakeRT()
    room.sessions[agent.id] = rt
    room.speaking = agent.id
    return rt


def test_a_reply_the_gateway_abandons_is_one_turn_not_two(monkeypatch):
    """P5. voice/realtime.py closes an abandoned reply with a synthetic
    `interrupted` response_done, and the gateway's own response.done for the
    same reply can still arrive behind it - the bridge cannot dedupe the pair,
    because the synthetic one carries no response id. _pump_member had no latch
    against the second, so two finalizes raced on one shared buffer and the
    reply was written as TWO assistant_turns: the real line, then the same reply
    again as text="" flagged transcript_missing.

    The false flag is the damage. transcript_missing tells a rater "the audio
    played but its text was lost", so hanging it on a turn that never happened
    teaches raters to distrust the one signal that protects them - and
    verify_record's 'every agent turn transcribed' check fails on an encounter
    whose transcript is in fact complete."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "1.5")

    async def scenario():
        runner, session, ws = make_runner("S4A")
        dan = runner._resolve_agents()[0]
        rt = _room_with(runner, dan)
        pump = asyncio.ensure_future(runner._pump_member(dan, rt))
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        rt.feed({"type": "agent_transcript_delta", "text": "Turn one words."})
        # Exactly what the bridge emits for an `error` frame mid-reply.
        rt.feed({"type": "response_done", "interrupted": True})
        rt.feed({"type": "error", "message": "gateway said no"})
        rt.feed({"type": "response_done"})          # ... and then the real one
        await asyncio.sleep(2.5)
        pump.cancel()
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1, f"one reply was recorded as {len(turns)} turns"
    assert turns[0]["text"] == "Turn one words."
    assert turns[0]["transcript_missing"] is False
    assert not session.store.of("transcript_missing"), (
        "a turn that never happened was flagged transcript_missing"
    )
    # One reply, one scored moment.
    assert len(session.store.of("steering_pair")) == 1
    # And the loss itself is still on the record.
    assert session.store.of("voice_error")


def test_a_late_whole_line_does_not_reopen_an_abandoned_reply(monkeypatch):
    """The same defect by its other route. The gateway's whole-line transcript
    can land BETWEEN the synthetic response_done and the real one, and
    _pump_member announces on it — which used to clear the finalize latch as
    though a new reply had begun, so the real response_done behind it spawned a
    second finalize on the same buffer and wrote the reply twice again, the
    second copy empty and flagged transcript_missing. An empty buffer is what
    makes a whole line a new reply; a full one makes it late text for the turn
    that is closing. P5."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "1.5")

    async def scenario():
        runner, session, ws = make_runner("S4A")
        dan = runner._resolve_agents()[0]
        rt = _room_with(runner, dan)
        pump = asyncio.ensure_future(runner._pump_member(dan, rt))
        for ev in (
            {"type": "agent_audio", "pcm": b"\x00\x01" * 40},
            {"type": "agent_transcript_delta", "text": "Turn one words."},
            {"type": "response_done", "interrupted": True},
            {"type": "agent_transcript", "text": "Turn one words, all of them."},
            {"type": "response_done"},
        ):
            rt.feed(ev)
            await asyncio.sleep(0.05)
        await asyncio.sleep(2.5)
        pump.cancel()
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1, f"one reply was recorded as {len(turns)} turns"
    # The authoritative whole line wins, and it is not thrown away by the fix.
    assert turns[0]["text"] == "Turn one words, all of them."
    assert not session.store.of("transcript_missing")


def test_a_second_response_done_still_releases_the_floor():
    """The swallow above must never be the reason a room goes quiet: a duplicate
    that is dropped still has to release the floor, exactly as the barge-in
    swallow beside it does. P5."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        dan = runner._resolve_agents()[0]
        rt = _room_with(runner, dan)
        pump = asyncio.ensure_future(runner._pump_member(dan, rt))
        rt.feed({"type": "agent_transcript_delta", "text": "Something."})
        rt.feed({"type": "response_done", "interrupted": True})
        await asyncio.sleep(0.05)
        runner._response_done.clear()               # as a waiting turn would
        rt.feed({"type": "response_done"})
        await asyncio.sleep(0.3)
        held = runner._response_done.is_set()
        pump.cancel()
        return held

    assert asyncio.run(scenario()), "the swallowed duplicate stranded the floor"


def test_a_new_reply_is_not_swallowed_by_the_previous_ones_latch():
    """A latch that outlives its own reply is the failure mode this shape has
    (see `barged_in`): the NEXT reply's response_done would be swallowed and its
    turn never written. P5."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        dan = runner._resolve_agents()[0]
        rt = _room_with(runner, dan)
        pump = asyncio.ensure_future(runner._pump_member(dan, rt))
        for line in ("First reply.", "Second reply.", "Third reply."):
            rt.feed({"type": "agent_transcript_delta", "text": line})
            rt.feed({"type": "agent_transcript", "text": line})
            rt.feed({"type": "response_done"})
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.5)
        pump.cancel()
        return session

    session = asyncio.run(scenario())
    assert [t["text"] for t in session.store.of("assistant_turn")] == [
        "First reply.", "Second reply.", "Third reply.",
    ]


def test_a_truncated_turn_is_recorded_as_truncated(monkeypatch):
    """R15. The bridge sets `interrupted` on the response_done it synthesises
    for a reply the gateway abandoned mid-sentence. Both spawn sites dropped it,
    so the record claimed a complete delivery and a rater comparing the stage
    direction to the line had no way to tell a cut-off line from a bad one."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "1.5")

    async def one_to_one():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        rt.feed({"type": "agent_transcript_delta", "text": "So you are letting it "})
        rt.feed({"type": "response_done", "interrupted": True})
        await asyncio.sleep(2.0)
        pump.cancel()
        return session

    async def in_a_room():
        runner, session, ws = make_runner("S4A")
        dan = runner._resolve_agents()[0]
        rt = _room_with(runner, dan)
        pump = asyncio.ensure_future(runner._pump_member(dan, rt))
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        rt.feed({"type": "agent_transcript_delta", "text": "So you are letting it "})
        rt.feed({"type": "response_done", "interrupted": True})
        await asyncio.sleep(2.0)
        pump.cancel()
        return session

    for session in (asyncio.run(one_to_one()), asyncio.run(in_a_room())):
        turn = session.store.of("assistant_turn")[0]
        assert turn["interrupted"] is True, "a cut-off turn reads as a complete one"
        pair = session.store.of("steering_pair")[0]
        assert pair["actor"]["interrupted"] is True
        # The words that were spoken are still there: flagging is not dropping.
        assert turn["text"] == "So you are letting it"


def test_an_ordinary_turn_is_not_flagged_interrupted():
    """The counterpart claim. A flag that is set on turns that were not
    truncated is worth as little as one that is never set at all. R15."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "agent_transcript_delta", "text": "I finished my sentence."})
        rt.feed({"type": "agent_transcript", "text": "I finished my sentence."})
        rt.feed({"type": "response_done"})
        await asyncio.sleep(0.8)
        pump.cancel()
        return session

    turn = asyncio.run(scenario()).store.of("assistant_turn")[0]
    assert turn["interrupted"] is False
    assert turn["transcript_missing"] is False


def test_a_completed_transcript_is_not_waited_out(monkeypatch):
    """P6. The quiescence rule inferred the end of the transcript stream from a
    second of silence, and charged that second to EVERY agent turn - including
    the ordinary one where the gateway had already SAID the line was complete.
    assistant_done goes out only after the wait, and the page keeps its 'still
    speaking' cue up until then, so the participant sat through a second of
    nothing after every single reply."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "3")

    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "agent_transcript_delta",
                 "text": "That is not how I remember it."})
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        # The gateway's own end-of-transcript event: the stream is over.
        rt.feed({"type": "agent_transcript", "text": "That is not how I remember it."})
        rt.feed({"type": "response_done"})
        started = time.time()
        while not ws.frames("assistant_done") and time.time() - started < 5:
            await asyncio.sleep(0.02)
        elapsed = time.time() - started
        pump.cancel()
        return elapsed, session

    elapsed, session = asyncio.run(scenario())
    assert elapsed < 0.5, (
        f"{elapsed:.2f}s of dead air after a reply whose transcript was complete"
    )
    assert session.store.of("assistant_turn")[0]["text"] == \
        "That is not how I remember it."


def test_a_room_turn_with_a_complete_transcript_releases_the_floor_at_once(monkeypatch):
    """Same rule in a room, where it compounds: the floor and the next speaker
    wait on the same signal, so the delay was charged per speaker per turn. P6."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "3")

    async def scenario():
        runner, session, ws = make_runner("S4A")
        dan = runner._resolve_agents()[0]
        rt = _room_with(runner, dan)
        runner._response_done.clear()
        pump = asyncio.ensure_future(runner._pump_member(dan, rt))
        rt.feed({"type": "agent_transcript_delta", "text": "I read it the same way."})
        rt.feed({"type": "agent_transcript", "text": "I read it the same way."})
        rt.feed({"type": "response_done"})
        started = time.time()
        while not runner._response_done.is_set() and time.time() - started < 5:
            await asyncio.sleep(0.02)
        elapsed = time.time() - started
        pump.cancel()
        return elapsed, session

    elapsed, session = asyncio.run(scenario())
    assert elapsed < 0.5, f"the room's floor was held {elapsed:.2f}s for nothing"
    assert session.store.of("assistant_turn")[0]["text"] == "I read it the same way."


def test_a_still_streaming_transcript_is_still_waited_out(monkeypatch):
    """The correctness the quiet window bought, kept. Without the gateway's
    end-of-transcript event nothing has said the stream is over, so the wait
    must still run - inferring 'complete' from a buffer that merely holds the
    first delta is B25/R3, which recorded 'Great,' and threw the line away. P6."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "3")

    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "agent_transcript_delta", "text": "Great,"})
        rt.feed({"type": "response_done"})
        await asyncio.sleep(0.7)      # far past any short settle window
        rt.feed({"type": "agent_transcript_delta", "text": " we are aligned."})
        await asyncio.sleep(2.0)
        pump.cancel()
        return session

    session = asyncio.run(scenario())
    turns = session.store.of("assistant_turn")
    assert len(turns) == 1
    assert turns[0]["text"] == "Great, we are aligned."


def test_a_missing_transcript_still_costs_the_whole_grace(monkeypatch):
    """And a turn whose text never arrives is still held for the full budget and
    still marked, rather than being called complete early. P6."""
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "1")

    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        rt.feed({"type": "agent_audio", "pcm": b"\x00\x01" * 40})
        rt.feed({"type": "response_done"})
        started = time.time()
        while not ws.frames("assistant_done") and time.time() - started < 5:
            await asyncio.sleep(0.02)
        elapsed = time.time() - started
        pump.cancel()
        return elapsed, session

    elapsed, session = asyncio.run(scenario())
    assert elapsed >= 0.9, "an empty buffer was called settled"
    assert session.store.of("transcript_missing")
    assert session.store.of("assistant_turn")[0]["transcript_missing"] is True


def test_two_callers_racing_for_a_beat_fire_it_once():
    """P7. _brief_next_beat reads the beat, sends it, and only then spends it,
    and it has two callers in different tasks: _client_to_model on the
    participant's turn end and _silence_watchdog on the probe. With only the
    session.update serialised, both could hold the same trigger and both fire
    it - two trigger_fired rows at indices N and N+1 for one beat, coverage
    inflated by one, and beat N+1 never briefed, never delivered and never
    scored, with nothing in the record to show it had been skipped."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        # S1A's SECOND interaction is the one with several planted beats, which
        # is what makes "the second caller sees the first one spent" observable
        # as two different beats rather than merely as nothing firing twice.
        runner.segment = 1
        rt = FakeRT()
        runner.rt = rt

        # A brief that is genuinely on the wire when the second caller arrives.
        real_update = rt.update_instructions

        async def slow_update(instructions):
            await asyncio.sleep(0.2)
            await real_update(instructions)
        rt.update_instructions = slow_update

        await asyncio.gather(
            runner._brief_next_beat(probing=False),
            runner._brief_next_beat(probing=True),
        )
        return runner, session

    runner, session = asyncio.run(scenario())
    fired = session.store.of("trigger_fired")
    assert len(fired) == 2, "both callers had a beat to spend"
    # Two DIFFERENT beats, at consecutive indices, each briefed before it fired.
    assert [f["index"] for f in fired] == [0, 1]
    assert len({f["trigger_id"] for f in fired}) == 2, (
        "the same beat was fired twice: coverage inflated and the next beat skipped"
    )
    assert runner._fired == [f["trigger_id"] for f in fired]
    assert runner._trigger_idx == 2
    # Every fired beat was actually sent, and the last brief standing is the
    # last one fired.
    assert len(session.store.of("stage_direction")) == 2
    assert runner._pending_direction["trigger_id"] == fired[-1]["trigger_id"]


def test_a_beat_a_racing_caller_already_spent_is_not_fired_again():
    """The same race with only ONE beat left: the second caller must find the
    beat gone, not fire it a second time. P7."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        # Spend every beat but the last.
        runner._trigger_idx = len(runner._triggers()) - 1

        real_update = rt.update_instructions

        async def slow_update(instructions):
            await asyncio.sleep(0.2)
            await real_update(instructions)
        rt.update_instructions = slow_update

        await asyncio.gather(
            runner._brief_next_beat(probing=False),
            runner._brief_next_beat(probing=True),
        )
        return runner, session

    runner, session = asyncio.run(scenario())
    fired = session.store.of("trigger_fired")
    assert len(fired) == 1, f"one beat remained and {len(fired)} were claimed"
    assert runner._trigger_idx == len(runner._triggers())


def test_a_run_of_corrupt_audio_frames_does_not_bury_the_participant():
    """P9. The corrupt-frame guard reports once per chunk, which is right: a row
    per occurrence is how an analyst sees how much audio was lost. But _pump
    forwarded every error to the participant's screen, and audio deltas arrive
    every few tens of milliseconds, so one bad stream filled the transcript with
    dozens of identical 'Something went wrong' lines mid-conversation."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        for _ in range(25):
            rt.feed({
                "type": "error", "transient": True,
                "message": "discarded a corrupt audio frame from the gateway: bad",
            })
        await asyncio.sleep(0.4)
        pump.cancel()
        return session, ws

    session, ws = asyncio.run(scenario())
    assert len(session.store.of("voice_error")) == 25, (
        "the per-chunk record of how much audio was lost must not be collapsed"
    )
    assert len(ws.frames("error")) == 1, (
        f"{len(ws.frames('error'))} error banners for one bad stream"
    )


def test_a_real_error_is_never_hidden_behind_a_transient_one():
    """The throttle must not swallow a fault the session did NOT survive: a
    genuine terminal error is what tells the participant the encounter has
    stopped. P9."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        for _ in range(5):
            rt.feed({"type": "error", "transient": True, "message": "corrupt frame"})
        rt.feed({"type": "error", "message": "realtime connection lost: 1006"})
        await asyncio.sleep(0.4)
        pump.cancel()
        return ws

    frames = asyncio.run(scenario()).frames("error")
    assert len(frames) == 2
    assert "connection lost" in frames[-1]["message"]


def test_the_transient_notice_is_rearmed_for_the_next_reply():
    """One notice per reply, not one per encounter: a second corrupt stream
    later in the conversation is a new fault and the participant is told
    again. P9."""
    async def scenario():
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        pump = asyncio.ensure_future(runner._pump(rt))
        for _ in range(2):
            for _ in range(4):
                rt.feed({"type": "error", "transient": True, "message": "corrupt frame"})
            rt.feed({"type": "response_done"})     # reply boundary
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.3)
        pump.cancel()
        return ws

    assert len(asyncio.run(scenario()).frames("error")) == 2


def test_a_group_steer_records_that_it_reached_nobody():
    """B42. In a room _steer() re-briefs nobody - a blanket re-brief would have
    to reach every member at once and a mid-stream session.update mutes this
    bridge - so an auto gear shift made on a turn with no beat pending reaches
    the actor turns later, or never. It was recorded exactly like a shift that
    had landed, in S3 and S4, half the study, so the steering log read as a
    stimulus history when it was a list of intentions."""
    async def scenario():
        runner, session, ws = make_runner("S4A")
        await runner._steer()
        return session

    session = asyncio.run(scenario())
    assert session.steer_delivered == [False], (
        "a room shift was recorded without saying the actor had not been told"
    )


def test_a_one_to_one_steer_says_which_way_it_went():
    """The 1:1 half. Nothing can know at knob-set time whether the re-brief will
    go out (a review can take eleven seconds and routinely lands mid-reply), so
    the shift is written undetermined and the event right after it says what
    happened: steer_delivered when the brief left, steer_deferred when it was
    held back. B42."""
    async def run_one(responding):
        runner, session, ws = make_runner("S1A")
        rt = FakeRT()
        runner.rt = rt
        rt._responding = responding

        async def auto_steer(*, delivered=None):
            session.steering_log.append({"note": "shifted"})
            session.steer_delivered.append(delivered)
        session.auto_steer = auto_steer
        await runner._steer()
        return session

    landed = asyncio.run(run_one(False))
    assert landed.steer_delivered == [None], "1:1 delivery cannot be known yet"
    assert landed.store.of("steer_delivered"), "a shift that landed said nothing"
    assert not landed.store.of("steer_deferred")

    held = asyncio.run(run_one(True))
    assert held.steer_delivered == [None]
    assert held.store.of("steer_deferred")
    assert not held.store.of("steer_delivered")
