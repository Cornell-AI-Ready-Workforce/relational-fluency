"""A group room on a model of its own, and a per-turn direction that arrives.

Two defects, both only visible in the configuration the study would use to
decide whether it can move families: the room asked for one model while the
process stays on another.

1. THE ROOM'S MODEL NEVER REACHED THE WIRE. `GroupRoom(model=...)` decided
   which capability row the room read — which voices its characters were cast
   into, whether its floor was real, whether the participant was transcribed at
   all — and decided nothing whatsoever about the socket. Every session took
   RealtimeVoiceSession's own default, REALTIME_MODEL. Asked for
   gpt-realtime-2.1 with the process default left alone, the room cast three
   characters into `alloy`, `ash` and `ballad` and then opened three
   nto.gemini-live-2.5-flash sockets, where connect() refuses each of those
   voices by name: a group encounter that dies at the top, with a participant
   sitting there. The second half is quieter and worse. The room's default was
   frozen at import (`from .voice.realtime import MODEL`) while the runner
   reads the same attribute at call time, deliberately, so a model resolved
   after this module was imported gave a room on the gemini row inside a
   process on the gpt one — `floor_is_real` False, `close_participant_turn`
   returning on its first line, and an encounter with a perfect agent
   transcript and not one word the participant said.

2. NOTHING CARRIED THE DIRECTOR'S PER-TURN INTENT INTO A ROOM. The director
   composes one direction per speaker per turn; `_run_group_turn` read
   `agent_id` out of each routed entry and dropped the rest, and `_speak_as` —
   the only function that ever appended an intent to a brief — has no callers.
   The beat and persona re-briefs already went out over the one route that
   works; the per-turn direction went nowhere. These tests hold the wiring at
   the unit level: which character it reaches, which it does not, that a
   planted beat is never displaced by it, and that on the configured family the
   room sends nothing at all for one while the record still says so.

What is NOT asserted here is that gpt-realtime-2.1 OBEYS the direction. No test
double can be evidence of that, and the echo trap makes a naive check worse
than none: gpt-realtime-2.1 will repeat an instruction like "say only BANANA"
back verbatim and then decline to follow it, so a delivery check written that
way reports failure on the family where delivery works. That half was measured
live against api.ai.it.cornell.edu, by observed behaviour on a direction the
model has no reason to refuse, and is reported with the change.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import group_room as gr                      # noqa: E402
from server import realtime_voice_session as rvs         # noqa: E402
from server.scenarios import load_scenario               # noqa: E402
from server.voice import realtime as rt_mod              # noqa: E402
from server.voice.realtime import capabilities_for       # noqa: E402

GEMINI = "nto.gemini-live-2.5-flash"
GPT = "gpt-realtime-2.1"

# What the runner's own _voice_for hands out with the process default left
# alone: the gemini roster, by cast position. Every one of them is refused by
# the gpt family, which is what made the mismatch fatal rather than cosmetic.
SHIPPED_VOICES = ("Puck", "Charon", "Kore")


class Agent:
    def __init__(self, aid, name):
        self.id, self.name = aid, name


CAST = [Agent("dan", "Dan"), Agent("priya", "Priya"), Agent("mel", "Mel")]


# --------------------------------------------------------------------------
# 1. The room's model, all the way to the socket.
# --------------------------------------------------------------------------

class FakeWire:
    """The one thing a real RealtimeVoiceSession needs that we will not give
    it: a websocket. Everything upstream of the send — the voice check, the
    URL, the per-family session dict — is the real code, because that is where
    the defect lives."""

    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


@pytest.fixture
def real_sessions(monkeypatch):
    """Open real sessions, against no gateway. Returns [(url, FakeWire), ...],
    one entry per socket opened — a room opens several on the same URL."""
    wires = []

    async def fake_connect(url, **kw):
        wire = FakeWire()
        wires.append((url, wire))
        return wire

    monkeypatch.setattr(rt_mod.websockets, "connect", fake_connect)
    monkeypatch.setattr(rt_mod, "gateway_api_key", lambda: "test-key")
    return wires


def _open_room(model=None, voice_for=None):
    room = gr.GroupRoom(
        CAST,
        instructions_for=lambda a: f"You are {a.name}.",
        voice_for=voice_for or (lambda a: SHIPPED_VOICES[CAST.index(a)]),
        tools=[],
        model=model,
    )
    asyncio.run(room.open())
    return room


def test_a_room_asked_for_gpt_opens_gpt_sockets(real_sessions):
    """The defect itself, in the configuration that exposes it: the model set
    for the room, the process default left alone."""
    assert rt_mod.MODEL == GEMINI, (
        "this test is about a room whose model differs from the process's; "
        f"the process is on {rt_mod.MODEL}"
    )
    room = _open_room(model=GPT)

    opened = dict(room.sessions)
    opened["scribe"] = room.scribe
    for channel, rt in opened.items():
        assert rt is not None, f"{channel} never opened"
        assert rt.model == GPT, (
            f"{channel} was opened on {rt.model}: the room read the gpt row "
            "and then handed its characters to a session on the other family"
        )
    # And the socket agrees, which is the half a `model` attribute alone would
    # not prove: the URL carries the model the gateway routes on.
    urls = [url for url, _ in real_sessions]
    assert all(GPT in url for url in urls), urls
    assert len(urls) == len(CAST) + 1, "one socket per character, plus scribe"


def test_the_voice_a_character_is_cast_in_is_legal_on_the_socket_it_gets(
        real_sessions):
    """Why the mismatch was fatal rather than cosmetic.

    The room casts from the row it read; connect() checks against the model the
    session will actually open on. With the two disagreeing, every character in
    the room is refused by name — `voice 'alloy' is not accepted by the
    gemini-live family` — and GroupRoom.open() tears the room down and
    re-raises. That is the whole group interaction failing at its first line.
    """
    room = _open_room(model=GPT)
    caps = capabilities_for(GPT)
    voices = [rt.voice for rt in room.sessions.values()]
    assert all(caps.accepts_voice(v) for v in voices), voices
    assert caps.accepts_voice(room.scribe.voice)
    # The cast keeps three distinguishable voices rather than collapsing onto
    # the family default, which is the room's existing substitution rule and
    # must survive the model now being honoured.
    assert len(set(voices)) == len(CAST), voices
    assert set(room.voice_substitutions) == {a.id for a in CAST}


def test_the_session_dict_is_built_from_the_rooms_family(real_sessions):
    """The keys that decide whether an encounter has any data in it.

    `turn_detection: null` is what makes the floor real on gpt, and
    `input_audio_transcription` is the participant channel — without it that
    family returns no participant transcript at all. Both are chosen from the
    session's own model, so a session opened on the wrong one is not merely
    mislabelled: it is a differently configured encounter.
    """
    room = _open_room(model=GPT)
    payload = room.sessions["dan"]._session_payload()
    assert payload["turn_detection"] is None
    assert payload["input_audio_transcription"] == {"model": "whisper-1"}
    # And the frame that actually went out says the same.
    _, wire = real_sessions[0]
    (frame,) = [f for f in wire.sent if f.get("type") == "session.update"]
    assert frame["session"]["turn_detection"] is None
    assert frame["session"]["voice"] in capabilities_for(GPT).voices


def test_a_room_follows_a_model_resolved_after_this_module_was_imported(
        real_sessions, monkeypatch):
    """The quiet half. The runner reads REALTIME_MODEL off the bridge module at
    call time — its own docstring says that is deliberate — and the room used to
    bind it at import. A model resolved later therefore gave a room reading the
    gemini row inside a process on the gpt one, which is not a labelling
    problem: `floor_is_real` False means close_participant_turn returns on its
    first line, and the encounter records not one word the participant said."""
    monkeypatch.setattr(rt_mod, "MODEL", GPT)
    room = _open_room(voice_for=lambda a: "")   # let each family default

    assert all(rt.model == GPT for rt in room.sessions.values())
    assert room.model == GPT
    assert room.caps.family == "gpt-realtime"
    assert room.floor_is_real is True, (
        "the room read the other family's row, so it would never close the "
        "participant's turn on the scribe"
    )
    assert rvs.realtime_model() == room.model, (
        "the runner and the room must not be able to disagree about which "
        "model this encounter is on"
    )


def test_the_configured_default_is_left_exactly_where_it_was(real_sessions):
    """The guard on both fixes. With nothing overridden, a room is on the
    configured model, on the gemini row, with the floor a filter and steering
    inert — i.e. what a study encounter does today."""
    room = _open_room()
    assert room.model == GEMINI == rt_mod.MODEL
    assert all(rt.model == GEMINI for rt in room.sessions.values())
    assert room.caps.family == "gemini-live"
    assert room.floor_is_real is False
    assert room.steering_is_real is False
    assert all(GEMINI in url for url, _ in real_sessions)


def test_steering_is_real_is_read_from_the_one_table():
    """Per-family behaviour has one home, REALTIME_FAMILIES, and this is a
    reading of the `honours_session_update` column rather than an opinion of
    its own. It is the question `_direct_member` asks before it sends
    anything."""
    for model in (GEMINI, GPT):
        room = gr.GroupRoom(CAST, instructions_for=lambda a: "x",
                            voice_for=lambda a: "", tools=[], model=model)
        assert room.steering_is_real is capabilities_for(
            model).honours_session_update
    unknown = gr.GroupRoom(CAST, instructions_for=lambda a: "x",
                           voice_for=lambda a: "", tools=[],
                           model="some.model.nobody.has.tried")
    assert unknown.steering_is_real is False, (
        "a family the table does not cover is not a family known to carry a "
        "direction"
    )


# --------------------------------------------------------------------------
# 2. The director's per-turn direction, into the room.
# --------------------------------------------------------------------------

class MemberRT:
    """A room member's session, in the shape the runner uses one.

    `updates` is the whole point: every mid-session session.update this member
    was sent, which is the one and only route a direction takes on this bridge.
    """

    def __init__(self, voice="Puck", model=GEMINI):
        self.ws = object()
        self.voice = voice
        self.model = model
        self.instructions = ""
        self.updates = []
        self.autofire_active = False
        self.pending_input = 0
        self.send_failures = 0
        self.last_send_error = ""
        self.debug_log = []
        self._responding = False

    @property
    def responding(self):
        return self._responding

    def clear_response_state(self):
        self._responding = False

    async def connect(self, **kw):
        return None

    async def close(self):
        self.ws = None

    async def update_instructions(self, instructions):
        self.instructions = instructions
        self.updates.append(instructions)
        return bool(capabilities_for(self.model).honours_session_update)

    async def send_audio(self, pcm):
        self.pending_input += len(pcm)

    async def commit_input(self):
        self.pending_input = 0

    async def request_response(self):
        self._responding = True

    async def cancel_response(self):
        self._responding = False


class FakeStore:
    def __init__(self):
        self.events = []

    def event(self, type_, **fields):
        self.events.append(dict(type=type_, **fields))

    def append_assistant_audio(self, pcm, agent_id=None):
        pass

    def append_user_audio(self, pcm):
        pass

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeEngine:
    def __init__(self, agent):
        self.agent = agent

    def _system_prompt(self, branches, note, group=False):
        return f"You are {self.agent.name}."


class ScriptedDirector:
    """Returns exactly the routed entries a test wants to see delivered."""

    model = "fake-director"

    def __init__(self, entries):
        self.entries = entries
        self.calls = 0

    async def route(self, history, text):
        self.calls += 1
        return [dict(e) for e in self.entries]


class FakeSession:
    def __init__(self, scenario_id, director):
        self.scenario = load_scenario(scenario_id, "p_test")
        self.is_group = self.scenario.mode == "group"
        self.engines = {a.id: FakeEngine(a) for a in self.scenario.cast}
        self.store = FakeStore()
        self.director = director
        self.triggered_branches = []
        self.shared_history = []
        self.steering_log = []
        self.broadcasts = []

    def append_user(self, text):
        self.shared_history.append({"speaker": "user", "text": text})

    def append_agent(self, agent_id, text):
        self.shared_history.append({"speaker": agent_id, "text": text})

    async def broadcast(self, payload):
        self.broadcasts.append(payload)

    async def auto_steer(self, *, delivered=None):
        pass


class FakeWS:
    def __init__(self):
        self.json = []

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        pass


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setenv("ROUTE_TRANSCRIPT_WAIT", "0.05")
    monkeypatch.setenv("AUTOFIRE_WAIT", "0.05")
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0.1")


def _runner_with_room(model, entries, *, drop=()):
    """A group runner whose room is on `model`, with the director scripted.

    `drop` names members whose session is gone, so a routed speaker can be
    unavailable the way a dropped socket makes them.
    """
    session = FakeSession("S4A", ScriptedDirector(entries))
    runner = rvs.RealtimeVoiceSessionRunner(session, FakeWS())
    room = gr.GroupRoom(
        runner._resolve_agents(),
        instructions_for=lambda a: runner._instructions_for(a),
        voice_for=lambda a: "",
        tools=[],
        model=model,
    )
    voices = capabilities_for(model).voices
    for i, a in enumerate(runner._resolve_agents()):
        if a.id in drop:
            continue
        room.sessions[a.id] = MemberRT(voice=voices[i], model=model)

    granted = room.give_floor

    async def give_floor(agent_id):
        rt = await granted(agent_id)
        runner._response_done.set()
        return rt
    room.give_floor = give_floor

    async def nothing():
        return None
    runner._advance_when_spent = nothing
    runner.room = room
    # An agent has already spoken, so route() cannot take its opener fast path.
    session.append_agent("dan", "So that is the plan.")
    runner._last_user_text = "I want to come back to the timeline."
    return runner, session, room


def _directions_in(rt):
    """Every direction this member was actually told, verbatim."""
    marker = "DIRECTOR NOTE"
    return [u.split(marker, 1)[1] for u in rt.updates if marker in u]


def test_a_per_turn_direction_reaches_the_character_it_names(monkeypatch):
    """The gap itself. The director writes one direction per speaker, every
    turn, and in a room nothing carried it: `agent_id` was read out of each
    routed entry and everything else dropped."""
    to_priya = "Ask what the deadline actually was, and do not let it slide."
    runner, session, room = _runner_with_room(
        GPT, [{"agent_id": "priya", "intent": to_priya}],
    )
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))

    told = _directions_in(room.sessions["priya"])
    assert told, "the character who was routed and directed was told nothing"
    assert to_priya in told[0]
    # And on the record, as its own kind of direction: no planted beat was
    # spent for it, so a rater can tell it from an ESCI-scored one.
    (row,) = [r for r in session.store.of("stage_direction")
              if r.get("source") == "director_intent"]
    assert row["agent_id"] == "priya"
    assert row["stage_direction"] == to_priya
    assert row["trigger_id"] is None
    assert row["delivered"] is True and row["acked"] is True


def test_it_reaches_that_character_and_not_the_others(monkeypatch):
    """A room is several actors on several sockets. A direction addressed to one
    of them and delivered to all of them would not be steering, it would be the
    scene being played three times over."""
    to_priya = "Ask what the deadline actually was."
    to_chris = "Stay out of it until you are asked."
    runner, session, room = _runner_with_room(
        GPT,
        [{"agent_id": "priya", "intent": to_priya},
         {"agent_id": "chris", "intent": to_chris}],
    )
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))

    priya_told = "\n".join(_directions_in(room.sessions["priya"]))
    chris_told = "\n".join(_directions_in(room.sessions["chris"]))
    assert to_priya in priya_told and to_chris not in priya_told
    assert to_chris in chris_told and to_priya not in chris_told
    # Dan was not routed this turn and must hear nothing addressed to anyone.
    dan_told = "\n".join(_directions_in(room.sessions["dan"]))
    assert to_priya not in dan_told and to_chris not in dan_told


def test_a_direction_follows_the_character_and_not_the_position():
    """The floor does not always go to routed_seq[0].

    The anti-dominance filter skips a candidate who just spoke, so the second
    name in the director's sequence can be the one that actually takes the
    floor — and the direction written for the first must not travel with it. A
    note written for Priya, performed by Chris, is a direction the instrument
    never staged, recorded as though it had been. It is the rule the planted
    beats have had since _trigger_agent, held here for the per-turn kind."""
    to_priya = "Ask what the deadline actually was."
    to_chris = "Say plainly that the date has already slipped twice."
    runner, session, room = _runner_with_room(
        GPT,
        [{"agent_id": "priya", "intent": to_priya},
         {"agent_id": "chris", "intent": to_chris}],
    )
    # Priya spoke last turn, so the anti-dominance rule hands the floor to
    # Chris, second in the director's sequence.
    runner._last_group_speaker = "priya"
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))

    (route,) = session.store.of("director_route")
    assert route["speakers"][0] == "chris", (
        "the test is only meaningful if the floor went to the second name"
    )
    chris_told = "\n".join(_directions_in(room.sessions["chris"]))
    assert to_chris in chris_told
    for aid, rt in room.sessions.items():
        assert to_priya not in "\n".join(_directions_in(rt)), (
            f"{aid} was handed a direction written for priya, who never spoke"
        )


def test_a_planted_beat_is_never_displaced_by_a_per_turn_direction():
    """A beat is the scored independent variable and it arrives by this same
    route. Two directions in one brief would leave a rater unable to say which
    one the reply answered — and the second session.update would replace the
    first, so the beat would be the half that was lost."""
    to_dan = "Push the schedule, briskly."
    runner, session, room = _runner_with_room(
        GPT, [{"agent_id": "dan", "intent": to_dan}],
    )
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))

    told = "\n".join(_directions_in(room.sessions["dan"]))
    assert told, "S4A's first beat belongs to dan and should have been briefed"
    fired = session.store.of("trigger_fired")
    assert fired and fired[0]["trigger_id"] == "t1_priya_interrupted"
    assert to_dan not in told, "the per-turn intent displaced the scored beat"
    (yielded,) = session.store.of("director_intent_yielded")
    assert yielded["agent_id"] == "dan" and yielded["intent"] == to_dan
    assert yielded["reason"] == "planted_beat_briefed"


def test_a_direction_whose_speaker_never_got_the_floor_is_retracted():
    """The pairing discipline the planted beats already have.

    A pending direction is paired by _finalize_member with whichever turn
    finalises next. Left standing after a failed grant it would attach this
    turn's note to somebody else's line, and a rater comparing a direction to
    the reply it produced would be reading a pairing that never happened."""
    to_priya = "Ask what the deadline actually was."
    runner, session, room = _runner_with_room(
        GPT, [{"agent_id": "priya", "intent": to_priya}],
    )

    async def no_floor(agent_id):
        room.speaking = agent_id
        runner._response_done.set()
        return None
    room.give_floor = no_floor
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))

    assert session.store.of("floor_grant_failed")
    assert [r for r in session.store.of("stage_direction")
            if r.get("source") == "director_intent"], (
        "the direction did go out, which is what has to be retracted"
    )
    (dropped,) = session.store.of("director_intent_undelivered")
    assert dropped["agent_id"] == "priya" and dropped["intent"] == to_priya
    assert runner._pending_direction is None, (
        "a direction nobody spoke was left to be paired with the next turn"
    )


def test_on_the_configured_model_the_room_sends_nothing_for_a_direction():
    """The Gemini path, unchanged, on purpose.

    A mid-session session.update is inert there — three frames, zero acks, an
    actor that went on ignoring the direction — so a room on that family sends
    no frame for one at all. Not because the direction does not matter, but
    because the only thing worse than a direction that does not arrive is a
    session.update landing on a member mid-reply, which on that family is how a
    character goes silent for the rest of the encounter. The route is chosen so
    that the beat is deferred (S4A's beats belong to dan) and the only
    session.update this turn could produce would be the direction's.
    """
    to_priya = "Ask what the deadline actually was."
    runner, session, room = _runner_with_room(
        GEMINI, [{"agent_id": "priya", "intent": to_priya}],
    )
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))

    assert session.store.of("trigger_deferred"), (
        "the test is only isolating the direction if no beat was briefed"
    )
    for aid, rt in room.sessions.items():
        assert rt.updates == [], (
            f"{aid} was sent a session.update on the family where one is inert"
        )


def test_and_says_so_rather_than_reporting_a_delivery_it_did_not_make():
    """Not sending is not the same as not reporting. A silent skip would make
    the configured model look exactly like a model that delivers."""
    to_priya = "Ask what the deadline actually was."
    runner, session, room = _runner_with_room(
        GEMINI, [{"agent_id": "priya", "intent": to_priya}],
    )
    asyncio.run(asyncio.wait_for(runner._run_group_turn(), timeout=10))

    (row,) = [r for r in session.store.of("stage_direction")
              if r.get("source") == "director_intent"]
    assert row["stage_direction"] == to_priya
    assert row["delivered"] is False and row["acked"] is False
    (unacked,) = session.store.of("steer_unacked")
    assert unacked["model"] == GEMINI, (
        "the row an analyst reads to find out which family the encounter was "
        "unsteered on must name the model the ROOM was on"
    )
    assert unacked["agent_id"] == "priya"
    # And onto the researcher's live strip, which is the only symptom of an
    # unsteered encounter that is visible while it is still running.
    frames = [b for b in session.broadcasts
              if b.get("kind") == "steer_unacked"]
    assert frames and frames[0]["severity"] == "error"


def test_the_unacked_report_still_names_the_process_model_on_the_1_to_1_path():
    """The default answer stays the default. Outside a room there is no second
    model to disagree with, and the 1:1 path's row must go on naming the model
    this process is on."""
    session = FakeSession("S4A", ScriptedDirector([]))
    runner = rvs.RealtimeVoiceSessionRunner(session, FakeWS())
    asyncio.run(runner._report_unacked_steering())
    (row,) = session.store.of("steer_unacked")
    assert row["model"] == rvs.realtime_model() == rt_mod.MODEL
    assert row["agent_id"] == runner.agent_id


def test_nothing_in_this_change_moves_REALTIME_MODEL():
    """The one value that is not ours to change. Both fixes are about a room
    carrying the model it was GIVEN; the study's own setting is decided by the
    PI and read from config, and neither file may write it."""
    for name in ("group_room.py", "realtime_voice_session.py"):
        src = (ROOT / "server" / name).read_text(encoding="utf-8")
        assert 'setting("REALTIME_MODEL"' not in src, name
        assert "REALTIME_MODEL =" not in src, name
        assert 'setenv("REALTIME_MODEL' not in src, name
    assert os.getenv("REALTIME_MODEL") in (None, GEMINI)
