"""The runner's two silent lies: the voice it sends, and the delivery it claims.

Both were invisible from inside a live encounter, which is why neither was
caught by a run that "worked".

The voice. `_voice()` read one field, `voice_id`, and that field means two
different things in this repo: the v3 loader writes Gemini voice names into it,
and the group scenario YAML keeps ElevenLabs voice ids in it, left over from
the v1 cascade. So a group encounter sent "9BWtsMINqrJLrRacOk9x" to a
speech-to-speech gateway as a voice name. Probed on the Cornell gateway on
2026-09-10, one socket per case: gpt-realtime-2.1 answers that with
`invalid_value` on session.audio.output.voice and NO session.updated, and
nto.gemini-live-2.5-flash answers with no frame at all and NO session.updated.
Either way the session.update carrying the character brief is refused whole, so
the actor plays the gateway's stock assistant, or nothing. Nothing in the
process can see it happen.

The delivery. Every director path — a planted beat, a group scene opening, a
character switch, the steering re-brief — reaches the actor through one
mid-session session.update, and the record was written when that send returned.
On gpt-realtime-2.1 that update is acknowledged in tens of milliseconds. On
nto.gemini-live-2.5-flash, the model config/consent.yaml commits the study to,
it is acknowledged never: three mid-session frames over two sockets, zero
session.updated back, no error. So the steering log asserted a delivery the
platform had not made, for every direction, on the family the study runs on.

And the attribution. Segment and interaction were recorded only INSIDE the
stage direction, so an actor turn that ran unsteered reached encounter_record
with both null — which is not a rare turn but the whole tail of every
interaction, once its planted beats are spent.

No network and no credentials here. The live evidence above is quoted, not
re-run; these tests hold the runner to it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import realtime_voice_session as rvs  # noqa: E402
from server.scenarios import load_scenario  # noqa: E402
from server.voice import realtime as rt_mod  # noqa: E402


GEMINI = "nto.gemini-live-2.5-flash"
GPT = "gpt-realtime-2.1"

# One character from each group scenario, carrying the id that started this.
AN_ELEVENLABS_ID = "9BWtsMINqrJLrRacOk9x"

GROUP_YAML_SCENARIOS = [
    "hidden_profile_vendor", "blameful_retro",
    "dominated_brainstorm", "public_retraction",
]


# --------------------------------------------------------------------------
# Fakes, in the shapes the runner actually uses.
# --------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.events = []
        self.started_at = 0.0

    def event(self, type_, **fields):
        self.events.append(dict(type=type_, **fields))

    def of(self, type_):
        return [e for e in self.events if e["type"] == type_]


class FakeEngine:
    def __init__(self, agent):
        self.agent = agent

    def _system_prompt(self, branches, note, group=False):
        return f"SYSTEM PROMPT for {self.agent.id}"


class FakeSession:
    def __init__(self, scenario_id):
        self.scenario = load_scenario(scenario_id, "p_test")
        self.is_group = self.scenario.mode == "group"
        self.engines = {a.id: FakeEngine(a) for a in self.scenario.cast}
        self.store = FakeStore()
        self.director = None
        self.triggered_branches = []
        self.shared_history = []
        self.steering_log = []
        self.broadcasts = []
        self.steer_delivered = []

    def append_agent(self, agent_id, text):
        self.shared_history.append({"speaker": agent_id, "text": text})

    async def broadcast(self, payload):
        self.broadcasts.append(payload)

    async def auto_steer(self, *, delivered=None):
        self.steering_log.append({"note": "shifted"})
        self.steer_delivered.append(delivered)


class FakeWS:
    def __init__(self):
        self.json = []

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, payload):
        pass


class AckRT:
    """A bridge that answers session.update the way the real one now does.

    `ack` is what update_instructions returns: True acknowledged, False asked
    and not answered, None nobody could tell. Mirrors the three-valued contract
    in server/voice/realtime.py so a runner tested against this is tested
    against the shape the gateway actually produces.
    """

    def __init__(self, ack=True, voice="Puck"):
        self.ws = object()
        self.voice = voice
        self.model = "fake-realtime"
        self.ack = ack
        self.last_update_acked = None
        self.instructions = []
        self.autofire_active = False
        self.pending_input = 0
        self._responding = False

    @property
    def responding(self):
        return self._responding

    async def update_instructions(self, instructions):
        self.instructions.append(instructions)
        self.last_update_acked = self.ack
        return self.ack

    async def cancel_response(self):
        self._responding = False

    def clear_response_state(self):
        self._responding = False


def make_runner(scenario_id):
    session = FakeSession(scenario_id)
    runner = rvs.RealtimeVoiceSessionRunner(session, FakeWS())
    return runner, session


@pytest.fixture
def on_model(monkeypatch):
    """Run the runner as though REALTIME_MODEL named this model.

    The runner reads the model off the bridge module at call time precisely so
    that this is possible: the study cannot be asked to switch families to be
    tested, because config/consent.yaml names the one it runs on.
    """
    def _set(model):
        monkeypatch.setattr(rt_mod, "MODEL", model)
        return model
    return _set


# --------------------------------------------------------------------------
# 1. A scenario's ElevenLabs id must never reach a realtime session.
# --------------------------------------------------------------------------

def test_no_group_scenario_can_put_an_elevenlabs_id_on_the_wire(on_model):
    """The defect, over every scenario that carries one.

    Held over all four group YAML scenarios rather than one, because the id is
    per character: the fix has to be a rule about what may be sent, not a patch
    to the one cast somebody happened to test.
    """
    on_model(GEMINI)
    # Straight off the capability table, not off the runner's own accessor:
    # what may go on the wire is the table's claim, and this test is about
    # whether the runner obeys it.
    roster = rt_mod.capabilities_for(GEMINI).voices
    for scenario_id in GROUP_YAML_SCENARIOS:
        runner, _ = make_runner(scenario_id)
        for agent in runner.cast:
            chosen = runner._voice_for(agent)
            assert chosen in roster, (
                f"{scenario_id}/{agent.id} would open its session with "
                f"{chosen!r}; the gateway refuses the whole session.update "
                "for an unknown voice, so the character brief never lands"
            )


def test_the_id_is_still_in_the_scenario_file(on_model):
    """Guard the test above against passing for the wrong reason.

    If the group scenarios ever stop carrying ElevenLabs ids, the assertion
    above becomes vacuous and would keep passing while the runner's rule was
    removed. This is what notices."""
    session = FakeSession("hidden_profile_vendor")
    ids = [getattr(a, "voice_id", None) for a in session.scenario.cast]
    assert AN_ELEVENLABS_ID in ids, (
        "the scenario no longer carries the id these tests are about"
    )


def test_a_voice_from_the_other_family_is_refused_too(on_model):
    """The same rule, in the direction that only appears if the study moves.

    S1A's cast is cast in Gemini voice names by the v3 loader. On the gpt
    family those are not merely different, they are rejected: probed live,
    voice="Puck" against gpt-realtime-2.1 returns invalid_value and no
    session.updated, exactly as an ElevenLabs id does. A capability table that
    is consulted only for ElevenLabs ids is not a capability table."""
    on_model(GPT)
    runner, _ = make_runner("S1A")
    roster = rt_mod.capabilities_for(GPT).voices
    assert "Fenrir" == runner.cast[0].voice_id, "S1A's casting changed"
    for agent in runner.cast:
        assert runner._voice_for(agent) in roster


def test_a_gemini_scenario_voice_survives_on_gemini(on_model):
    """And the casting is not thrown away when it IS speakable.

    The v3 loader's voice names are a casting decision — consecutive characters
    are given different voices on purpose — and a fix that ignored `voice_id`
    outright would silently recast every v3 scenario."""
    on_model(GEMINI)
    runner, _ = make_runner("S1A")
    assert runner._voice_for(runner.cast[0]) == "Fenrir"
    assert runner._voice_for(runner.cast[1]) == "Charon"


def test_characters_without_a_usable_voice_still_sound_different(on_model):
    """Falling back must not collapse a room into one voice.

    Three characters answering in the same voice is not a cosmetic problem in a
    group encounter: the participant is being scored on whether they addressed
    the right person."""
    on_model(GPT)
    runner, _ = make_runner("hidden_profile_vendor")
    chosen = [runner._voice_for(a) for a in runner.cast]
    assert len(set(chosen)) == len(chosen), f"the room shares voices: {chosen}"


def test_the_realtime_field_wins_over_the_cascade_field(on_model):
    """`realtime_voice` is the realtime path's own field and is asked first."""
    on_model(GEMINI)
    runner, _ = make_runner("S1A")
    agent = runner.cast[0]
    object.__setattr__(agent, "realtime_voice", "Aoede")
    assert runner._voice_for(agent) == "Aoede"


def test_the_realtime_field_is_checked_against_the_roster_as_well(on_model):
    """Being the right FIELD does not make a value the right VALUE.

    A scenario author writing `realtime_voice: Puck` while the study runs on
    gpt would mute that character exactly as an ElevenLabs id does, and the
    field name is no protection at all."""
    on_model(GPT)
    runner, _ = make_runner("S1A")
    agent = runner.cast[0]
    object.__setattr__(agent, "realtime_voice", "Puck")
    assert runner._voice_for(agent) in rt_mod.capabilities_for(GPT).voices


def test_a_voice_that_cannot_be_spoken_is_written_down_once(on_model):
    """A casting decision that did not survive the run is a fact about the data.

    A rater listening for two distinguishable characters, or an analyst asking
    why a scenario sounds nothing like its author's notes, needs the value
    itself: an ElevenLabs id and a misspelt Gemini name are different repairs
    to the scenario file. Once, because _voice_of runs on every character
    switch and on every member of a room."""
    on_model(GEMINI)
    runner, session = make_runner("hidden_profile_vendor")
    agent = runner.cast[0]
    for _ in range(4):
        runner._voice_for(agent)

    rows = session.store.of("realtime_voice_unusable")
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    assert rows[0]["value"] == AN_ELEVENLABS_ID
    assert rows[0]["agent_id"] == agent.id
    assert rows[0]["model"] == GEMINI
    assert "Puck" in rows[0]["offered"], (
        "the row does not say what the model would have accepted, so it "
        "cannot be acted on without re-deriving the roster"
    )


def test_a_usable_voice_is_not_reported_as_a_problem(on_model):
    on_model(GEMINI)
    runner, session = make_runner("S1A")
    runner._voice_for(runner.cast[0])
    assert not session.store.of("realtime_voice_unusable")


# --------------------------------------------------------------------------
# 2. One place to read what a model needs.
# --------------------------------------------------------------------------

def test_the_roster_is_the_capability_table_and_not_a_second_copy(on_model):
    """The runner must not carry its own idea of what a model accepts.

    Two rosters is how they drift, and a drifted roster is silent: the voice is
    simply refused and the persona goes with it. So this asserts identity with
    the table rather than equality with a literal list."""
    for model in (GEMINI, GPT):
        on_model(model)
        caps = rt_mod.capabilities_for(model)
        assert rvs.realtime_voices() == list(caps.voices)


def test_the_two_families_do_not_share_a_single_voice():
    """Why the roster has to be per-model and not a superset.

    Nothing in either list is accepted by the other family, so there is no
    'safe' voice to default to and no way to be right about a voice without
    first being right about the model."""
    gem = set(rt_mod.capabilities_for(GEMINI).voices)
    gpt = set(rt_mod.capabilities_for(GPT).voices)
    assert not (gem & gpt)


def test_a_model_the_table_does_not_cover_names_no_voice(on_model):
    """An uncovered model must not be given an invented voice.

    Guessing is how a study ends up on a model whose behaviour nobody checked,
    with a voice nobody checked either. Naming none leaves the refusal to
    require_capabilities at connect, which can say which model and which
    table."""
    on_model("nto.some-model-nobody-has-probed")
    runner, _ = make_runner("S1A")
    assert rvs.realtime_voices() == []
    assert runner._voice_for(runner.cast[0]) == ""


# --------------------------------------------------------------------------
# 3. Steering bookkeeping may not assert what it did not see.
# --------------------------------------------------------------------------

def _fire_a_beat(ack):
    """Brief the actor with its next planted beat on a bridge that answers `ack`."""
    async def scenario():
        runner, session = make_runner("S1A")
        runner.rt = AckRT(ack=ack)
        await runner._brief_next_beat(probing=False)
        return session
    return asyncio.run(scenario())


@pytest.mark.parametrize("ack", [True, False, None])
def test_a_stage_direction_records_what_the_platform_said(ack):
    """The three answers, kept apart.

    True the gateway acknowledged the update; False it was asked and did not;
    None this bridge could not tell. Collapsing the last two into "delivered"
    is what made every direction on Gemini look like it had landed, and
    collapsing them into "not delivered" would blame the gateway for a gap in
    our own instrumentation."""
    session = _fire_a_beat(ack)
    (row,) = session.store.of("stage_direction")
    assert "acked" in row, (
        "the stage direction says nothing about whether it arrived, so an "
        "analyst cannot tell a direction the actor received from one it did not"
    )
    assert row["acked"] is ack


def test_a_beat_the_actor_never_received_is_still_a_beat_that_fired():
    """The brief left; the gateway did not answer. Those are two facts.

    trigger_fired is coverage — verify_record counts it to decide whether an
    encounter reached its scored moments — and retracting it here would be a
    second wrong answer. The direction is recorded as unacknowledged instead,
    which is the fact that is actually known."""
    session = _fire_a_beat(False)
    assert session.store.of("trigger_fired")
    assert session.store.of("stage_direction")[0]["acked"] is False


def _steer_once(ack):
    async def scenario():
        runner, session = make_runner("S1A")
        runner.rt = AckRT(ack=ack)
        await runner._steer()
        return session
    return asyncio.run(scenario())


@pytest.mark.parametrize("ack", [True, False, None])
def test_the_steering_event_says_whether_the_actor_was_told(ack):
    """steer_delivered used to mean only "not deferred".

    It was written the instant the send returned, which on the study's own
    model is every time and means nothing. The event still marks the difference
    from steer_deferred — the brief was issued rather than held back — and
    `acked` now carries the half that was missing."""
    session = _steer_once(ack)
    (row,) = session.store.of("steer_delivered")
    assert "acked" in row, "a steer that claims delivery must say who confirmed it"
    assert row["acked"] is ack


def test_an_unacknowledged_encounter_is_dated_once():
    """One row, not one per turn.

    On a family that acknowledges no mid-session update this fires on every
    direction, and a row per turn buries the fact it exists to carry: from that
    moment on, the planted beats that make the encounter scoreable are not
    reaching the actor."""
    async def scenario():
        runner, session = make_runner("S1A")
        runner.rt = AckRT(ack=False)
        await runner._steer()
        await runner._steer()
        await runner._brief_next_beat(probing=False)
        return session

    session = asyncio.run(scenario())
    rows = session.store.of("steer_unacked")
    assert len(rows) == 1, f"expected one row, got {len(rows)}"
    assert rows[0]["model"] == rvs.realtime_model()


def test_an_acknowledged_encounter_is_not_accused_of_anything():
    session = _steer_once(True)
    assert not session.store.of("steer_unacked")


def test_a_bridge_that_cannot_tell_is_not_read_as_a_refusal():
    """None is not False.

    "No ack observed" while nothing is draining the socket says something about
    this process, not about the gateway, and an encounter must not be marked as
    unsteered on the strength of it."""
    session = _steer_once(None)
    assert not session.store.of("steer_unacked")
    assert session.store.of("steer_delivered")[0]["acked"] is None


def test_a_deferred_steer_still_claims_nothing():
    """The pre-existing gap this sits next to: a re-brief held back because a
    reply is in flight goes out as steer_deferred and no steer_delivered at
    all. That must stay true — a deferred brief was never sent, so it has no
    ack to report."""
    async def scenario():
        runner, session = make_runner("S1A")
        rt = AckRT(ack=True)
        rt._responding = True
        runner.rt = rt
        await runner._steer()
        return session

    session = asyncio.run(scenario())
    assert session.store.of("steer_deferred")
    assert not session.store.of("steer_delivered")
    assert not session.store.of("steer_unacked")


# --------------------------------------------------------------------------
# 4. Per-turn attribution, on the turn.
# --------------------------------------------------------------------------

def test_an_unsteered_turn_keeps_its_segment_and_interaction():
    """The deterministic one.

    An interaction has a finite number of planted beats. Every actor turn after
    the last one is spent runs with no stage direction, and segment and
    interaction lived only inside the direction — so the tail of every
    interaction reached the record unattributed, in the half a rater reads by
    segment. Which interaction a line belongs to is a fact about when it was
    spoken, not about whether anyone was directing at the time."""
    async def scenario():
        runner, session = make_runner("S1A")
        rt = AckRT()
        runner.rt = rt
        runner.segment = 1
        # `direction` is snapshotted at spawn time and passed in; None is a
        # turn that ran with no beat pending, which is the case at issue.
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(runner._finalize_turn(
            "riley", runner.agent, rt, ["an unsteered line"], None, stop=stop,
        ), timeout=5)
        return runner, session

    runner, session = asyncio.run(scenario())
    (pair,) = session.store.of("steering_pair")
    assert pair["direction"] is None, "this turn is supposed to be unsteered"
    assert pair["segment"] == 1
    assert pair["interaction"] == runner._interaction_id()


def test_a_group_turn_keeps_its_segment_and_interaction():
    """The same gap on the group finalizer, which is S3 and S4 — half the study."""
    async def scenario():
        runner, session = make_runner("S4A")
        runner.segment = 1
        await runner._finalize_member_inner(runner.cast[0], "an unsteered line")
        return runner, session

    runner, session = asyncio.run(scenario())
    (pair,) = session.store.of("steering_pair")
    assert pair["direction"] is None
    assert pair["segment"] == 1
    assert pair["interaction"] == runner._interaction_id()


def test_the_record_names_the_voice_the_participant_actually_heard():
    """The group pair used to copy the scenario's `voice_id` straight in.

    On a group YAML scenario that is an ElevenLabs id — a voice no participant
    has ever heard, because it was refused by the gateway before a word was
    spoken. A record that names it is not merely unhelpful; it is evidence of a
    delivery that did not happen."""
    async def scenario():
        runner, session = make_runner("hidden_profile_vendor")
        await runner._finalize_member_inner(runner.cast[0], "a line")
        return session

    session = asyncio.run(scenario())
    (pair,) = session.store.of("steering_pair")
    assert pair["actor"]["voice"] != AN_ELEVENLABS_ID
    assert pair["actor"]["voice"] in rt_mod.capabilities_for(GEMINI).voices
